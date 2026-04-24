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
from html import escape as _esc
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse
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


# ---------------------------------------------------------------------------
# Phase 4.1 — read-only dashboard UI
# ---------------------------------------------------------------------------


_DASHBOARD_CSS = """
<style>
  :root {
    color-scheme: light;
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, sans-serif;
    margin: 2em;
    color: #222;
    background: #fafafa;
  }
  h1 { font-size: 1.45em; margin-bottom: 0.3em; color: #111; }
  h2 {
    font-size: 1.05em;
    border-bottom: 1px solid #ddd;
    padding-bottom: 0.3em;
    margin-top: 2em;
    color: #333;
  }
  section { margin-top: 1.5em; }
  table { border-collapse: collapse; margin: 0.5em 0; background: #fff; }
  th, td {
    padding: 0.35em 0.75em;
    border: 1px solid #e0e0e0;
    text-align: left;
    font-size: 0.9em;
    white-space: nowrap;
  }
  th { background: #f3f3f3; font-weight: 600; color: #444; }
  .meta { color: #666; font-size: 0.85em; }
  .empty { color: #666; font-style: italic; margin: 1em 0; }
  .fingerprint {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.78em;
    color: #666;
  }
  .status-ok       { color: #1a7f1a; font-weight: 600; }
  .status-warning  { color: #b85c00; font-weight: 600; }
  .status-critical { color: #c0262f; font-weight: 600; }
  .status-insufficient_data { color: #666; font-weight: 600; }
  .status-promising { color: #1a6fa3; font-weight: 600; }
  .status-ready_for_shadow_filter_test { color: #1a7f1a; font-weight: 600; }
  .status-weak    { color: #b85c00; font-weight: 600; }
  .status-not_ready { color: #666; font-weight: 600; }
  .kv td:first-child { color: #555; font-weight: 600; }
  footer { margin-top: 3em; color: #999; font-size: 0.8em; }
</style>
""".strip()


def _status_span(value: Optional[str]) -> str:
    """Render a status string inside a span with the corresponding CSS class."""
    if value is None:
        return '<span class="meta">n/a</span>'
    safe = _esc(str(value))
    css_class = f"status-{_esc(str(value))}"
    return f'<span class="{css_class}">{safe}</span>'


def _fmt_pct(value) -> str:
    try:
        if value is None:
            return "n/a"
        return f"{float(value):.2%}"
    except Exception:
        return "n/a"


def _fmt_num(value, spec: str = ".2f") -> str:
    try:
        if value is None:
            return "n/a"
        return format(float(value), spec)
    except Exception:
        return "n/a"


def _render_totals(report: dict) -> str:
    totals = report.get("totals") or {}
    if not totals:
        return '<p class="empty">(no totals available)</p>'
    rows = "".join(
        f"<tr><td>{_esc(str(k))}</td><td>{_esc(str(v))}</td></tr>"
        for k, v in totals.items()
    )
    return f'<table class="kv">{rows}</table>'


def _render_guardrail(report: dict) -> str:
    gr = report.get("guardrails") or {}
    status_html = _status_span(gr.get("status"))
    action = _esc(str(gr.get("recommended_action") or ""))
    reasons = gr.get("reasons") or []
    if reasons:
        reasons_html = "<ul>" + "".join(
            f"<li>{_esc(str(r))}</li>" for r in reasons
        ) + "</ul>"
    else:
        reasons_html = '<p class="meta">(no reasons recorded)</p>'
    return (
        f"<p>Status: {status_html}</p>"
        f"<p><strong>Recommended action:</strong> {action or '<em>none</em>'}</p>"
        f"<p><strong>Reasons:</strong></p>{reasons_html}"
    )


def _render_readiness(report: dict) -> str:
    pr = report.get("promotion_readiness") or {}
    if not pr:
        return '<p class="empty">(no readiness data)</p>'
    status_html = _status_span(pr.get("status"))
    outcome = pr.get("outcome_count", "n/a")
    min_req = pr.get("min_required_outcomes", "n/a")
    ab = pr.get("ab") or {}
    cdf = pr.get("cdf") or {}

    def _side_row(name: str, side: dict) -> str:
        return (
            f"<tr>"
            f"<td>{_esc(name)}</td>"
            f"<td>{_esc(str(side.get('outcome_count', 'n/a')))}</td>"
            f"<td>{_fmt_pct(side.get('win_rate'))}</td>"
            f"<td>{_fmt_num(side.get('avg_r_multiple'))}</td>"
            f"</tr>"
        )

    return (
        f"<p>Status: {status_html}</p>"
        f"<p>Outcomes: {_esc(str(outcome))} / {_esc(str(min_req))} required</p>"
        f'<table><tr><th>cohort</th><th>outcomes</th><th>win_rate</th>'
        f"<th>avg_R</th></tr>"
        f"{_side_row('A/B', ab)}{_side_row('C/D/F', cdf)}"
        f"</table>"
    )


