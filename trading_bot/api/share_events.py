"""
Phase 10.3 — Viral Loop Optimization.

Records one JSONL event each time a share opportunity is generated
(``share_generated``) or an inbound visit arrives carrying a
``?src=`` attribution token (``inbound_visit``). Together these
two event types let operators measure the viral loop without ever
storing personal data.

Events emitted:

  * ``share_generated``
      — a share payload was attached to a preview response (one
        row per response, never duplicated within a single request).
  * ``inbound_visit``
      — first-touch source: a request arrived with a non-empty
        ``?src=`` query parameter.

Privacy posture (identical to Phase 5.1 / 5.5 / 8.4 logs):

  * Raw API keys are **never** persisted. Callers MUST pre-hash
    via ``_hash_api_key`` (or the equivalent server / billing /
    growth helper) before calling ``record_share_event``. The
    function refuses raw keys: the parameter is named ``key_hash``,
    not ``api_key``.
  * No IP, no User-Agent, no email, no name, no payment field
    ever enters a record — only the opaque key hash, the event
    type, the HTTP path, and the (sanitised) ``src`` token when
    one is present.

Failure posture:

  * All I/O is best-effort — a disk failure never breaks the
    underlying API request. Every exception path is caught and
    logged at DEBUG.
  * Thread-safe via a module-level ``threading.Lock``.

Env vars:

    TRADING_API_SHARE_EVENTS_LOG_PATH   JSONL output path
                                        (default:
                                        data/api_share_events.jsonl)

CLI::

    python -m trading_bot.api.share_events --summary
    python -m trading_bot.api.share_events --summary --json
    python -m trading_bot.api.share_events --summary \\
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

SHARE_EVENTS_LOG_ENV_VAR = "TRADING_API_SHARE_EVENTS_LOG_PATH"
DEFAULT_SHARE_EVENTS_LOG_PATH = "data/api_share_events.jsonl"

# Event types — keep the strings exactly in sync with the
# CORE_CONTROL Phase 10.3 documentation. Anything outside this
# set is silently dropped.
EVENT_SHARE_GENERATED = "share_generated"
EVENT_INBOUND_VISIT = "inbound_visit"

VALID_SHARE_EVENTS: frozenset[str] = frozenset({
    EVENT_SHARE_GENERATED,
    EVENT_INBOUND_VISIT,
})

# Sanitiser for ``src`` — same character set + cap as the Phase
# 5.1 ``ref_code`` sanitiser, so the two attribution tokens can be
# joined cleanly across the growth and share event logs.
SRC_MAX_LENGTH = 64
_SRC_STRIP_RE = re.compile(r"[^A-Za-z0-9\-_:.]")

# Bound on the ``endpoint`` string so a malformed caller can't
# bloat the JSONL file size.
_ENDPOINT_MAX_LENGTH = 128

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _hash_api_key(api_key: Optional[str]) -> str:
    """SHA-256[:32] — identical to server/billing/growth/upgrade hashers."""
    if not api_key:
        return ""
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:32]


def _log_path() -> Path:
    return Path(
        os.getenv(SHARE_EVENTS_LOG_ENV_VAR, DEFAULT_SHARE_EVENTS_LOG_PATH)
    )


def sanitize_src(raw: Optional[str]) -> str:
    """
    Strip to the documented safe character set, cap length. Untrusted
    input — a caller can put anything in a query param, so we never
    pass the raw bytes to the JSONL file.

    Returns ``""`` for ``None``, empty input, or input that contains
    only stripped characters.
    """
    if raw is None:
        return ""
    s = str(raw)
    if not s:
        return ""
    return _SRC_STRIP_RE.sub("", s)[:SRC_MAX_LENGTH]


def _sanitize_endpoint(value: Optional[str]) -> Optional[str]:
    """Strip + cap; reject empty values."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s[:_ENDPOINT_MAX_LENGTH]


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
        log.debug("share_events.serialize_error", error=str(exc))
        return
    with _write_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            log.debug(
                "share_events.write_error",
                path=str(path),
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_share_event(
    key_hash: Optional[str],
    type: str,
    endpoint: Optional[str] = None,
    *,
    src: Optional[str] = None,
    request_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """
    Phase 10.3 — append one share / inbound-visit record.

    The recorded schema mirrors the Phase 5.5 / 8.4 base schema:

    ::

        {
          "timestamp":    "...Z",
          "api_key_hash": "<SHA-256[:32]>",
          "event":        "share_generated" | "inbound_visit",
          "endpoint":     "/reports/latest" | ...,
          "src":          "<sanitised src>" | null,
          "request_id":   "<32-hex>" | null,
        }

    The ``key_hash`` is taken AS-IS — callers MUST pre-hash; this
    function refuses raw keys (the parameter is named ``key_hash``,
    not ``api_key``). For ``inbound_visit`` events on unauthenticated
    requests the caller may pass an empty hash and the row is still
    skipped: we do not record anonymous rows because there is
    nothing to join on.

    Any I/O failure is best-effort: the caller's request must NEVER
    fail because the share-events writer hit disk trouble.

    Returns a small status dict for tests:

      - ``{"action": "recorded", "event": "...", "api_key_hash": "..."}``
      - ``{"action": "skipped", "reason": "no_key_hash"}``
      - ``{"action": "skipped", "reason": "unknown_event"}``
    """
    if type not in VALID_SHARE_EVENTS:
        return {"action": "skipped", "reason": "unknown_event"}
    if not key_hash or not isinstance(key_hash, str) or not key_hash.strip():
        return {"action": "skipped", "reason": "no_key_hash"}
    h = key_hash.strip()

    cleaned_src = sanitize_src(src)
    record = {
        "timestamp": _now_iso_utc(now),
        "api_key_hash": h,
        "event": str(type),
        "endpoint": _sanitize_endpoint(endpoint),
        "src": cleaned_src or None,
        "request_id": (
            str(request_id).strip()[:64]
            if isinstance(request_id, str) and request_id.strip()
            else None
        ),
    }
    _append_record(record)
    return {"action": "recorded", "event": type, "api_key_hash": h}


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _read_records(path: Path) -> list[dict]:
    """Load a JSONL share-events file. Missing / corrupt → []."""
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
    top_endpoints: int = 5,
    top_sources: int = 5,
) -> dict:
    """
    Aggregate a share-events JSONL file into something humans and
    dashboards can both read.

    Returns a dict with:
      * ``total_events``  — total rows read.
      * ``events``        — per-event-name stats:
          ``{event: {"count": n, "unique_users": m}}``
      * ``top_endpoints`` — top ``top_endpoints`` endpoints across
        all events.
      * ``sources``       — per-src stats when at least one event
        had a non-empty src:
          ``{src: {"count": n, "unique_users": m, "events": k}}``
        Sorted by ``count`` descending; capped at ``top_sources``.
    """
    p = Path(path) if path is not None else _log_path()
    records = _read_records(p)

    per_event_count: Counter[str] = Counter()
    per_event_users: dict[str, set[str]] = defaultdict(set)
    per_endpoint_count: Counter[str] = Counter()
    per_src_count: Counter[str] = Counter()
    per_src_users: dict[str, set[str]] = defaultdict(set)
    per_src_events: dict[str, set[str]] = defaultdict(set)

    for rec in records:
        evt = rec.get("event")
        if not isinstance(evt, str) or not evt:
            continue
        h = str(rec.get("api_key_hash") or "")
        per_event_count[evt] += 1
        if h:
            per_event_users[evt].add(h)
        ep = rec.get("endpoint")
        if isinstance(ep, str) and ep:
            per_endpoint_count[ep] += 1
        src = rec.get("src")
        if isinstance(src, str) and src:
            per_src_count[src] += 1
            if h:
                per_src_users[src].add(h)
            per_src_events[src].add(evt)

    events_section = {
        evt: {
            "count": per_event_count[evt],
            "unique_users": len(per_event_users.get(evt, set())),
        }
        for evt in sorted(per_event_count.keys())
    }

    top_endpoints_list = [
        {"endpoint": ep, "count": n}
        for ep, n in per_endpoint_count.most_common(max(0, int(top_endpoints)))
    ]

    sources_list = [
        {
            "src": src,
            "count": n,
            "unique_users": len(per_src_users.get(src, set())),
            "events": len(per_src_events.get(src, set())),
        }
        for src, n in per_src_count.most_common(max(0, int(top_sources)))
    ]

    return {
        "total_events": len(records),
        "events": events_section,
        "top_endpoints": top_endpoints_list,
        "sources": sources_list,
    }


def format_summary_text(summary: dict) -> str:
    """Render ``summarize()`` output as plain text for the CLI."""
    lines: list[str] = []
    bar = "=" * 72
    lines.append(bar)
    lines.append("SHARE EVENT SUMMARY")
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
    lines.append("Top endpoints:")
    endpoints = summary.get("top_endpoints", []) or []
    if not endpoints:
        lines.append("  (no endpoints recorded)")
    else:
        for row in endpoints:
            lines.append(
                f"  {str(row.get('endpoint', ''))[:40]:<40} "
                f"count={row.get('count', 0)}"
            )

    sources = summary.get("sources", []) or []
    lines.append("")
    lines.append("By src:")
    if not sources:
        lines.append("  (no inbound sources present)")
    else:
        for row in sources:
            lines.append(
                f"  {str(row.get('src', ''))[:30]:<30} "
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
        prog="python -m trading_bot.api.share_events",
        description=(
            "Summarise the Phase 10.3 share-events JSONL log. "
            "Offline analytics only — never hits Core or the live API."
        ),
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Print a summary of the share-events log.",
    )
    parser.add_argument(
        "--path", default=None,
        help=(
            "Path to the share-events JSONL "
            f"(default: ${SHARE_EVENTS_LOG_ENV_VAR} or "
            f"{DEFAULT_SHARE_EVENTS_LOG_PATH})."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of the plain-text summary.",
    )
    parser.add_argument(
        "--top-endpoints", type=int, default=5,
        help="Number of top endpoints to include (default: 5).",
    )
    parser.add_argument(
        "--top-sources", type=int, default=5,
        help="Number of top inbound sources to include (default: 5).",
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
        target,
        top_endpoints=args.top_endpoints,
        top_sources=args.top_sources,
    )

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=False, default=str))
    else:
        print(format_summary_text(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
