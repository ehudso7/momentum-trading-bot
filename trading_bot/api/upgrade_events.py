"""
Phase 5.5 — Upgrade CTA Telemetry.

Records one JSONL event each time a free-tier caller encounters a
conversion-relevant friction point so we can measure which
prompts/limits actually drive upgrades.

Events emitted (all only for ``tier == "free"``):

  * ``dashboard_banner_seen``
      — the Phase 5.4 yellow banner was rendered for this user
  * ``daily_request_limit_hit``
      — Phase 5.4's global request-count 429 fired
  * ``report_limit_hit``
      — Phase 5.4's ``/reports/*`` 403 fired
  * ``old_report_blocked``
      — Phase 4.5's ``MAX_FREE_TIER_DAYS`` window 403 fired on
        ``/reports/{date}``
  * ``experiment_limit_blocked``
      — Phase 4.5's ``MAX_FREE_TIER_EXPERIMENTS`` cap fired on
        ``/experiments/*``

Privacy posture (identical to Phase 5.1 and Phase 4.9 logs):

  * Raw API keys are **never** persisted. Only
    ``SHA-256(api_key)[:32]``, which matches the
    ``_hash_api_key`` digest used by server / billing /
    conversion / growth so BI pipelines can join all five logs on
    one column.
  * No IP, no User-Agent, no email, no name, no payment field
    ever enters a record.
  * Unknown / unsupported event names are dropped silently.

Failure posture:

  * All I/O is best-effort — a disk failure never breaks the
    underlying API request. Every exception path is caught and
    logged at DEBUG.
  * Thread-safe via a module-level ``threading.Lock``.

Env vars:

    TRADING_API_UPGRADE_EVENTS_LOG_PATH   JSONL output path
                                          (default:
                                          data/api_upgrade_events.jsonl)

CLI::

    python -m trading_bot.api.upgrade_events --summary
    python -m trading_bot.api.upgrade_events --summary --json
    python -m trading_bot.api.upgrade_events --summary \\
        --path path/to/events.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UPGRADE_EVENTS_LOG_ENV_VAR = "TRADING_API_UPGRADE_EVENTS_LOG_PATH"
DEFAULT_UPGRADE_EVENTS_LOG_PATH = "data/api_upgrade_events.jsonl"

# Event types — keep the strings exactly in sync with the CORE_CONTROL
# documentation. Anything outside this set is silently dropped.
EVENT_DASHBOARD_BANNER_SEEN = "dashboard_banner_seen"
EVENT_DAILY_REQUEST_LIMIT_HIT = "daily_request_limit_hit"
EVENT_REPORT_LIMIT_HIT = "report_limit_hit"
EVENT_OLD_REPORT_BLOCKED = "old_report_blocked"
EVENT_EXPERIMENT_LIMIT_BLOCKED = "experiment_limit_blocked"

# Phase 8.4 — three-stage upgrade-pressure funnel:
#   shown      → upgrade payload was attached to a response
#   clicked    → caller hit POST /billing/checkout
#   completed  → Stripe webhook flipped the key to premium
EVENT_UPGRADE_SHOWN = "upgrade_shown"
EVENT_UPGRADE_CLICKED = "upgrade_clicked"
EVENT_UPGRADE_COMPLETED = "upgrade_completed"

VALID_EVENTS: frozenset[str] = frozenset({
    EVENT_DASHBOARD_BANNER_SEEN,
    EVENT_DAILY_REQUEST_LIMIT_HIT,
    EVENT_REPORT_LIMIT_HIT,
    EVENT_OLD_REPORT_BLOCKED,
    EVENT_EXPERIMENT_LIMIT_BLOCKED,
    EVENT_UPGRADE_SHOWN,
    EVENT_UPGRADE_CLICKED,
    EVENT_UPGRADE_COMPLETED,
})

# Phase 8.4 — events that travel with the new ``reason`` /
# ``endpoint`` shape rather than the legacy
# ``copy_variant`` / ``ref_code`` shape. Both shapes share the
# same JSONL file; readers branch on ``event``.
_PHASE_84_FUNNEL_EVENTS: frozenset[str] = frozenset({
    EVENT_UPGRADE_SHOWN,
    EVENT_UPGRADE_CLICKED,
    EVENT_UPGRADE_COMPLETED,
})

# The recorded tier is always "free" — premium users never reach an
# emission site (the middleware/gate short-circuits earlier).
TIER_FREE = "free"

# Sanitiser for ``ref_code`` — same character set and cap as the
# Phase 5.1 growth logger so the two files can be joined cleanly
# on ``(api_key_hash, ref_code)``.
REF_CODE_MAX_LENGTH = 64
_REF_CODE_STRIP_RE = re.compile(r"[^A-Za-z0-9\-_:.]")

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _hash_api_key(api_key: Optional[str]) -> str:
    """SHA-256[:32] — identical to server/billing/conversion/growth."""
    if not api_key:
        return ""
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:32]


def _hash_copy_variant(copy_variant: Optional[str]) -> Optional[str]:
    """
    Phase 5.8 — derive ``SHA-256(copy)[:32]`` from a resolved nudge
    copy string so the upgrade-events log can group rows by which
    variant the operator was running, without ever persisting the
    raw copy text.

    ``None`` / empty input → ``None`` (the field is optional and
    is recorded as ``null`` in the JSONL row).
    """
    if copy_variant is None:
        return None
    s = str(copy_variant)
    if not s:
        return None
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


def _log_path() -> Path:
    return Path(
        os.getenv(UPGRADE_EVENTS_LOG_ENV_VAR, DEFAULT_UPGRADE_EVENTS_LOG_PATH)
    )


def _sanitize_ref_code(raw: Optional[str]) -> str:
    """
    Strip to the documented safe character set, cap length. A
    caller can put anything in a query param — we never trust it.
    """
    if raw is None:
        return ""
    s = str(raw)
    if not s:
        return ""
    return _REF_CODE_STRIP_RE.sub("", s)[:REF_CODE_MAX_LENGTH]


def _now_iso_utc(now: Optional[datetime] = None) -> str:
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _append_record(record: dict, target: Optional[Path] = None) -> None:
    """Thread-safe JSONL append. Never raises."""
    path = target if target is not None else _log_path()
    try:
        line = json.dumps(record, sort_keys=False, default=str)
    except Exception as exc:
        log.debug("upgrade_events.serialize_error", error=str(exc))
        return
    with _write_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            log.debug(
                "upgrade_events.write_error",
                path=str(path),
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_upgrade_event(
    api_key: Optional[str],
    event: str,
    *,
    path: Optional[str] = None,
    request_id: Optional[str] = None,
    ref_code: Optional[str] = None,
    copy_variant: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """
    Append one upgrade-telemetry record. Best-effort.

    Phase 5.8: callers of the three "carries-copy" events
    (``dashboard_banner_seen``, ``daily_request_limit_hit``,
    ``report_limit_hit``) may pass the **resolved** Phase 5.7
    nudge copy as ``copy_variant``. The function hashes it to
    ``SHA-256(copy)[:32]`` and stores the hash on the record.
    The raw copy text is **never** persisted.

    Returns a small status dict for tests:

      - ``{"action": "recorded", "event": "...", "api_key_hash": "..."}``
      - ``{"action": "skipped", "reason": "no_api_key"}``
      - ``{"action": "skipped", "reason": "unknown_event"}``

    Any I/O failure still returns ``{"action": "recorded", ...}`` —
    the caller's only guarantee is that this function will not
    raise and will not block. Failures are observable via
    structlog at DEBUG.
    """
    if event not in VALID_EVENTS:
        return {"action": "skipped", "reason": "unknown_event"}

    hashed = _hash_api_key(api_key)
    if not hashed:
        return {"action": "skipped", "reason": "no_api_key"}

    cleaned_ref = _sanitize_ref_code(ref_code)
    variant_hash = _hash_copy_variant(copy_variant)
    record = {
        "timestamp": _now_iso_utc(now),
        "api_key_hash": hashed,
        "event": str(event),
        "path": str(path) if path else "",
        "request_id": str(request_id) if request_id else None,
        "tier": TIER_FREE,
        "ref_code": cleaned_ref or None,
        "copy_variant_hash": variant_hash,
    }
    _append_record(record)
    return {"action": "recorded", "event": event, "api_key_hash": hashed}


# ---------------------------------------------------------------------------
# Phase 8.4 — three-stage upgrade funnel
# ---------------------------------------------------------------------------

# Bound on the operator-supplied ``reason`` / ``endpoint`` strings so
# a malformed caller can't blow up the JSONL file size.
_PHASE_84_REASON_MAX_LENGTH = 64
_PHASE_84_ENDPOINT_MAX_LENGTH = 128


def _sanitize_short_str(value: Optional[str], cap: int) -> Optional[str]:
    """Strip + cap; reject empties. Returns None on bad input so the
    caller can omit the field cleanly."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s[:cap]


