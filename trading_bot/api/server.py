"""
Phase 4.0 — Public SaaS Boundary (Read-Only Analytics Layer).

A FastAPI application that exposes **read-only** analytics drawn from
files already written to disk by the Core bot (daily reports, the
experiment manifest). The API:

- Never imports any Core execution, scoring, or filter module.
- Never places, simulates, or automates trades.
- Never exposes scorer weights, filter internals, or raw decision
  pipeline state — only aggregated stats, guardrails, readiness,
  and shadow-filter simulation rows.

Endpoints:
    GET  /health                            — liveness probe (no auth)
    GET  /reports/latest                    — most recent daily report
    GET  /reports/{date}                    — daily report for YYYY-MM-DD
    GET  /experiments/recent?limit=N        — last N manifest records
    GET  /experiments/{n}                   — nth-most-recent manifest
                                              record (1 = most recent)

Authentication:
    The server requires an `Authorization: Bearer <token>` header on
    every endpoint except `/health`. The token must match the env var
    `TRADING_API_KEY`. If `TRADING_API_KEY` is unset the API refuses
    every protected request with 503 — a mis-deployed server must
    not accept traffic by accident.

Runtime configuration (env vars, read per-request so no restart is
needed when paths change):
    TRADING_API_KEY              — required for protected endpoints
    TRADING_API_REPORTS_DIR      — directory holding alpha_report_*.json
                                   (default: reports)
    TRADING_API_MANIFEST_PATH    — JSONL manifest path
                                   (default: data/alpha_experiments.jsonl)

Run with:
    uvicorn trading_bot.api.server:app --reload
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Runtime config (env-driven, evaluated per request)
# ---------------------------------------------------------------------------

API_KEY_ENV_VAR = "TRADING_API_KEY"
REPORTS_DIR_ENV_VAR = "TRADING_API_REPORTS_DIR"
MANIFEST_PATH_ENV_VAR = "TRADING_API_MANIFEST_PATH"

DEFAULT_REPORTS_DIR = "reports"
DEFAULT_MANIFEST_PATH = "data/alpha_experiments.jsonl"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _reports_dir() -> Path:
    return Path(os.getenv(REPORTS_DIR_ENV_VAR, DEFAULT_REPORTS_DIR))


def _manifest_path() -> Path:
    return Path(os.getenv(MANIFEST_PATH_ENV_VAR, DEFAULT_MANIFEST_PATH))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_security = HTTPBearer(auto_error=False)


def require_api_key(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> None:
    """
    Reject any request that does not carry the correct
    `Authorization: Bearer <TRADING_API_KEY>` header.

    - 503 when the server has no API key configured. The server must
      be explicitly set up to accept traffic.
    - 401 when the header is missing or non-Bearer.
    - 403 when the header's token does not match.
    """
    configured = (os.getenv(API_KEY_ENV_VAR, "") or "").strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "API key not configured on server; set TRADING_API_KEY "
                "before accepting requests"
            ),
        )
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization: Bearer <token> header",
        )
    if (creds.scheme or "").lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization scheme must be Bearer",
        )
    if creds.credentials != configured:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )


# ---------------------------------------------------------------------------
# Sanitization — the SaaS boundary. See docs/CORE_CONTROL.md.
# ---------------------------------------------------------------------------


def _sanitize_report(data: dict) -> dict:
    """
    Strip Core internals from a daily report dict before returning it.

    Keeps:
      - aggregated stats (tier_stats, reason_stats, regime_stats,
        decile_stats, totals, shadow_filter_simulation)
      - guardrails
      - promotion_readiness
      - scorer_fingerprint (hash string only)
      - report metadata (report_date, report_type)

    Strips:
      - scorer_config (weights, tier thresholds, regime scores)
      - filesystem paths embedded in `sources`
    """
    if not isinstance(data, dict):
        return {}
    out = dict(data)
    out.pop("scorer_config", None)
    if "sources" in out:
        sanitized_sources: dict[str, dict] = {}
        for name, meta in (out["sources"] or {}).items():
            if not isinstance(meta, dict):
                continue
            sanitized_sources[name] = {
                "exists": bool(meta.get("exists")),
                "rows": int(meta.get("rows", 0) or 0),
                "resolved_files": int(meta.get("resolved_files", 0) or 0),
            }
        out["sources"] = sanitized_sources
    return out


def _sanitize_manifest(record: dict) -> dict:
    """
    Strip Core internals from a manifest record.

    Keeps:
      - timestamp, report_date, git_commit
      - scorer_fingerprint (hash string)
      - env (already-redacted by Phase 3.6 snapshot_env)
      - totals, promotion_readiness, guardrails
      - shadow_filter_ab_summary

    Strips:
      - scorer_config (weights)
      - report_paths (server-internal filesystem paths)
    """
    if not isinstance(record, dict):
        return {}
    out = dict(record)
    out.pop("scorer_config", None)
    out.pop("report_paths", None)
    return out


def _read_manifest_records(path: Path) -> list[dict]:
    """Read JSONL manifest, skipping blank/malformed lines. Never raises."""
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("api.manifest_read_error", path=str(path), error=str(exc))
        return []
    records: list[dict] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except Exception:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _parse_report_file(path: Path) -> dict:
    """Read and JSON-parse a daily report file. Raises 500 on parse errors."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("api.report_parse_error", path=str(path), error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to parse report: {type(exc).__name__}",
        )