def _render_shadow_sim(report: dict) -> str:
    rows = report.get("shadow_filter_simulation") or []
    if not rows:
        return '<p class="empty">(no shadow simulation rows)</p>'
    header = (
        "<tr>"
        "<th>threshold</th>"
        "<th>allowed_buys</th>"
        "<th>blocked_buys</th>"
        "<th>allowed_outcomes</th>"
        "<th>allowed_win_rate</th>"
        "<th>allowed_avg_R</th>"
        "<th>blocked_outcomes</th>"
        "<th>blocked_win_rate</th>"
        "<th>blocked_avg_R</th>"
        "</tr>"
    )
    body = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        body.append(
            "<tr>"
            f"<td>{_esc(str(row.get('threshold', '')))}</td>"
            f"<td>{_esc(str(row.get('allowed_buy_count', 'n/a')))}</td>"
            f"<td>{_esc(str(row.get('blocked_buy_count', 'n/a')))}</td>"
            f"<td>{_esc(str(row.get('allowed_outcome_count', 'n/a')))}</td>"
            f"<td>{_fmt_pct(row.get('allowed_win_rate'))}</td>"
            f"<td>{_fmt_num(row.get('allowed_avg_r_multiple'))}</td>"
            f"<td>{_esc(str(row.get('blocked_outcome_count', 'n/a')))}</td>"
            f"<td>{_fmt_pct(row.get('blocked_win_rate'))}</td>"
            f"<td>{_fmt_num(row.get('blocked_avg_r_multiple'))}</td>"
            "</tr>"
        )
    return f"<table>{header}{''.join(body)}</table>"


def _render_experiments(records: list[dict]) -> str:
    if not records:
        return '<p class="empty">(no experiments recorded yet)</p>'
    header = (
        "<tr>"
        "<th>timestamp</th>"
        "<th>report_date</th>"
        "<th>guardrail</th>"
        "<th>readiness</th>"
        "<th>fingerprint</th>"
        "</tr>"
    )
    body = []
    # Show newest first for operator readability.
    for rec in reversed(records):
        if not isinstance(rec, dict):
            continue
        gr = (rec.get("guardrails") or {}).get("status")
        pr = (rec.get("promotion_readiness") or {}).get("status")
        fp = str(rec.get("scorer_fingerprint") or "")
        fp_display = f"{fp[:12]}…" if fp else "—"
        body.append(
            "<tr>"
            f"<td>{_esc(str(rec.get('timestamp', '')))}</td>"
            f"<td>{_esc(str(rec.get('report_date', '')))}</td>"
            f"<td>{_status_span(gr)}</td>"
            f"<td>{_status_span(pr)}</td>"
            f'<td class="fingerprint">{_esc(fp_display)}</td>'
            "</tr>"
        )
    return f"<table>{header}{''.join(body)}</table>"


def render_dashboard_html(
    report: Optional[dict],
    experiments: list[dict],
) -> str:
    """
    Build the dashboard HTML from sanitized inputs.

    Pure function: does no I/O. The caller must pass already-sanitized
    report and experiments dicts (via `_sanitize_report` /
    `_sanitize_manifest`). Because of that contract, no amount of
    upstream leakage can spill into the HTML — this renderer simply
    cannot access fields that have already been stripped.
    """
    generated_at = _esc(datetime.now(timezone.utc).isoformat())

    if report is None:
        report_block = (
            '<section><h2>Latest report</h2>'
            '<p class="empty">No daily reports available yet.</p>'
            "</section>"
        )
    else:
        report_date = _esc(str(report.get("report_date") or "unknown"))
        fp = _esc(str(report.get("scorer_fingerprint") or ""))
        fp_display = (
            f'<p class="fingerprint">scorer_fingerprint: {fp}</p>'
            if fp else ""
        )
        report_block = (
            "<section>"
            f"<h2>Latest report — {report_date}</h2>"
            f"{fp_display}"
            "<h3>Guardrails</h3>"
            f"{_render_guardrail(report)}"
            "<h3>Promotion readiness</h3>"
            f"{_render_readiness(report)}"
            "<h3>Totals</h3>"
            f"{_render_totals(report)}"
            "<h3>Shadow filter simulation</h3>"
            f"{_render_shadow_sim(report)}"
            "</section>"
        )

    experiments_block = (
        "<section>"
        "<h2>Recent experiments</h2>"
        f"{_render_experiments(experiments)}"
        "</section>"
    )

    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<title>Momentum Trading Bot — Analytics Dashboard</title>"
        f"{_DASHBOARD_CSS}"
        "</head><body>"
        "<h1>Momentum Trading Bot — Analytics Dashboard</h1>"
        f'<p class="meta">Read-only view. Generated at {generated_at}.</p>'
        f"{report_block}"
        f"{experiments_block}"
        "<footer>"
        "This dashboard is served by the Phase 4.0/4.1 SaaS boundary "
        "(trading_bot.api.server). It never exposes trade execution, "
        "alpha scoring weights, or Core decision state."
        "</footer>"
        "</body></html>"
    )


@app.get(
    "/dashboard",
    response_class=HTMLResponse,
    tags=["dashboard"],
    dependencies=[Depends(require_api_key)],
)
def dashboard() -> HTMLResponse:
    """
    Read-only HTML dashboard rendering the most recent daily report
    plus the last handful of experiment-manifest records.

    Renders gracefully when the reports directory or manifest are
    missing: the page still returns 200 with explicit empty states.
    Never exposes scorer_config, filesystem paths, raw webhook URL,
    or any execution control.
    """
    # Load latest report (best-effort — empty state wins on any error).
    report: Optional[dict] = None
    reports = _reports_dir()
    if reports.is_dir():
        candidates = sorted(reports.glob("alpha_report_*.json"))
        if candidates:
            try:
                raw = json.loads(candidates[-1].read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning(
                    "dashboard.report_parse_error",
                    path=str(candidates[-1]),
                    error=str(exc),
                )
                raw = None
            if isinstance(raw, dict):
                report = _sanitize_report(raw)

    # Load last 10 experiments.
    records = _read_manifest_records(_manifest_path())
    experiments = [_sanitize_manifest(r) for r in records[-10:]] if records else []

    html = render_dashboard_html(report, experiments)
    return HTMLResponse(content=html, status_code=200)


# Nothing below this line. The api module deliberately imports
# nothing from trading_bot.core, trading_bot.main, or any execution
# path — that constraint is verified structurally by tests.
