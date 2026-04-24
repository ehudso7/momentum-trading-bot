"""
Phase 3.6 — Alpha experiment manifest.

Append-only JSONL record of every paper-filter run's configuration
alongside the guardrail result. One record per daily report,
written to ``data/alpha_experiments.jsonl`` by default.

Audit/reporting only. Nothing in this module affects trading,
scoring, filtering, risk, execution, or portfolio code. The module
never reads back the manifest to make a decision — it is purely a
write-once, read-many audit trail for humans and future offline
analytics.

Safety:
    - Raw webhook URLs are NEVER stored. The env snapshot records
      only a boolean "<var>_present" for secrets-like fields.
    - Every I/O failure is caught — append_manifest_record returns
      (ok, err) and the daily-report caller is expected to ignore
      err except for logging.
    - Writes are serialized through a module-level threading lock
      so concurrent callers cannot interleave JSONL rows.

Usage:
    # Programmatic append (from daily_report.py):
    record = build_manifest_record(...)
    append_manifest_record(record, manifest_path=...)

    # Operator tail:
    python -m trading_bot.reporting.experiment_manifest --tail 10
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import structlog

log = structlog.get_logger(__name__)


DEFAULT_MANIFEST_PATH = "data/alpha_experiments.jsonl"

# Tracked env vars. `TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL` is
# deliberately NOT in this list — it's handled specially below.
TRACKED_ENV_VARS: tuple[str, ...] = (
    "TRADING_ALPHA_FILTER_ENABLED",
    "TRADING_ALPHA_FILTER_MIN_TIER",
    "TRADING_DATA_ROTATION",
)

# Env vars that are treated as secrets — stored as presence bool only.
SECRET_ENV_VARS: tuple[str, ...] = (
    "TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL",
)

GIT_COMMIT_TIMEOUT_SECONDS = 2.0

_WRITE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _env_present(name: str) -> bool:
    """Env var treated as a secret: True iff set to a non-empty value."""
    return bool((os.getenv(name, "") or "").strip())


def snapshot_env() -> dict:
    """
    Capture the tracked env vars plus booleans for secret-ish vars.

    Public (no leading underscore) so tests and future phases can
    reuse the same redaction rules.
    """
    snap: dict = {}
    for name in TRACKED_ENV_VARS:
        snap[name] = os.getenv(name, "") or ""
    for name in SECRET_ENV_VARS:
        snap[f"{name}_present"] = _env_present(name)
    return snap


def get_git_commit() -> Optional[str]:
    """
    Best-effort `git rev-parse HEAD`. Returns None if git is not
    installed, the working tree is not a git checkout, or the
    command takes too long. Never raises.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_COMMIT_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    commit = (result.stdout or "").strip()
    return commit or None


def _extract_ab_summary(
    shadow_filter_simulation: Optional[list],
) -> Optional[dict]:
    """Pull the A+B row out of the shadow-filter simulation payload."""
    if not shadow_filter_simulation:
        return None
    for row in shadow_filter_simulation:
        if not isinstance(row, dict):
            continue
        if row.get("threshold") == "A+B":
            return {
                "threshold": "A+B",
                "allowed_buy_count": row.get("allowed_buy_count"),
                "blocked_buy_count": row.get("blocked_buy_count"),
                "allowed_outcome_count": row.get("allowed_outcome_count"),
                "blocked_outcome_count": row.get("blocked_outcome_count"),
                "allowed_win_rate": row.get("allowed_win_rate"),
                "blocked_win_rate": row.get("blocked_win_rate"),
                "allowed_avg_pnl": row.get("allowed_avg_pnl"),
                "blocked_avg_pnl": row.get("blocked_avg_pnl"),
                "allowed_avg_r_multiple": row.get("allowed_avg_r_multiple"),
                "blocked_avg_r_multiple": row.get("blocked_avg_r_multiple"),
            }
    return None