def _validate_date(date: str) -> None:
    if not _DATE_RE.match(date or ""):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date must be YYYY-MM-DD",
        )


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


app = FastAPI(
    title="Momentum Trading Bot — Analytics API",
    description=(
        "Read-only analytics layer. No trade execution, no alpha "
        "scoring internals, no real-time decision pipeline. "
        "Serves pre-written daily reports and the append-only "
        "experiment manifest."
    ),
    version="1.0.0",
)


@app.get("/health", tags=["public"])
def health() -> dict[str, Any]:
    """Liveness probe — intentionally unauthenticated."""
    return {
        "status": "ok",
        "service": "momentum-trading-bot-analytics",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get(
    "/reports/latest",
    tags=["reports"],
    dependencies=[Depends(require_api_key)],
)
def latest_report() -> dict[str, Any]:
    """Return the most recent daily alpha validation report."""
    reports = _reports_dir()
    if not reports.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="reports directory not found",
        )
    candidates = sorted(reports.glob("alpha_report_*.json"))
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no daily reports available",
        )
    data = _parse_report_file(candidates[-1])
    return _sanitize_report(data)


@app.get(
    "/reports/{date}",
    tags=["reports"],
    dependencies=[Depends(require_api_key)],
)
def report_for_date(date: str) -> dict[str, Any]:
    """Return the daily report for the given YYYY-MM-DD."""
    _validate_date(date)
    path = _reports_dir() / f"alpha_report_{date}.json"
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no report for {date}",
        )
    return _sanitize_report(_parse_report_file(path))


@app.get(
    "/experiments/recent",
    tags=["experiments"],
    dependencies=[Depends(require_api_key)],
)
def recent_experiments(
    limit: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    """
    Return the last N experiment manifest records (default 10,
    maximum 100). Empty manifest → `{"count": 0, "records": []}`.
    """
    records = _read_manifest_records(_manifest_path())
    tail = records[-limit:] if limit > 0 else records
    sanitized = [_sanitize_manifest(r) for r in tail]
    return {"count": len(sanitized), "records": sanitized}


@app.get(
    "/experiments/{n}",
    tags=["experiments"],
    dependencies=[Depends(require_api_key)],
)
def experiment_by_index(n: int) -> dict[str, Any]:
    """
    Return the nth-most-recent experiment manifest record.

    `n=1` is the most recent; `n=2` is the one before; etc.
    """
    if n < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="n must be >= 1 (1 = most recent)",
        )
    records = _read_manifest_records(_manifest_path())
    if not records:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no experiment records available",
        )
    if n > len(records):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"only {len(records)} experiment record(s) on file",
        )
    return _sanitize_manifest(records[-n])


# Nothing below this line. The api module deliberately imports
# nothing from trading_bot.core, trading_bot.main, or any execution
# path — that constraint is verified structurally by tests.