def record_upgrade_funnel_event(
    key_hash: Optional[str],
    event: str,
    *,
    reason: Optional[str] = None,
    endpoint: Optional[str] = None,
    request_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """
    Phase 8.4 — append one upgrade-funnel record.

    Three valid event values track the conversion funnel:

      * ``upgrade_shown``     — a Phase 8.3 upgrade payload was
        attached to a response. Fired from inside
        ``server._build_upgrade_payload`` after a successful
        Stripe Checkout creation.
      * ``upgrade_clicked``   — caller hit ``POST /billing/checkout``.
        Fired from the route handler after the Stripe Session is
        successfully created.
      * ``upgrade_completed`` — Stripe webhook flipped the key to
        premium. Fired from ``billing.handle_webhook_event`` after
        the premium-cache mutation lands.

    The record schema mirrors the Phase 5.5 base schema and adds
    two operator-facing columns:

    ::

        {
          "timestamp":     "...Z",
          "api_key_hash":  "<SHA-256[:32]>",
          "event":         "upgrade_shown" | "upgrade_clicked" | "upgrade_completed",
          "tier":          "free",
          "request_id":    "<32-hex>" | null,
          "reason":        "usage_limit" | "feature_locked" | "limited_access" | ...,
          "endpoint":      "/reports/latest" | "/billing/checkout" | "/webhook/stripe" | ...,
        }

    The ``api_key_hash`` is taken AS-IS — callers MUST pre-hash;
    this function refuses raw keys (signature documents
    ``key_hash``, not ``api_key``). Any I/O failure is best-effort:
    the caller's request must NEVER fail because the funnel writer
    hit disk trouble.

    Returns a small status dict for tests:

      - ``{"action": "recorded", "event": "...", "api_key_hash": "..."}``
      - ``{"action": "skipped", "reason": "no_key_hash"}``
      - ``{"action": "skipped", "reason": "unknown_event"}``
    """
    if event not in _PHASE_84_FUNNEL_EVENTS:
        return {"action": "skipped", "reason": "unknown_event"}
    if not key_hash or not isinstance(key_hash, str) or not key_hash.strip():
        return {"action": "skipped", "reason": "no_key_hash"}
    h = key_hash.strip()

    record = {
        "timestamp": _now_iso_utc(now),
        "api_key_hash": h,
        "event": str(event),
        "tier": TIER_FREE,
        "request_id": (
            _sanitize_short_str(request_id, _PHASE_84_REASON_MAX_LENGTH)
        ),
        "reason": _sanitize_short_str(reason, _PHASE_84_REASON_MAX_LENGTH),
        "endpoint": _sanitize_short_str(
            endpoint, _PHASE_84_ENDPOINT_MAX_LENGTH,
        ),
    }
    _append_record(record)
    return {"action": "recorded", "event": event, "api_key_hash": h}


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _read_records(path: Path) -> list[dict]:
    """Load a JSONL upgrade-events file. Missing / corrupt → []."""
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except Exception:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def summarize(
    path: Union[str, Path, None] = None,
    *,
    top_paths: int = 5,
    top_refs: int = 5,
) -> dict:
    """
    Aggregate an upgrade-events JSONL file into something humans
    and dashboards can both read.

    Returns a dict with:
      * ``total_events`` — total rows read.
      * ``events`` — per-event-name stats:
          ``{event: {"count": n, "unique_users": m}}``
      * ``top_paths`` — top ``top_paths`` paths across all events
        (each entry: ``{"path": "...", "count": n}``).
      * ``ref_codes`` — per-ref_code stats when at least one event
        had a non-empty ref_code:
          ``{ref: {"count": n, "unique_users": m, "events": k}}``
        Sorted by ``count`` descending; capped at ``top_refs``.
    """
    p = Path(path) if path is not None else _log_path()
    records = _read_records(p)

    per_event_count: Counter[str] = Counter()
    per_event_users: dict[str, set[str]] = defaultdict(set)
    per_path_count: Counter[str] = Counter()
    per_ref_count: Counter[str] = Counter()
    per_ref_users: dict[str, set[str]] = defaultdict(set)
    per_ref_events: dict[str, set[str]] = defaultdict(set)

    for rec in records:
        evt = rec.get("event")
        if not isinstance(evt, str) or not evt:
            continue
        h = str(rec.get("api_key_hash") or "")
        per_event_count[evt] += 1
        if h:
            per_event_users[evt].add(h)
        pth = rec.get("path")
        if isinstance(pth, str) and pth:
            per_path_count[pth] += 1
        ref = rec.get("ref_code")
        if isinstance(ref, str) and ref:
            per_ref_count[ref] += 1
            if h:
                per_ref_users[ref].add(h)
            per_ref_events[ref].add(evt)

    events_section = {
        evt: {
            "count": per_event_count[evt],
            "unique_users": len(per_event_users.get(evt, set())),
        }
        for evt in sorted(per_event_count.keys())
    }

    top_paths_list = [
        {"path": pth, "count": n}
        for pth, n in per_path_count.most_common(max(0, int(top_paths)))
    ]

    ref_codes_list = [
        {
            "ref_code": ref,
            "count": n,
            "unique_users": len(per_ref_users.get(ref, set())),
            "events": len(per_ref_events.get(ref, set())),
        }
        for ref, n in per_ref_count.most_common(max(0, int(top_refs)))
    ]

    return {
        "total_events": len(records),
        "events": events_section,
        "top_paths": top_paths_list,
        "ref_codes": ref_codes_list,
    }


def format_summary_text(summary: dict) -> str:
    """Render ``summarize()`` output as plain text for the CLI."""
    lines: list[str] = []
    bar = "=" * 72
    lines.append(bar)
    lines.append("UPGRADE EVENT SUMMARY")
    lines.append(bar)
    lines.append(f"total_events : {summary.get('total_events', 0)}")
    lines.append("")

    events = summary.get("events", {}) or {}
    lines.append("By event:")
    if not events:
        lines.append("  (no events recorded)")
    else:
        name_col = max((len(e) for e in events), default=10)
        for name, stats in events.items():
            lines.append(
                f"  {name:<{name_col}} "
                f"count={stats.get('count', 0):<5} "
                f"unique_users={stats.get('unique_users', 0)}"
            )

    lines.append("")
    lines.append("Top paths:")
    paths = summary.get("top_paths", []) or []
    if not paths:
        lines.append("  (no paths recorded)")
    else:
        for row in paths:
            lines.append(
                f"  {str(row.get('path', ''))[:40]:<40} "
                f"count={row.get('count', 0)}"
            )

    refs = summary.get("ref_codes", []) or []
    lines.append("")
    lines.append("By ref_code:")
    if not refs:
        lines.append("  (no ref_codes present)")
    else:
        for row in refs:
            lines.append(
                f"  {str(row.get('ref_code', ''))[:30]:<30} "
                f"count={row.get('count', 0):<5} "
                f"unique_users={row.get('unique_users', 0):<5} "
                f"events={row.get('events', 0)}"
            )

    lines.append(bar)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.upgrade_events",
        description=(
            "Summarise the Phase 5.5 upgrade-telemetry JSONL log. "
            "Offline analytics only — never hits Core or the live API."
        ),
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Print a summary of the upgrade-events log.",
    )
    parser.add_argument(
        "--path", default=None,
        help=(
            "Path to the upgrade-events JSONL "
            f"(default: ${UPGRADE_EVENTS_LOG_ENV_VAR} or "
            f"{DEFAULT_UPGRADE_EVENTS_LOG_PATH})."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of the plain-text summary.",
    )
    parser.add_argument(
        "--top-paths", type=int, default=5,
        help="Number of top paths to include (default: 5).",
    )
    parser.add_argument(
        "--top-refs", type=int, default=5,
        help="Number of top ref_codes to include (default: 5).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.summary:
        parser.print_help()
        return 2

    target = Path(args.path) if args.path else _log_path()
    summary = summarize(
        target, top_paths=args.top_paths, top_refs=args.top_refs,
    )

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=False, default=str))
    else:
        print(format_summary_text(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