def build_manifest_record(
    *,
    report_date: str,
    scorer_fingerprint: str,
    scorer_config: dict,
    guardrails: dict,
    totals: dict,
    promotion_readiness: dict,
    shadow_filter_simulation: Optional[list[dict]],
    txt_path: Union[str, Path],
    json_path: Union[str, Path],
    git_commit: Optional[str] = None,
    env: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> dict:
    """
    Assemble a single manifest record dict.

    Deliberately explicit: every input is a named argument so test
    fixtures can build records without touching real I/O. Defaults
    that *do* touch I/O (env, git_commit, now) are called exactly
    when an explicit value is not provided.
    """
    timestamp = (now or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%S")
    env_snap = env if env is not None else snapshot_env()
    git = git_commit if git_commit is not None else get_git_commit()

    return {
        "timestamp": timestamp,
        "report_date": report_date,
        "git_commit": git,
        "scorer_fingerprint": scorer_fingerprint,
        "scorer_config": _deep_copy_json_safe(scorer_config),
        "env": _deep_copy_json_safe(env_snap),
        "report_paths": {
            "text": str(txt_path),
            "json": str(json_path),
        },
        "totals": _deep_copy_json_safe(totals or {}),
        "promotion_readiness": _deep_copy_json_safe(promotion_readiness or {}),
        "guardrails": _deep_copy_json_safe(guardrails or {}),
        "shadow_filter_ab_summary": _extract_ab_summary(shadow_filter_simulation),
    }


def _deep_copy_json_safe(value):
    """Cheap JSON round-trip to detach and strip non-serializable bits."""
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Append / read
# ---------------------------------------------------------------------------


def append_manifest_record(
    record: dict,
    manifest_path: Union[str, Path] = DEFAULT_MANIFEST_PATH,
) -> tuple[bool, Optional[str]]:
    """
    Thread-safely append one JSONL record. Returns ``(ok, error)``.

    Never raises. Serialization failures are reported as error codes
    so the caller can log them without catching exceptions. The
    module-level lock serializes concurrent writes from multiple
    threads in the same process; multi-process runs would need OS-
    level file locking which we deliberately do not take because
    the daily-report writer is single-process.
    """
    try:
        line = json.dumps(record, sort_keys=False, default=str)
    except Exception as exc:
        msg = f"serialize_error:{type(exc).__name__}:{exc}"
        log.warning("manifest.serialize_error", error=msg)
        return False, msg

    path = Path(manifest_path)
    with _WRITE_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            msg = f"write_error:{type(exc).__name__}:{exc}"
            log.warning("manifest.write_error", path=str(path), error=msg)
            return False, msg

    return True, None


def read_manifest(
    manifest_path: Union[str, Path] = DEFAULT_MANIFEST_PATH,
    limit: Optional[int] = None,
) -> list[dict]:
    """
    Read the manifest. Returns a list of record dicts.

    When ``limit`` is a positive int, returns the LAST N records
    (operator `--tail` semantics). Malformed / truncated JSONL lines
    are silently skipped so a partial write cannot make history
    unreadable.
    """
    path = Path(manifest_path)
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
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
    if limit is not None and limit > 0:
        records = records[-limit:]
    return records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.reporting.experiment_manifest",
        description=(
            "Read the append-only alpha experiment manifest. "
            "Use --tail N to limit to the last N entries."
        ),
    )
    parser.add_argument(
        "--manifest",
        default=DEFAULT_MANIFEST_PATH,
        help=f"Manifest JSONL path (default: {DEFAULT_MANIFEST_PATH})",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=10,
        help="Show only the last N entries (default: 10; use 0 for all)",
    )
    parser.add_argument(
        "--json-lines",
        action="store_true",
        help="Emit one compact JSON record per line instead of pretty-printed.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    tail = args.tail if args.tail and args.tail > 0 else None
    records = read_manifest(manifest_path, limit=tail)

    if not records:
        if not manifest_path.exists():
            print(
                f"(manifest not found at {manifest_path})",
                file=sys.stderr,
            )
        else:
            print(f"(no records in {manifest_path})")
        return 0

    if args.json_lines:
        for rec in records:
            print(json.dumps(rec, sort_keys=False, default=str))
    else:
        for i, rec in enumerate(records):
            if i > 0:
                print("---")
            print(json.dumps(rec, indent=2, sort_keys=False, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
