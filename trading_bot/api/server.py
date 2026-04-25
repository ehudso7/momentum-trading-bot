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

import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import date as _date_type, datetime, timedelta, timezone
from html import escape as _esc
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from trading_bot.api import key_store
from trading_bot.api.billing import (
    BillingAPIError,
    BillingConfigError,
    STRIPE_WEBHOOK_SECRET_ENV_VAR,
    create_checkout_session_for_hash,
    handle_webhook_event,
    is_premium_via_stripe,
    is_stripe_configured,
    verify_webhook_signature,
)
from trading_bot.api.insights import build_insights, truncate_for_free

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Runtime config (env-driven, evaluated per request)
# ---------------------------------------------------------------------------

API_KEY_ENV_VAR = "TRADING_API_KEY"
REPORTS_DIR_ENV_VAR = "TRADING_API_REPORTS_DIR"
MANIFEST_PATH_ENV_VAR = "TRADING_API_MANIFEST_PATH"

# Phase 4.2 — deployment hardening.
ALLOWED_ORIGINS_ENV_VAR = "TRADING_API_ALLOWED_ORIGINS"
RATE_LIMIT_ENV_VAR = "TRADING_API_RATE_LIMIT_PER_MINUTE"
DEFAULT_RATE_LIMIT_PER_MINUTE = 60

# Phase 4.5 — access tiers (free vs premium).
PREMIUM_KEYS_ENV_VAR = "TRADING_API_PREMIUM_KEYS"
TIER_FREE = "free"
TIER_PREMIUM = "premium"
MAX_FREE_TIER_DAYS = 3            # /reports/{date} window for free tier
MAX_FREE_TIER_EXPERIMENTS = 3     # /experiments/* cap for free tier
UPGRADE_REQUIRED_DETAIL = "upgrade required for full access"

# Phase 5.4 — free-tier daily usage caps (reversible via env vars).
# Both caps are hard ceilings on a per-key / per-UTC-day basis. They
# apply ONLY to free-tier callers — premium users are exempt, as
# are public paths (/ , /health) and the Stripe webhook.
FREE_MAX_REQUESTS_ENV_VAR = "TRADING_FREE_MAX_REQUESTS_PER_DAY"
FREE_MAX_REPORT_CALLS_ENV_VAR = "TRADING_FREE_MAX_REPORT_CALLS"
DEFAULT_FREE_MAX_REQUESTS_PER_DAY = 50
DEFAULT_FREE_MAX_REPORT_CALLS = 10
FREE_TIER_USAGE_HEADER = "X-Free-Tier-Usage"
FREE_TIER_REMAINING_HEADER = "X-Free-Tier-Remaining"
FREE_TIER_LIMIT_DETAIL = (
    "free tier limit reached — upgrade for continued access"
)
# Paths that count as "report calls" for the stricter cap.
_FREE_TIER_REPORT_PATH_PREFIX = "/reports/"
# Paths that Phase 5.4 does NOT count toward the daily cap and
# must NEVER emit a 429/403 from this middleware. Phase 7.1 adds
# the three browser icon routes — they are public by browser
# convention and must not count as paid surface area.
_FREE_TIER_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/", "/health", "/webhook/stripe",
        "/favicon.ico",
        "/apple-touch-icon.png",
        "/apple-touch-icon-precomposed.png",
    }
)

# Phase 8.1 — tier-aware daily usage enforcement.
#
# Phase 5.4 caps free-tier callers only and uses /reports/* as a
# tighter sub-cap. Phase 8.1 layers a UNIFIED tier-aware daily
# request cap on top: free callers get TRADING_FREE_DAILY_REQUEST_LIMIT,
# premium callers get TRADING_PREMIUM_DAILY_REQUEST_LIMIT. Both
# caps read the same usage JSONL log (Phase 4.6 schema unchanged).
#
# Registered LAST so it sits OUTERMOST in the middleware stack and
# fires FIRST on every request — its 429 short-circuits the older
# Phase 5.4 layer when both would otherwise reject the same call.
USAGE_ENFORCEMENT_ENABLED_ENV_VAR = "TRADING_USAGE_ENFORCEMENT_ENABLED"
FREE_DAILY_REQUEST_LIMIT_ENV_VAR = "TRADING_FREE_DAILY_REQUEST_LIMIT"
PREMIUM_DAILY_REQUEST_LIMIT_ENV_VAR = "TRADING_PREMIUM_DAILY_REQUEST_LIMIT"
USAGE_LIMIT_EXEMPT_PATHS_ENV_VAR = "TRADING_USAGE_LIMIT_EXEMPT_PATHS"
DEFAULT_FREE_DAILY_REQUEST_LIMIT = 50
DEFAULT_PREMIUM_DAILY_REQUEST_LIMIT = 1000
USAGE_LIMIT_HEADER = "X-Usage-Limit"
USAGE_REMAINING_HEADER = "X-Usage-Remaining"
USAGE_TIER_HEADER = "X-Usage-Tier"
RETRY_AFTER_HEADER = "Retry-After"
USAGE_LIMIT_DETAIL = "usage limit reached — upgrade for higher limits"
# Default exempt set for Phase 8.1 — same as Phase 5.4's. Operators
# can extend via TRADING_USAGE_LIMIT_EXEMPT_PATHS=,/foo,/bar.
_PHASE_81_DEFAULT_EXEMPT_PATHS: frozenset[str] = _FREE_TIER_EXEMPT_PATHS
# Truthy-ish strings for the toggle. Anything else (including unset,
# empty, "false", "0", "no") disables enforcement.
_TRUTHY_STRINGS: frozenset[str] = frozenset(
    {"1", "true", "yes", "on", "y", "t"},
)

# Phase 5.7 — dynamic free-tier nudge copy (reversible via env vars).
# Operators can A/B different upgrade messages without redeploying.
# Each env var overrides the corresponding default; an unset, blank,
# or unsafe value falls back to the default fail-closed.
UPGRADE_BANNER_COPY_ENV_VAR = "TRADING_UPGRADE_BANNER_COPY"
LIMIT_HIT_COPY_ENV_VAR = "TRADING_LIMIT_HIT_COPY"
REPORT_LIMIT_COPY_ENV_VAR = "TRADING_REPORT_LIMIT_COPY"
DEFAULT_UPGRADE_BANNER_COPY = (
    "You're using the free tier — upgrade for full access"
)
# The two API-detail defaults are aliases of the existing constants —
# kept as separate names so the Phase 5.7 surface is greppable.
DEFAULT_LIMIT_HIT_COPY = FREE_TIER_LIMIT_DETAIL
DEFAULT_REPORT_LIMIT_COPY = UPGRADE_REQUIRED_DETAIL
MAX_NUDGE_COPY_LENGTH = 180
# Reject any value that contains an ASCII control character other
# than space (0x20+). NUL through 0x1F minus tab/LF/CR are unsafe;
# DEL (0x7F) is also stripped. We could allow tab/LF/CR but a copy
# string with embedded newlines breaks JSON pretty-print and HTML
# block layout, so we treat them as unsafe too.
_NUDGE_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

DEFAULT_REPORTS_DIR = "reports"
DEFAULT_MANIFEST_PATH = "data/alpha_experiments.jsonl"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Fixed security headers. CSP allows only same-origin resources + inline
# style (the dashboard ships a single <style> block). No scripts permitted.
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'none'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'"
    ),
}

# In-memory fixed-window rate-limit bucket: client_ip -> (count, window_start).
# Thread-safe access is gated by `_rate_limit_lock`. Module-level state is
# appropriate here — a single-process API server is the deployment target.
_rate_limit_bucket: dict[str, tuple[int, float]] = {}
_rate_limit_lock = threading.Lock()

# Phase 4.4 — access audit trail.
AUDIT_LOG_ENV_VAR = "TRADING_API_AUDIT_LOG_PATH"
DEFAULT_AUDIT_LOG_PATH = "data/api_access_audit.jsonl"
REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_MAX_LENGTH = 64
# Only ASCII alnum, `-`, `_`, `:`, `.` survive sanitization. Anything else
# (including whitespace, control chars, unicode, HTML, newlines) is stripped.
_REQUEST_ID_STRIP_RE = re.compile(r"[^A-Za-z0-9\-_:.]")
# Statuses that mean "auth layer rejected the request". Everything else
# (200, 404, 429, etc.) counts as `authenticated: true` — the client was
# granted access, even if the resource itself did not exist.
_UNAUTHENTICATED_STATUSES: frozenset[int] = frozenset({401, 403, 503})

_audit_write_lock = threading.Lock()

# Phase 4.6 — per-key usage metrics.
USAGE_LOG_ENV_VAR = "TRADING_API_USAGE_LOG_PATH"
DEFAULT_USAGE_LOG_PATH = "data/api_usage.jsonl"
# Paths that are NEVER counted in the usage log. Everything else
# is considered a "protected" request for billing/adoption purposes.
_PUBLIC_PATHS_NO_USAGE: frozenset[str] = frozenset(
    {
        "/", "/health",
        # Phase 7.1 — browser icon routes are auto-requested by the
        # browser on every page view and never carry an Authorization
        # header. Excluding them keeps the per-key usage log clean.
        "/favicon.ico",
        "/apple-touch-icon.png",
        "/apple-touch-icon-precomposed.png",
    }
)

_usage_write_lock = threading.Lock()


def _reports_dir() -> Path:
    return Path(os.getenv(REPORTS_DIR_ENV_VAR, DEFAULT_REPORTS_DIR))


def _manifest_path() -> Path:
    return Path(os.getenv(MANIFEST_PATH_ENV_VAR, DEFAULT_MANIFEST_PATH))


# ---------------------------------------------------------------------------
# Phase 4.2 — env helpers for deployment hardening
# ---------------------------------------------------------------------------


def _rate_limit_per_minute() -> int:
    """
    Resolve the rate-limit env var. Any invalid value (non-int,
    negative, zero, empty, garbage) falls back to the documented
    default — a typo must never open the server to unlimited traffic.
    """
    raw = os.getenv(RATE_LIMIT_ENV_VAR, "")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_RATE_LIMIT_PER_MINUTE
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_RATE_LIMIT_PER_MINUTE
    if n <= 0:
        return DEFAULT_RATE_LIMIT_PER_MINUTE
    return n


def _allowed_origins() -> list[str]:
    """Parse the CORS allow-list env var. Empty/unset → no CORS."""
    raw = os.getenv(ALLOWED_ORIGINS_ENV_VAR, "")
    if not raw or not raw.strip():
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]


def _parse_positive_int_env(env_var: str, default: int) -> int:
    """
    Resolve a positive-integer env var fail-closed.

    Any invalid value (non-int, zero, negative, empty, garbage)
    falls back to ``default`` — a typo in a limit env var must
    never disable the limit entirely.
    """
    raw = os.getenv(env_var)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return n


def _free_max_requests_per_day() -> int:
    return _parse_positive_int_env(
        FREE_MAX_REQUESTS_ENV_VAR, DEFAULT_FREE_MAX_REQUESTS_PER_DAY,
    )


def _free_max_report_calls() -> int:
    return _parse_positive_int_env(
        FREE_MAX_REPORT_CALLS_ENV_VAR, DEFAULT_FREE_MAX_REPORT_CALLS,
    )


# ---------------------------------------------------------------------------
# Phase 8.1 — tier-aware usage enforcement helpers
# ---------------------------------------------------------------------------


def _usage_enforcement_enabled() -> bool:
    """
    Phase 8.1 — read the feature toggle. Default ON.

    Anything in ``_TRUTHY_STRINGS`` enables; everything else
    (including unset / blank / "false" / "0") disables. The default
    when the env var is not set is ON, matching the spec.
    """
    raw = os.getenv(USAGE_ENFORCEMENT_ENABLED_ENV_VAR)
    if raw is None:
        return True
    s = str(raw).strip().lower()
    if not s:
        # Explicitly-set blank disables, matching every other
        # "blank == default-off" toggle in the codebase.
        return False
    return s in _TRUTHY_STRINGS


def _free_daily_request_limit() -> int:
    """Phase 8.1 — tier-aware free cap (default 50)."""
    return _parse_positive_int_env(
        FREE_DAILY_REQUEST_LIMIT_ENV_VAR, DEFAULT_FREE_DAILY_REQUEST_LIMIT,
    )


def _premium_daily_request_limit() -> int:
    """Phase 8.1 — tier-aware premium cap (default 1000)."""
    return _parse_positive_int_env(
        PREMIUM_DAILY_REQUEST_LIMIT_ENV_VAR,
        DEFAULT_PREMIUM_DAILY_REQUEST_LIMIT,
    )


def _usage_limit_exempt_paths() -> frozenset[str]:
    """
    Phase 8.1 — union of the documented default exempt set with any
    operator-supplied additions via TRADING_USAGE_LIMIT_EXEMPT_PATHS
    (comma-separated). Blank entries are ignored. Paths without a
    leading slash are normalised to one.
    """
    raw = (os.getenv(USAGE_LIMIT_EXEMPT_PATHS_ENV_VAR, "") or "").strip()
    if not raw:
        return _PHASE_81_DEFAULT_EXEMPT_PATHS
    extra: set[str] = set()
    for piece in raw.split(","):
        p = piece.strip()
        if not p:
            continue
        if not p.startswith("/"):
            p = "/" + p
        extra.add(p)
    return _PHASE_81_DEFAULT_EXEMPT_PATHS | frozenset(extra)


def _resolve_nudge_copy(env_var: str, default: str) -> str:
    """
    Phase 5.7 — fail-closed string-env resolver for upgrade nudges.

    Rules (every failure mode → return ``default``):
      * env var unset → default.
      * value is empty / whitespace-only → default.
      * value contains an ASCII control character (NUL..0x1F or
        0x7F, including newlines / tabs) → default. Such characters
        break JSON pretty-print, HTML layout, and a few terminal
        loggers, so we refuse to pass them through.

    Otherwise the value is stripped and truncated to
    ``MAX_NUDGE_COPY_LENGTH`` characters.

    The returned string is *raw* — callers MUST run it through
    ``html.escape`` before injecting into HTML, and MUST place it
    in a JSON-encoded body for API responses (FastAPI's
    JSONResponse already handles this).
    """
    raw = os.getenv(env_var)
    if raw is None:
        return default
    s = str(raw).strip()
    if not s:
        return default
    if _NUDGE_CONTROL_CHARS_RE.search(s):
        return default
    if len(s) > MAX_NUDGE_COPY_LENGTH:
        s = s[:MAX_NUDGE_COPY_LENGTH]
    return s


def _upgrade_banner_copy() -> str:
    return _resolve_nudge_copy(
        UPGRADE_BANNER_COPY_ENV_VAR, DEFAULT_UPGRADE_BANNER_COPY,
    )


def _limit_hit_copy() -> str:
    return _resolve_nudge_copy(
        LIMIT_HIT_COPY_ENV_VAR, DEFAULT_LIMIT_HIT_COPY,
    )


def _report_limit_copy() -> str:
    return _resolve_nudge_copy(
        REPORT_LIMIT_COPY_ENV_VAR, DEFAULT_REPORT_LIMIT_COPY,
    )


def _client_ip(request: Request) -> str:
    """
    Prefer `X-Forwarded-For` when behind a reverse proxy. Falls back
    to the socket peer. Returns "unknown" if neither is available.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        # Use the first entry — the original client.
        first = xff.split(",")[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _reset_rate_limit_bucket() -> None:
    """Test helper — drops all in-memory counters. Not routed."""
    with _rate_limit_lock:
        _rate_limit_bucket.clear()


# ---------------------------------------------------------------------------
# Phase 4.4 — access audit trail helpers
# ---------------------------------------------------------------------------


def _audit_log_path() -> Path:
    return Path(os.getenv(AUDIT_LOG_ENV_VAR, DEFAULT_AUDIT_LOG_PATH))


def _sanitize_request_id(raw: Optional[str]) -> str:
    """
    Strip to a safe character set, cap length, fall back to uuid4.

    Accepts an arbitrary caller-provided ``X-Request-ID`` header —
    which means the raw value is untrusted and could contain
    anything, including HTML, newlines, or control characters.
    Only characters in ``[A-Za-z0-9\\-_:.]`` survive. An empty
    result after sanitization triggers uuid4 generation so every
    request always has a stable id.
    """
    if raw:
        cleaned = _REQUEST_ID_STRIP_RE.sub("", raw)[:REQUEST_ID_MAX_LENGTH]
        if cleaned:
            return cleaned
    return uuid.uuid4().hex


def _hash_user_agent(ua: Optional[str]) -> Optional[str]:
    """Return a short SHA-256 hash of the raw User-Agent, or None."""
    if not ua:
        return None
    digest = hashlib.sha256(ua.encode("utf-8", errors="replace")).hexdigest()
    return digest[:32]


def _append_audit_record(record: dict, path: Optional[Path] = None) -> None:
    """
    Thread-safely append a single JSONL record to the audit log.

    Best-effort: every failure path is caught + logged at DEBUG.
    Never raises — a disk outage must not fail a live API request.
    """
    target = path if path is not None else _audit_log_path()
    try:
        line = json.dumps(record, sort_keys=False, default=str)
    except Exception as exc:
        log.debug("audit.serialize_error", error=str(exc))
        return
    with _audit_write_lock:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            log.debug(
                "audit.write_error", path=str(target), error=str(exc)
            )


def _is_authenticated_status(status_code: int) -> bool:
    """Whether a response status represents an auth-layer acceptance."""
    return int(status_code) not in _UNAUTHENTICATED_STATUSES


# ---------------------------------------------------------------------------
# Phase 4.6 — per-key usage metrics
# ---------------------------------------------------------------------------


def _usage_log_path() -> Path:
    return Path(os.getenv(USAGE_LOG_ENV_VAR, DEFAULT_USAGE_LOG_PATH))


def _hash_api_key(api_key: Optional[str]) -> str:
    """
    Anonymize an API key for usage metrics. Returns the first 32
    hex characters of ``SHA-256(api_key)``. Empty/None input → "".

    Deterministic: the same key always maps to the same hash, so
    usage records cluster by caller without ever storing the raw
    token. Not reversible and deliberately not a full-length hash
    — 128 bits of the SHA-256 digest is plenty for grouping.
    """
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


def _append_usage_record(record: dict, path: Optional[Path] = None) -> None:
    """
    Thread-safely append a single JSONL usage record.

    Best-effort: every failure path is caught + logged at DEBUG.
    Never raises — a disk outage must not fail a live API request.
    """
    target = path if path is not None else _usage_log_path()
    try:
        line = json.dumps(record, sort_keys=False, default=str)
    except Exception as exc:
        log.debug("usage.serialize_error", error=str(exc))
        return
    with _usage_write_lock:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            log.debug(
                "usage.write_error", path=str(target), error=str(exc)
            )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_security = HTTPBearer(auto_error=False)


def _premium_keys_set() -> set[str]:
    """Parse the premium-keys env var into a set. Empty/unset → empty set."""
    raw = os.getenv(PREMIUM_KEYS_ENV_VAR, "") or ""
    if not raw.strip():
        return set()
    return {k.strip() for k in raw.split(",") if k.strip()}


def _is_premium(
    api_key: Optional[str],
    request: Optional[Request] = None,
) -> bool:
    """
    True iff the supplied key is premium.

    Precedence (Phase 6.2):
      1. If a request is supplied AND ``require_api_key`` already
         resolved the tier, honour the cached value on
         ``request.state.api_key_tier``. This avoids re-hashing /
         re-reading the manifest for every helper that asks.
      2. If Stripe is configured AND the cache shows this api_key
         as having an active subscription → premium.
      3. If the key is in ``TRADING_API_PREMIUM_KEYS`` → premium
         (operator override; works even when Stripe is configured).
      4. If the manifest entry for this key has tier="premium" AND
         the key has not been revoked → premium.
      5. Otherwise → free / not premium.

    A revoked key is never premium (the manifest path checks revocation).
    Empty / None input → False.
    """
    if not api_key:
        return False
    if request is not None:
        cached = getattr(
            getattr(request, "state", None), "api_key_tier", None,
        )
        if cached == TIER_PREMIUM:
            return True
        if cached == TIER_FREE:
            return False
    if is_stripe_configured() and is_premium_via_stripe(api_key):
        return True
    if api_key in _premium_keys_set():
        return True
    entry = key_store.verify_api_key(api_key)
    if entry is not None and entry.tier == TIER_PREMIUM:
        return True
    return False


def _is_known_key(api_key: Optional[str]) -> bool:
    """
    True iff ``api_key`` would be accepted by ``require_api_key``
    (free or premium, env-backed or manifest-backed). Used by the
    free-tier middleware to decide whether to enforce or fall
    through; the actual authentication still happens at the route
    dependency.

    A revoked manifest key is NOT known.
    """
    if not api_key:
        return False
    configured = (os.getenv(API_KEY_ENV_VAR, "") or "").strip()
    if configured and api_key == configured:
        return True
    if api_key in _premium_keys_set():
        return True
    if is_stripe_configured() and is_premium_via_stripe(api_key):
        return True
    if key_store.verify_api_key(api_key) is not None:
        return True
    return False


def _extract_bearer_token(request: Request) -> Optional[str]:
    """
    Pull the bearer token out of the ``Authorization`` header
    without logging it and without touching the FastAPI dependency
    system. Returns ``None`` if the header is absent or malformed.
    """
    auth = request.headers.get("authorization") or ""
    parts = auth.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def _emit_upgrade_event(
    request: Optional[Request],
    api_key: Optional[str],
    event: str,
    *,
    copy_variant: Optional[str] = None,
) -> None:
    """
    Phase 5.5 + 5.8 — best-effort upgrade-telemetry emission.

    Extracts the request_id (set by audit_middleware) and the
    ``?ref=`` query parameter from ``request`` and forwards them to
    ``trading_bot.api.upgrade_events.record_upgrade_event``.

    Phase 5.8: if ``copy_variant`` is supplied (the resolved Phase
    5.7 nudge copy that was actually rendered to the user), it is
    hashed inside the events module and stored as
    ``copy_variant_hash`` on the row. The raw copy is never
    persisted.

    Every failure path is caught and logged at DEBUG. Telemetry
    must NEVER affect the outgoing response or the caller's latency.
    """
    try:
        path_value: Optional[str] = None
        request_id: Optional[str] = None
        ref_code: Optional[str] = None
        if request is not None:
            try:
                path_value = request.url.path
            except Exception:
                path_value = None
            try:
                request_id = getattr(
                    getattr(request, "state", None), "request_id", None,
                )
            except Exception:
                request_id = None
            try:
                raw_ref = request.query_params.get("ref")
                if raw_ref:
                    ref_code = raw_ref
            except Exception:
                ref_code = None
        # Lazy import — keeps the SaaS boundary explicitly clean.
        from trading_bot.api.upgrade_events import record_upgrade_event
        record_upgrade_event(
            api_key=api_key,
            event=event,
            path=path_value,
            request_id=request_id,
            ref_code=ref_code,
            copy_variant=copy_variant,
        )
    except Exception as exc:
        log.debug("upgrade_events.emit_error", event=event, error=str(exc))


def _count_free_tier_usage_today(
    key_hash: str,
    today: Optional[str] = None,
) -> tuple[int, int]:
    """
    Return ``(total_requests, report_requests)`` for ``key_hash`` on
    the current UTC date.

    * ``total_requests`` — every usage-log row for this key whose
      ``timestamp`` falls on ``today``.
    * ``report_requests`` — the subset whose ``path`` starts with
      ``/reports/``.

    Best-effort: any I/O / decoding failure returns ``(0, 0)`` and
    logs at DEBUG. The caller's request must never fail because
    this counter hit disk trouble.
    """
    if not key_hash:
        return (0, 0)
    day = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = _usage_log_path()
    if not path.exists():
        return (0, 0)
    total = 0
    reports = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("key_hash") != key_hash:
                    continue
                ts = rec.get("timestamp")
                if not isinstance(ts, str) or not ts.startswith(day):
                    continue
                total += 1
                p = rec.get("path")
                if isinstance(p, str) and p.startswith(
                    _FREE_TIER_REPORT_PATH_PREFIX,
                ):
                    reports += 1
    except Exception as exc:
        log.debug("free_tier.count_error", error=str(exc))
    return (total, reports)


def require_api_key(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> str:
    """
    Reject any request that does not carry the correct
    `Authorization: Bearer <token>` header. Returns the validated
    token string so handlers can read it (e.g., for tier classification).

    Accepted tokens (Phase 6.2 precedence):
      1. Revoked manifest hash → 403 (rejected before any other check
         so a revoked key cannot be reinstated by also appearing in
         the env-var lists).
      2. ``TRADING_API_KEY`` exact match → free tier.
      3. ``TRADING_API_PREMIUM_KEYS`` membership → premium tier.
      4. Stripe-cached active subscription (when Stripe is
         configured) → premium tier.
      5. Manifest entry (``data/api_keys_manifest.jsonl``) with
         ``tier="premium"`` → premium tier.
      6. Manifest entry with ``tier="free"`` → free tier.
      7. Otherwise → 403.

    Failure modes:
      - 503 when no env keys AND no usable manifest entries — fail-closed.
      - 401 when the header is missing or non-Bearer.
      - 403 when the header's token matches none of the sources, or
        when the presented key has been revoked.

    Side effect on success: the validated token, its hash, and its
    resolved tier are stashed on ``request.state`` so downstream
    middleware/handlers can read them without re-hashing or
    re-resolving. The raw token NEVER leaves the request's
    in-memory state — it is not logged and not persisted.
    """
    configured = (os.getenv(API_KEY_ENV_VAR, "") or "").strip()
    premium_keys = _premium_keys_set()
    manifest_active = key_store.has_active_keys()
    if not configured and not premium_keys and not manifest_active:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "API key not configured on server; set TRADING_API_KEY, "
                "TRADING_API_PREMIUM_KEYS, or issue a key via "
                "`python -m trading_bot.api.keys issue` before "
                "accepting requests"
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
    presented = creds.credentials

    # Revocation is the very first check after parsing — a revoked
    # key must never authenticate, even if it also happens to match
    # an env var. This makes revocation the unambiguous kill switch.
    presented_hash = key_store.hash_api_key(presented)
    if presented_hash and key_store.is_revoked(presented_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    # Precedence (Phase 6.2):
    #   1. Stripe active/cache → premium  (so a cancelled Stripe
    #      subscription cannot be silently reinstated by the same
    #      key also appearing in the manifest as premium)
    #   2. env premium list   → premium  (operator override)
    #   3. manifest premium   → premium  (CLI-issued)
    #   4. manifest free      → free     (CLI-issued)
    #   5. env single key     → free     (legacy single-tenant deploy)
    #   6. otherwise          → 403
    resolved_tier: Optional[str] = None
    if is_stripe_configured() and is_premium_via_stripe(presented):
        resolved_tier = TIER_PREMIUM
    elif presented in premium_keys:
        resolved_tier = TIER_PREMIUM
    else:
        entry = key_store.verify_api_key(presented)
        if entry is not None:
            resolved_tier = entry.tier
        elif configured and presented == configured:
            resolved_tier = TIER_FREE

    if resolved_tier is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    # Stash only after validation — failure paths raised above never
    # reach this line, so the presence of `api_key` / `api_key_tier`
    # on request.state implies "auth OK".
    try:
        request.state.api_key = presented
        request.state.api_key_hash = presented_hash
        request.state.api_key_tier = resolved_tier
    except Exception:
        pass
    return presented


# ---------------------------------------------------------------------------
# Phase 4.5 — free-tier limit enforcement
# ---------------------------------------------------------------------------


def _today_utc() -> _date_type:
    """UTC date helper. Patchable in tests for deterministic windowing."""
    return datetime.now(timezone.utc).date()


def _free_date_allowed(date_str: str) -> bool:
    """
    Free tier may access dates within the last MAX_FREE_TIER_DAYS days
    (today + the previous N-1). Older dates and future dates are blocked.
    """
    try:
        target = _date_type.fromisoformat(date_str)
    except (ValueError, TypeError):
        # Malformed dates are rejected upstream by `_validate_date`;
        # return True here so the validator's 400 wins over the 403.
        return True
    today = _today_utc()
    delta_days = (today - target).days
    return 0 <= delta_days < MAX_FREE_TIER_DAYS


def _enforce_free_limits(
    *,
    is_premium: bool,
    date_requested: Optional[str] = None,
    n_experiments: Optional[int] = None,
    explicit_limit: Optional[int] = None,
    request: Optional[Request] = None,
    api_key: Optional[str] = None,
) -> None:
    """
    Raise HTTP 403 with the documented "upgrade required for full
    access" message when a free-tier request exceeds the per-endpoint
    cap. Premium requests are short-circuit no-ops.

    Parameters (all optional — pass only those relevant to the route):
      date_requested  : YYYY-MM-DD asked for; rejected if outside free window.
      n_experiments   : 1-indexed nth-most-recent experiment; rejected if > cap.
      explicit_limit  : caller-supplied `?limit=` value; rejected if > cap.
      request         : (Phase 5.5) enables upgrade-event telemetry.
      api_key         : (Phase 5.5) enables upgrade-event telemetry.
    """
    if is_premium:
        return
    if date_requested is not None and not _free_date_allowed(date_requested):
        _emit_upgrade_event(request, api_key, "old_report_blocked")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=UPGRADE_REQUIRED_DETAIL,
        )
    if n_experiments is not None and n_experiments > MAX_FREE_TIER_EXPERIMENTS:
        _emit_upgrade_event(request, api_key, "experiment_limit_blocked")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=UPGRADE_REQUIRED_DETAIL,
        )
    if explicit_limit is not None and explicit_limit > MAX_FREE_TIER_EXPERIMENTS:
        _emit_upgrade_event(request, api_key, "experiment_limit_blocked")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=UPGRADE_REQUIRED_DETAIL,
        )


# ---------------------------------------------------------------------------
# Sanitization — the SaaS boundary. See docs/CORE_CONTROL.md.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 8.2 — feature-level tier differentiation
# ---------------------------------------------------------------------------
#
# Free-tier callers receive a curated subset of each tier-gated
# response; premium callers receive the full response. The split is
# defined here once and used consistently across /reports/latest,
# /reports/history, and /dashboard so a single allow-list change
# propagates everywhere.

PREMIUM_FEATURE_DETAIL = "premium feature — upgrade required"
PREMIUM_FEATURE_HINT = "upgrade for full access"

# Fields that survive the free-tier projection on a daily report.
# Anything outside this allow-list is dropped before the response is
# emitted. The allow-list keeps the high-level summary an integrator
# needs to render a "did the bot work today?" view, while hiding the
# deep per-tier / per-reason / per-regime breakdowns that are part
# of the premium offering.
_FREE_REPORT_ALLOWED_FIELDS: tuple[str, ...] = (
    "report_type",
    "report_date",
    "scorer_fingerprint",
    "totals",
    "promotion_readiness",
)


def _is_premium_user(request: Request) -> bool:
    """
    Phase 8.2 — fast path for tier classification inside route
    handlers and middleware.

    Prefers the value cached on ``request.state.api_key_tier`` by
    ``require_api_key`` (Phase 6.2). When that's missing — e.g. a
    test calls a helper directly without going through the auth
    dependency — falls back to extracting the bearer and consulting
    ``_is_premium``.

    Returns False on any unauthenticated request, so callers can use
    this in /-style public handlers without a separate guard.
    """
    cached = getattr(
        getattr(request, "state", None), "api_key_tier", None,
    )
    if cached == TIER_PREMIUM:
        return True
    if cached == TIER_FREE:
        return False
    api_key = _extract_bearer_token(request)
    if not api_key:
        return False
    return _is_premium(api_key, request=request)


def _project_report_for_free(report: dict) -> dict:
    """
    Project a sanitised daily report through the Phase 8.2 free-tier
    allow-list. Returns a NEW dict; the input is not mutated. Pure
    function — no I/O.

    The Phase 8.3 upgrade payload is attached by the route handler
    via ``_build_upgrade_payload`` (which makes a Stripe call), so
    this helper stays cheap to call in tests and from the dashboard
    renderer.
    """
    if not isinstance(report, dict):
        return {"tier": TIER_FREE}
    out: dict[str, Any] = {
        f: report[f] for f in _FREE_REPORT_ALLOWED_FIELDS if f in report
    }
    out["tier"] = TIER_FREE
    return out


def _build_upgrade_payload(
    request: Request,
    *,
    reason: str,
    required: bool,
    is_premium: Optional[bool] = None,
    key_hash: Optional[str] = None,
) -> Optional[dict]:
    """
    Phase 8.3 — upgrade-pressure payload builder.

    Returns the documented dict shape::

        {
          "required":     bool,
          "reason":       str,
          "checkout_url": str,
          "hint":         str,
        }

    …for free-tier callers, or ``None`` for premium callers and on
    any Stripe-side failure. The caller is expected to attach the
    payload to its response under an ``upgrade`` key when present
    and emit the base response unchanged otherwise.

    The ``checkout_url`` is freshly minted via
    ``billing.create_checkout_session_for_hash`` and is NEVER
    persisted — neither this function nor any caller writes the
    URL to the operator logs, manifest, premium cache, or
    revocation log. The Stripe-side metadata follows the Phase 7.3
    hash-only contract (``metadata[key_hash]`` only, never
    ``metadata[api_key]``).

    Performance note: this helper makes one outbound Stripe
    request per call. The caller is responsible for ensuring the
    helper fires only on responses where the upgrade prompt
    matters (free-tier 429 / 403 / limited-access body); premium
    callers exit early and never trigger the network call.

    Failure posture: any exception inside the Stripe call is
    caught, logged at DEBUG, and produces a ``None`` return — so
    a transient Stripe outage degrades to "no upgrade prompt
    attached" rather than crashing the underlying response.
    """
    # Premium short-circuit — never call Stripe for an existing
    # subscriber.
    if is_premium is None:
        is_premium = _is_premium_user(request)
    if is_premium:
        return None

    # Find the caller's hash. Prefer the cached one set by
    # require_api_key (Phase 6.2); otherwise re-extract the bearer
    # so this helper works from middleware that fires BEFORE the
    # auth dependency.
    if not key_hash:
        cached = getattr(
            getattr(request, "state", None), "api_key_hash", None,
        )
        if cached:
            key_hash = cached
    if not key_hash:
        api_key = _extract_bearer_token(request)
        if not api_key:
            return None
        try:
            key_hash = key_store.hash_api_key(api_key)
        except Exception:
            return None
    if not key_hash:
        return None

    try:
        success_url, cancel_url = _build_checkout_redirect_urls()
    except HTTPException as exc:
        # Public-base-url not configured → no checkout possible.
        log.debug(
            "upgrade_payload.config_missing",
            reason=reason, status=int(exc.status_code),
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log.debug(
            "upgrade_payload.url_build_error",
            reason=reason, error=str(exc),
        )
        return None

    try:
        result = create_checkout_session_for_hash(
            key_hash=key_hash,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except Exception as exc:  # noqa: BLE001 — defensive on Stripe path
        log.debug(
            "upgrade_payload.checkout_failed",
            reason=reason, error=str(exc),
        )
        return None

    # Phase 8.4 — funnel: stage 1 of 3 (`upgrade_shown`). Best-
    # effort: any failure inside the writer is swallowed so the
    # caller's response is unaffected.
    try:
        from trading_bot.api.upgrade_events import (
            EVENT_UPGRADE_SHOWN, record_upgrade_funnel_event,
        )
        request_id = getattr(
            getattr(request, "state", None), "request_id", None,
        )
        endpoint = ""
        try:
            endpoint = request.url.path
        except Exception:
            endpoint = ""
        record_upgrade_funnel_event(
            key_hash, EVENT_UPGRADE_SHOWN,
            reason=reason, endpoint=endpoint, request_id=request_id,
        )
    except Exception as exc:
        log.debug("upgrade_funnel.shown_emit_error", error=str(exc))

    return {
        "required": bool(required),
        "reason": reason,
        "checkout_url": result["checkout_url"],
        "hint": PREMIUM_FEATURE_HINT,
    }


def _premium_required_response(
    request: Request,
    tier: str,
) -> JSONResponse:
    """
    Phase 8.3 — uniform 403 builder for every premium-only feature.

    Returns a ``JSONResponse`` (rather than raising
    ``HTTPException``) so we can attach the Phase 8.3 upgrade
    payload to the body. The body schema is::

        {
          "detail":  "premium feature — upgrade required",
          "upgrade": <Phase 8.3 payload, or absent if Stripe failed>
        }

    Phase 8.2 contract preserved: ``detail`` is still the documented
    constant, ``X-Usage-Tier`` still carries the caller's tier so a
    client can branch without parsing the body.
    """
    body: dict[str, Any] = {"detail": PREMIUM_FEATURE_DETAIL}
    upgrade = _build_upgrade_payload(
        request, reason="feature_locked", required=True,
    )
    if upgrade is not None:
        body["upgrade"] = upgrade
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content=body,
        headers={USAGE_TIER_HEADER: tier},
    )


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


# ---------------------------------------------------------------------------
# Phase 4.2 — middleware stack
#
# FastAPI/Starlette applies registered middleware in REVERSE order: the
# last one registered here runs OUTERMOST. We therefore register them in
# the order (innermost → outermost):
#
#   1. rate_limit  — rejects excess requests close to the handler.
#   2. logging     — measures + logs once, seeing final status even for
#                    rate-limited 429s.
#   3. cors        — attaches CORS response headers per config.
#   4. security    — outermost; security headers get applied to EVERY
#                    response, including 429, CORS preflight, and errors.
#
# None of the middleware ever logs the Authorization header. Rate limit
# state is kept in a process-local dict; a reverse proxy / WAF is the
# expected second line of defence for multi-process deployments.
# ---------------------------------------------------------------------------


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Reject clients that exceed the per-minute budget."""
    limit = _rate_limit_per_minute()
    client_ip = _client_ip(request)
    now = time.time()
    # Fixed 60-second window keyed by wall-clock minute.
    window_start = (int(now) // 60) * 60

    with _rate_limit_lock:
        count, prev_window = _rate_limit_bucket.get(
            client_ip, (0, window_start)
        )
        if prev_window != window_start:
            count = 0
        count += 1
        _rate_limit_bucket[client_ip] = (count, window_start)

    if count > limit:
        retry_after = max(1, int(window_start + 60 - now))
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "rate limit exceeded"},
            headers={"Retry-After": str(retry_after)},
        )
    return await call_next(request)


@app.middleware("http")
async def free_tier_middleware(request: Request, call_next):
    """
    Phase 5.4 — enforce per-UTC-day request caps on free-tier callers.

    Two caps, both driven by env vars (reversible):

      * ``TRADING_FREE_MAX_REQUESTS_PER_DAY`` (default 50)
        — every protected request counts, regardless of path.
      * ``TRADING_FREE_MAX_REPORT_CALLS`` (default 10)
        — tighter cap on calls whose path starts with
        ``/reports/``.

    When a free-tier caller exceeds ``max_requests_per_day`` we
    return ``429`` with
    ``{"detail": "free tier limit reached — upgrade for continued access"}``.
    When they exceed ``max_report_calls`` on a ``/reports/*`` path
    we return ``403`` with
    ``{"detail": "upgrade required for full access"}``.

    Both rejection responses — and every successful response for a
    free-tier caller — carry the headers
    ``X-Free-Tier-Usage: <current>/<limit>`` and
    ``X-Free-Tier-Remaining: <remaining>``.

    Unaffected surfaces:

      * premium users (``_is_premium`` returns True);
      * the public landing / health paths and ``POST /webhook/stripe``;
      * unauthenticated requests (they are rejected with 401/403
        by ``require_api_key`` at dispatch time, NOT by this
        middleware — so limits cannot be used as an auth oracle).

    Best-effort: any failure reading the usage log degrades to
    "no counts yet" — we prefer to let a request through than to
    block a paying user because of a disk outage.
    """
    path = request.url.path
    if path in _FREE_TIER_EXEMPT_PATHS:
        return await call_next(request)

    api_key = _extract_bearer_token(request)
    if not api_key:
        # Anonymous: let require_api_key reject with 401/403.
        return await call_next(request)
    if not _is_known_key(api_key):
        # Unknown key: let require_api_key reject with 401/403.
        return await call_next(request)
    if _is_premium(api_key):
        # Premium keys are entirely exempt — no enforcement, no
        # headers. The premium surface must behave identically to
        # the pre-Phase-5.4 server.
        return await call_next(request)

    # Free-tier authenticated caller. Enforce and annotate.
    max_total = _free_max_requests_per_day()
    max_reports = _free_max_report_calls()
    key_hash = _hash_api_key(api_key)
    total_today, reports_today = _count_free_tier_usage_today(key_hash)
    is_report = path.startswith(_FREE_TIER_REPORT_PATH_PREFIX)

    usage_header = f"{total_today}/{max_total}"
    remaining = max(0, max_total - total_today)

    if total_today >= max_total:
        # Phase 5.7 — operator-overridable copy. Resolve once so the
        # exact same string flows into the response body AND the
        # Phase 5.8 telemetry hash.
        copy = _limit_hit_copy()
        # Phase 5.5 + 5.8 — telemetry is best-effort; never affects
        # the response.
        _emit_upgrade_event(
            request, api_key, "daily_request_limit_hit",
            copy_variant=copy,
        )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": copy},
            headers={
                FREE_TIER_USAGE_HEADER: usage_header,
                FREE_TIER_REMAINING_HEADER: "0",
            },
        )
    if is_report and reports_today >= max_reports:
        copy = _report_limit_copy()
        _emit_upgrade_event(
            request, api_key, "report_limit_hit",
            copy_variant=copy,
        )
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": copy},
            headers={
                FREE_TIER_USAGE_HEADER: usage_header,
                FREE_TIER_REMAINING_HEADER: str(remaining),
            },
        )

    response: Response = await call_next(request)

    # Annotate the response. We only set the headers on responses
    # the auth layer accepted — a 401/403/503 means the caller never
    # actually authenticated, so presenting a per-user counter there
    # would be misleading AND could be used as an account-exists
    # oracle.
    if response.status_code not in {401, 403, 503}:
        new_total = total_today + 1
        response.headers[FREE_TIER_USAGE_HEADER] = (
            f"{new_total}/{max_total}"
        )
        response.headers[FREE_TIER_REMAINING_HEADER] = str(
            max(0, max_total - new_total),
        )
    return response


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """
    Log one structured line per request. NEVER touches the
    Authorization header, so a bearer token cannot leak into the
    observability pipeline.
    """
    start = time.perf_counter()
    method = request.method
    path = request.url.path
    client_ip = _client_ip(request)
    try:
        response: Response = await call_next(request)
    except Exception as exc:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        log.warning(
            "api.request_error",
            method=method,
            path=path,
            status=500,
            duration_ms=duration_ms,
            client_ip=client_ip,
            error_type=type(exc).__name__,
        )
        raise
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    request_id = getattr(getattr(request, "state", None), "request_id", None)
    log.info(
        "api.request",
        method=method,
        path=path,
        status=response.status_code,
        duration_ms=duration_ms,
        client_ip=client_ip,
        request_id=request_id,
    )
    return response


@app.middleware("http")
async def usage_enforcement_middleware(request: Request, call_next):
    """
    Phase 8.1 — tier-aware daily usage enforcement.

    Layered ABOVE Phase 5.4's free-tier middleware in the request
    pipeline so its 429 short-circuits when both would otherwise
    reject the same call. Layered BELOW the audit + usage + growth
    + cors + security-headers middlewares so 429 responses still
    get audited and security-decorated.

    Skipped for:
      * the documented exempt paths (``/``, ``/health``,
        ``/webhook/stripe``, the three browser icon routes) plus
        anything operators add via
        ``TRADING_USAGE_LIMIT_EXEMPT_PATHS``;
      * unauthenticated requests (``_extract_bearer_token`` returns
        None) — let ``require_api_key`` handle 401/403/503;
      * unknown / bogus keys (``_is_known_key`` returns False) —
        same: do not count failed auth against any user;
      * the entire enforcement layer when
        ``TRADING_USAGE_ENFORCEMENT_ENABLED`` is set to a non-truthy
        value.

    For authenticated callers the middleware:
      1. Classifies the tier (premium vs free) using the existing
         ``_is_premium`` helper.
      2. Reads the per-key request count for the current UTC date
         from the existing usage JSONL log (Phase 4.6 schema —
         ``key_hash`` is already SHA-256, never the raw key).
      3. Compares against the tier-appropriate cap:
         * free → ``TRADING_FREE_DAILY_REQUEST_LIMIT`` (default 50)
         * premium → ``TRADING_PREMIUM_DAILY_REQUEST_LIMIT`` (default 1000)
      4. If at-or-above cap → 429 with the documented body and the
         ``X-Usage-Limit`` / ``X-Usage-Remaining`` / ``X-Usage-Tier``
         / ``Retry-After`` headers.
      5. Otherwise calls ``call_next`` and decorates the response
         with the same headers (so callers always know where they
         stand). The headers reflect the *post-request* count when
         we know the call will succeed.

    Best-effort: any failure reading the usage log degrades to
    "no counts yet" (Phase 4.6's ``_count_free_tier_usage_today``
    already returns ``(0, 0)`` on disk trouble) so a partial outage
    NEVER blocks an authenticated user.
    """
    if not _usage_enforcement_enabled():
        return await call_next(request)

    path = request.url.path
    if path in _usage_limit_exempt_paths():
        return await call_next(request)

    api_key = _extract_bearer_token(request)
    if not api_key:
        # Anonymous: let require_api_key reject with 401.
        return await call_next(request)
    if not _is_known_key(api_key):
        # Unknown / bogus / revoked: let require_api_key reject
        # with 403. Do NOT count this against any user.
        return await call_next(request)

    is_premium = _is_premium(api_key)
    tier = TIER_PREMIUM if is_premium else TIER_FREE
    limit = (
        _premium_daily_request_limit() if is_premium
        else _free_daily_request_limit()
    )
    key_hash = _hash_api_key(api_key)
    total_today, _ = _count_free_tier_usage_today(key_hash)

    usage_headers = {
        USAGE_LIMIT_HEADER: str(limit),
        USAGE_REMAINING_HEADER: str(max(0, limit - total_today)),
        USAGE_TIER_HEADER: tier,
    }

    if total_today >= limit:
        # 429 with the same usage headers + Retry-After. The header
        # value is the number of seconds until the next UTC midnight,
        # since the cap is daily.
        now_utc = datetime.now(timezone.utc)
        midnight_utc = (
            now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        retry_after = max(1, int((midnight_utc - now_utc).total_seconds()))
        body: dict[str, Any] = {"detail": USAGE_LIMIT_DETAIL}
        # Phase 8.3 — attach the upgrade-pressure payload for free
        # callers. Pass the already-classified is_premium + key_hash
        # so the helper does not redundantly re-extract the bearer.
        upgrade = _build_upgrade_payload(
            request,
            reason="usage_limit",
            required=True,
            is_premium=is_premium,
            key_hash=key_hash,
        )
        if upgrade is not None:
            body["upgrade"] = upgrade
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=body,
            headers={
                **usage_headers,
                USAGE_REMAINING_HEADER: "0",
                RETRY_AFTER_HEADER: str(retry_after),
            },
        )

    response: Response = await call_next(request)
    # Decorate every response from this caller — including Phase 8.2
    # premium-feature 403s, which DO consume usage quota because
    # auth succeeded. Skip only 401/503 (the auth dependency
    # rejected the request after we let it through, so no usage
    # row will be written and we shouldn't lie about the count).
    if response.status_code not in {401, 503}:
        new_total = total_today + 1
        response.headers[USAGE_LIMIT_HEADER] = str(limit)
        response.headers[USAGE_REMAINING_HEADER] = str(
            max(0, limit - new_total),
        )
        # Honour an explicit X-Usage-Tier set by the route handler
        # (e.g. the Phase 8.2 premium-feature 403). Otherwise apply
        # the middleware's classification.
        response.headers.setdefault(USAGE_TIER_HEADER, tier)
    return response


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """
    Append one JSONL audit record per request. Sets X-Request-ID on
    both request.state and the outgoing response headers.

    Never logs the Authorization header. The User-Agent is hashed
    (SHA-256, 32 hex chars) rather than stored verbatim so fingerprints
    still cluster per client without persisting the raw identifier.

    Must never fail the request — all exceptions from record building
    or file I/O are caught and the response is returned unchanged.
    """
    start = time.perf_counter()
    method = request.method
    path = request.url.path
    client_ip = _client_ip(request)
    ua_hash = _hash_user_agent(request.headers.get("user-agent"))
    request_id = _sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))

    # Make the id available to downstream middleware and the handler.
    try:
        request.state.request_id = request_id
    except Exception:
        # request.state should always exist on Starlette, but never raise.
        pass

    try:
        response: Response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        try:
            _append_audit_record({
                "timestamp": (
                    datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.%fZ"
                    )
                ),
                "method": method,
                "path": path,
                "status_code": 500,
                "duration_ms": duration_ms,
                "client_ip": client_ip,
                "authenticated": False,
                "user_agent_hash": ua_hash,
                "request_id": request_id,
            })
        except Exception as exc:
            log.debug("audit.record_build_error", error=str(exc))
        raise

    duration_ms = round((time.perf_counter() - start) * 1000, 2)

    # Stamp the response with the request id so clients can correlate.
    try:
        response.headers[REQUEST_ID_HEADER] = request_id
    except Exception:
        pass

    try:
        _append_audit_record({
            "timestamp": (
                datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                )
            ),
            "method": method,
            "path": path,
            "status_code": int(response.status_code),
            "duration_ms": duration_ms,
            "client_ip": client_ip,
            "authenticated": _is_authenticated_status(response.status_code),
            "user_agent_hash": ua_hash,
            "request_id": request_id,
        })
    except Exception as exc:
        log.debug("audit.record_build_error", error=str(exc))

    return response


@app.middleware("http")
async def usage_middleware(request: Request, call_next):
    """
    Phase 4.6 — append one per-key usage record for every SUCCESSFUL
    protected request. Skipped for:
      - public paths (/, /health).
      - requests that never authenticated (request.state.api_key
        was not set by `require_api_key`).

    The raw API key never leaves memory: we hash it (SHA-256[:32])
    before any I/O. Usage writes never fail the request — all
    exceptions are caught and logged at DEBUG.

    Registered AFTER `audit_middleware` so `request.state.request_id`
    is already populated by the time the usage record is built.
    """
    start = time.perf_counter()
    try:
        response: Response = await call_next(request)
    except Exception:
        # We skip usage logging on handler exceptions — the audit
        # trail covers them. Re-raise so outer middleware (logging,
        # security headers) still runs.
        raise

    duration_ms = round((time.perf_counter() - start) * 1000, 2)

    # Skip public paths.
    if request.url.path in _PUBLIC_PATHS_NO_USAGE:
        return response

    # Skip requests that never authenticated (401/403/503, OPTIONS
    # preflight handled by cors, etc. — in all such cases the
    # auth dependency did not run to completion).
    api_key = getattr(getattr(request, "state", None), "api_key", None)
    if not api_key:
        return response

    try:
        # Phase 6.2 — prefer the values cached on request.state by
        # require_api_key so we don't re-hash the same key or
        # re-resolve the same tier per request.
        cached_hash = getattr(
            getattr(request, "state", None), "api_key_hash", None,
        )
        key_hash = cached_hash if cached_hash else _hash_api_key(api_key)
        record = {
            "timestamp": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "key_hash": key_hash,
            "tier": (
                TIER_PREMIUM
                if _is_premium(api_key, request=request)
                else TIER_FREE
            ),
            "method": request.method,
            "path": request.url.path,
            "status_code": int(response.status_code),
            "duration_ms": duration_ms,
            "request_id": getattr(
                getattr(request, "state", None), "request_id", None
            ),
        }
        _append_usage_record(record)
    except Exception as exc:
        log.debug("usage.record_build_error", error=str(exc))

    return response


@app.middleware("http")
async def growth_middleware(request: Request, call_next):
    """
    Phase 5.1 — record a growth event iff the authenticated caller
    supplied a ``?ref=<code>`` query parameter.

    Skipped for:
      - unauthenticated requests (``request.state.api_key`` unset).
      - requests without a ``ref`` query param or with an empty /
        invalid one (the growth module sanitizes to a safe charset).

    Best-effort: every exception from the growth writer is caught
    and logged at DEBUG so the trading-bot's read-only API remains
    unaffected by a disk-full / permissions failure here.

    Registered AFTER ``usage_middleware`` but BEFORE ``cors`` — the
    order is immaterial for correctness since both read
    ``request.state.api_key`` in their POST phase, but this keeps
    the three "per-request record" middlewares adjacent.
    """
    response: Response = await call_next(request)
    try:
        api_key = getattr(
            getattr(request, "state", None), "api_key", None,
        )
        if not api_key:
            return response
        raw_ref = request.query_params.get("ref")
        if not raw_ref:
            return response
        # Lazy-import to keep the SaaS boundary unambiguously clean:
        # growth is an API-layer sibling, not Core.
        from trading_bot.api.growth import record_growth_event
        record_growth_event(
            api_key=api_key,
            ref_code=raw_ref,
            path=request.url.path,
            request_id=getattr(
                getattr(request, "state", None), "request_id", None,
            ),
        )
    except Exception as exc:
        log.debug("growth.middleware_error", error=str(exc))
    return response


@app.middleware("http")
async def cors_middleware(request: Request, call_next):
    """
    Tiny read-only CORS implementation driven by
    `TRADING_API_ALLOWED_ORIGINS`. Env-driven so the server can be
    reconfigured without a restart. Default (env unset) → zero CORS
    headers and preflights return 403.
    """
    allowed = _allowed_origins()
    origin = request.headers.get("origin")

    # CORS preflight
    if request.method == "OPTIONS" and origin is not None:
        if origin in allowed:
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization",
                    "Access-Control-Max-Age": "600",
                    "Vary": "Origin",
                },
            )
        return Response(status_code=403)

    response: Response = await call_next(request)
    if origin and origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        # Append to any existing Vary header instead of clobbering it.
        vary = response.headers.get("vary")
        response.headers["Vary"] = (
            f"{vary}, Origin" if vary and "origin" not in vary.lower()
            else (vary or "Origin")
        )
    return response


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Outermost middleware — applies security headers to every response."""
    response: Response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value
    return response


@app.get("/health", tags=["public"])
def health() -> dict[str, Any]:
    """Liveness probe — intentionally unauthenticated."""
    return {
        "status": "ok",
        "service": "momentum-trading-bot-analytics",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Phase 7.1 — browser icon noise cleanup
# ---------------------------------------------------------------------------
#
# Browsers auto-request /favicon.ico, /apple-touch-icon.png, and
# /apple-touch-icon-precomposed.png on almost every page view. We do
# not ship icon assets, so those requests otherwise 404. Returning
# 204 No Content keeps the audit log clean without shipping any
# binary asset. Security headers still apply (they come from the
# global response-header middleware).
#
# No auth required — these URLs are public by browser convention and
# are never used to carry secrets.


_ICON_ROUTES: tuple[str, ...] = (
    "/favicon.ico",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
)


def _icon_204() -> Response:
    """Return an empty 204 with no body and no Content-Type."""
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/favicon.ico", tags=["public"])
def favicon_ico() -> Response:
    return _icon_204()


@app.get("/apple-touch-icon.png", tags=["public"])
def apple_touch_icon() -> Response:
    return _icon_204()


@app.get("/apple-touch-icon-precomposed.png", tags=["public"])
def apple_touch_icon_precomposed() -> Response:
    return _icon_204()


# ---------------------------------------------------------------------------
# Phase 4.3 + 5.2 — public landing page (now conversion-focused)
#
# The page is fully static HTML with exactly one optional piece of
# dynamic content: the sanitised ``?ref=<code>`` query parameter is
# echoed into a small "Invited by" banner so a referrer can confirm
# their link resolved correctly. The sanitisation set is identical
# to the growth-log sanitiser (Phase 5.1), which already strips any
# character outside ``[A-Za-z0-9\-_:.]`` and caps at 64 chars — so
# HTML, javascript: URIs, angle brackets, and control characters
# cannot survive. The ref value is also HTML-escaped at render time
# as defence in depth.
#
# Everything else on the page is a compile-time constant. The
# handler performs no disk reads, no env lookup, no subprocess, and
# no helper that performs I/O — which is how we guarantee that
# report data, scorer_config, paths, or secrets cannot leak.
# ---------------------------------------------------------------------------


# Use the exact same ref-code sanitiser the growth middleware uses
# when it writes the event record. This guarantees "what you see is
# what gets logged" — the display never reveals a character that
# the audit log would have silently stripped.
from trading_bot.api.growth import _sanitize_ref_code as _sanitize_landing_ref_code  # noqa: E402


# Phase 5.9 — polished SaaS landing-page stylesheet. Inline only;
# zero JavaScript, zero external resources, mobile-first via a
# single ``@media (min-width: 600px)`` breakpoint. The block is
# intentionally separate from ``_DASHBOARD_CSS`` so neither
# stylesheet has to absorb the other's concerns.
_LANDING_PAGE_CSS = """
<style>
  :root {
    color-scheme: light;
    --bg: #f6f8fb;
    --surface: #ffffff;
    --surface-soft: #eef3fa;
    --primary: #2057b2;
    --primary-dark: #0e2f5a;
    --accent: #1a7f1a;
    --text: #1f2730;
    --muted: #5b6878;
    --border: #dde3ed;
    --shadow-sm: 0 1px 2px rgba(15, 33, 68, 0.06);
    --shadow-md: 0 6px 18px rgba(15, 33, 68, 0.08);
    --hero-grad: linear-gradient(140deg, #0e2f5a 0%, #2057b2 60%, #3a7bd8 100%);
    --radius: 10px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 16px;
    line-height: 1.55;
    color: var(--text);
    background: var(--bg);
  }
  h1, h2, h3 { color: var(--primary-dark); margin-top: 0; }
  section {
    padding: 2.5em 1.25em;
    max-width: 980px;
    margin: 0 auto;
  }
  section h2 {
    font-size: 1.35em;
    margin: 0 0 0.7em;
    letter-spacing: -0.005em;
  }
  section.hero {
    max-width: none;
    background: var(--hero-grad);
    color: #ffffff;
    padding: 3.5em 1.5em 3em;
    text-align: center;
  }
  section.hero h1 {
    color: #ffffff;
    font-size: 1.85em;
    margin: 0 0 0.4em;
    letter-spacing: -0.01em;
  }
  section.hero .tagline {
    font-size: 1.15em;
    opacity: 0.96;
    max-width: 640px;
    margin: 0 auto 0.6em;
  }
  section.hero .meta {
    color: rgba(255, 255, 255, 0.85);
    font-size: 0.88em;
    margin: 0 auto 1.2em;
  }
  .ref {
    display: inline-block;
    background: rgba(255, 255, 255, 0.14);
    border: 1px solid rgba(255, 255, 255, 0.32);
    border-radius: 6px;
    padding: 0.45em 0.85em;
    margin: 0.4em auto 0;
    color: #ffffff;
    font-size: 0.92em;
  }
  .ref code {
    background: rgba(255, 255, 255, 0.18);
    padding: 0.05em 0.4em;
    margin-left: 0.25em;
    border-radius: 3px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .feature-grid {
    list-style: none;
    padding: 0;
    margin: 0.8em 0 0;
    display: grid;
    grid-template-columns: 1fr;
    gap: 1em;
    counter-reset: stepnum;
  }
  .feature-grid li {
    counter-increment: stepnum;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1em 1.1em 1em 3em;
    box-shadow: var(--shadow-sm);
    position: relative;
  }
  .feature-grid li::before {
    content: counter(stepnum);
    position: absolute;
    left: 0.85em;
    top: 0.95em;
    width: 1.7em;
    height: 1.7em;
    border-radius: 50%;
    background: var(--primary);
    color: #ffffff;
    text-align: center;
    line-height: 1.7em;
    font-weight: 700;
    font-size: 0.92em;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.2em 1.3em;
    box-shadow: var(--shadow-sm);
    margin-top: 0.6em;
  }
  .card table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.94em;
  }
  .card th, .card td {
    text-align: left;
    padding: 0.5em 0.65em;
    border-bottom: 1px solid var(--border);
  }
  .card tr:last-child td { border-bottom: none; }
  .example-card th { color: var(--muted); font-weight: 600; }
  .example-card .kv td:first-child { color: var(--muted); width: 40%; }
  .example-card table + table { margin-top: 0.7em; }
  .status-ok {
    display: inline-block;
    color: var(--accent);
    background: rgba(26, 127, 26, 0.1);
    border-radius: 999px;
    padding: 0.05em 0.7em;
    font-weight: 700;
    font-size: 0.88em;
  }
  .compare-card th {
    background: var(--surface-soft);
    color: var(--muted);
    font-weight: 600;
    font-size: 0.9em;
  }
  .compare-card td:first-child { color: var(--muted); }
  .compare-card td:last-child {
    color: var(--primary-dark);
    font-weight: 600;
  }
  .cue-row {
    margin-top: 1em;
    display: grid;
    gap: 0.8em;
    grid-template-columns: 1fr;
  }
  .cue {
    background: var(--surface-soft);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0.9em 1em;
    text-align: center;
    color: var(--primary-dark);
    font-weight: 600;
  }
  .cta-card {
    background: linear-gradient(180deg, #f3f8ff 0%, #e6efff 100%);
    border: 1px solid #c8d8f4;
    border-radius: var(--radius);
    padding: 1.6em 1.4em;
    text-align: center;
    margin-top: 0.6em;
    box-shadow: var(--shadow-md);
  }
  .cta-card p { margin: 0.4em 0; }
  .cta-card strong { color: var(--primary-dark); }
  .meta { color: var(--muted); font-size: 0.92em; }
  footer {
    border-top: 1px solid var(--border);
    color: var(--muted);
    text-align: center;
    padding: 1.6em 1em;
    font-size: 0.85em;
    background: #ffffff;
  }
  @media (min-width: 600px) {
    body { font-size: 16.5px; }
    section { padding: 3em 2em; }
    section.hero { padding: 5em 2em 3.5em; }
    section.hero h1 { font-size: 2.5em; }
    section.hero .tagline { font-size: 1.3em; }
    .feature-grid { grid-template-columns: repeat(3, 1fr); }
    .cue-row { grid-template-columns: 1fr 1fr; }
  }
  @media (min-width: 880px) {
    section { padding: 3.5em 2em; }
  }
</style>
""".strip()


def _landing_page_body(ref_code: str) -> str:
    """
    Build the <body>…</body> of the landing page. Pure — takes
    ``ref_code`` (already sanitised; may be empty) and returns a
    deterministic string.
    """
    if ref_code:
        ref_banner = (
            "<p class=\"ref\">Invited by: "
            f"<code>{_esc(ref_code)}</code></p>"
        )
    else:
        ref_banner = ""
    return (
        "<body>"
        # --- Section 1: Hero ---
        "<section class=\"hero\">"
        "<h1>Momentum Trading Bot — Analytics</h1>"
        "<p class=\"tagline\">"
        "See which trades your system should have taken — "
        "before risking money."
        "</p>"
        "<p class=\"meta\">Read-only SaaS layer. No trading. "
        "No execution.</p>"
        f"{ref_banner}"
        "</section>"
        # --- Section 2: How it works ---
        "<section>"
        "<h2>How it works</h2>"
        "<ol class=\"feature-grid\">"
        "<li>Every candidate the trading bot evaluates is scored "
        "into an A / B / C / D / F tier — offline, without ever "
        "gating a live trade.</li>"
        "<li>This service publishes daily validation reports with "
        "tier stats, decile calibration, and shadow-filter "
        "simulation rows so you can see how the scorer would have "
        "performed.</li>"
        "<li>When the filter would have kept trades that did "
        "worse than the ones it would have rejected, the guardrail "
        "flips to warning or critical and the experiment audit "
        "trail records exactly which configuration produced the "
        "outcome.</li>"
        "</ol>"
        "</section>"
        # --- Section 3: Example output ---
        "<section>"
        "<h2>Example output</h2>"
        "<p class=\"meta\">Illustrative snapshot — not live data.</p>"
        "<div class=\"card example-card\">"
        "<table class=\"kv\">"
        "<tr><td>Report date</td><td>2026-04-22</td></tr>"
        "<tr><td>Guardrail status</td>"
        "<td><span class=\"status-ok\">ok</span></td></tr>"
        "<tr><td>Matched trades</td><td>103</td></tr>"
        "</table>"
        "<table>"
        "<tr><th>Tier</th><th>Rows</th>"
        "<th>Allowed outcome</th><th>Blocked outcome</th></tr>"
        "<tr><td>A</td><td>14</td><td>+0.64%</td><td>+0.12%</td></tr>"
        "<tr><td>B</td><td>31</td><td>+0.31%</td><td>+0.05%</td></tr>"
        "<tr><td>C</td><td>58</td><td>+0.14%</td><td>+0.02%</td></tr>"
        "</table>"
        "<table>"
        "<tr><th>Shadow threshold</th>"
        "<th>Allowed</th><th>Blocked</th></tr>"
        "<tr><td>0.55</td><td>45</td><td>58</td></tr>"
        "<tr><td>0.60</td><td>32</td><td>71</td></tr>"
        "</table>"
        "</div>"
        "</section>"
        # --- Section 4: Upgrade trigger ---
        "<section>"
        "<h2>Upgrade</h2>"
        "<div class=\"card compare-card\">"
        "<table>"
        "<tr><th>Capability</th><th>Free</th><th>Premium</th></tr>"
        "<tr><td>Daily validation reports</td>"
        "<td>last 3 days</td><td>full history</td></tr>"
        "<tr><td>Experiment audit trail</td>"
        "<td>3 most recent</td><td>full history</td></tr>"
        "<tr><td>Protected dashboard</td>"
        "<td>—</td><td>included</td></tr>"
        "</table>"
        "</div>"
        "<div class=\"cue-row\">"
        "<div class=\"cue\">Most users upgrade after ~7 days.</div>"
        "<div class=\"cue\">Premium users run 3–5x more requests "
        "than free users.</div>"
        "</div>"
        "</section>"
        # --- Section 5: CTA ---
        "<section>"
        "<h2>Get started</h2>"
        "<div class=\"cta-card\">"
        "<p>Request a <strong>Bearer API key</strong> from your "
        "operator to unlock the full analytics surface — "
        "daily validation reports, the audit trail, and the "
        "protected dashboard.</p>"
        "<p class=\"meta\">"
        "No sign-up fields on this page. No JavaScript. No trading "
        "endpoints. Every data endpoint requires a key."
        "</p>"
        "</div>"
        "</section>"
        "<footer>"
        "This landing page is public. All analytics endpoints "
        "require a Bearer API key."
        "</footer>"
        "</body></html>"
    )


def render_landing_page_html(ref_code: str = "") -> str:
    """
    Build the public landing page.

    Pure function: given the same ``ref_code`` it returns the same
    HTML. No I/O, no env reads, no config lookup.

    The caller is responsible for passing an already-sanitised
    ``ref_code`` (the route handler does this via
    ``_sanitize_landing_ref_code``). An unsanitised value is
    defended-against by ``_esc`` at render time, but the public
    contract is: only the sanitised charset.
    """
    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<title>Momentum Trading Bot — Analytics</title>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"description\" content=\""
        "Read-only analytics, guardrail monitoring, and audit-trail API "
        "for the Momentum Trading Bot. See which trades your system "
        "should have taken — before risking money."
        "\">"
        + _LANDING_PAGE_CSS +
        "</head>"
        + _landing_page_body(ref_code)
    )


@app.get("/", response_class=HTMLResponse, tags=["public"])
def landing_page(request: Request) -> HTMLResponse:
    """
    Public product/status page. Intentionally unauthenticated.

    The handler reads exactly one piece of runtime state: the
    ``?ref=<code>`` query parameter, which is sanitised to the
    growth-log charset before being echoed back. Every other byte
    on the page is a compile-time constant, so the handler cannot
    leak reports, manifest data, env secrets, or any other
    protected content.
    """
    raw_ref = request.query_params.get("ref")
    ref_code = _sanitize_landing_ref_code(raw_ref) if raw_ref else ""
    return HTMLResponse(
        content=render_landing_page_html(ref_code),
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Phase 4.7 — Stripe billing webhook.
#
# This is the ONLY non-read-only endpoint exposed by the SaaS API.
# It accepts server-to-server webhook deliveries from Stripe, and
# its job is strictly to update the premium-key cache based on
# subscription lifecycle events. It performs no trade action, does
# not touch any Core module, and never echoes sensitive payment
# metadata back to the caller. The boundary tests in
# tests/test_api_server.py explicitly allow POST on this single path
# and reject it everywhere else.
# ---------------------------------------------------------------------------


@app.post(
    "/webhook/stripe",
    tags=["billing"],
    # Intentionally include_in_schema=True so the OpenAPI doc shows
    # the integration surface; Stripe doesn't consume the schema.
)
async def stripe_webhook(request: Request) -> dict[str, Any]:
    """
    Accept Stripe webhook deliveries for subscription lifecycle events.

    - Requires `STRIPE_WEBHOOK_SECRET` to be configured; returns 503
      fail-closed otherwise, so a mis-deployed server cannot accept
      arbitrary billing traffic.
    - Signature is verified manually (no `stripe` pip dep required);
      invalid signatures → 400.
    - Updates the premium-key cache via
      `trading_bot.api.billing.handle_webhook_event`, which itself
      catches every failure and never raises into this handler.

    Handled event types:
      - customer.subscription.created (adds premium on active/trialing)
      - customer.subscription.deleted (removes premium)
      - invoice.payment_failed         (removes premium immediately)
    """
    secret = (os.getenv(STRIPE_WEBHOOK_SECRET_ENV_VAR, "") or "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="billing webhook not configured",
        )

    raw_body = await request.body()
    signature = request.headers.get("stripe-signature", "")
    if not verify_webhook_signature(raw_body, signature, secret):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid webhook signature",
        )

    try:
        event = json.loads(raw_body.decode("utf-8", errors="replace"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid webhook payload",
        )

    result = handle_webhook_event(event)
    # Log a structured line — the billing module already logs on
    # failures; this is a success breadcrumb tagged for ops.
    log.info(
        "billing.webhook_processed",
        type=result.get("type"),
        action=result.get("action"),
    )
    return {
        "received": True,
        "action": result.get("action", "ignored"),
        "type": result.get("type", ""),
    }


# ---------------------------------------------------------------------------
# Phase 7.3 — POST /billing/checkout (authenticated end-user upgrade)
# ---------------------------------------------------------------------------
#
# A free-tier caller hits this endpoint to receive a Stripe Checkout
# Session URL that — once paid — flips them to premium via the
# existing Phase 4.7 / 7.0 webhook plumbing.
#
# Identity flows through ``key_hash`` exclusively:
#   * the supplied bearer key is hashed by ``require_api_key`` and
#     stashed on ``request.state.api_key_hash`` (Phase 6.2);
#   * the hash is forwarded to Stripe in metadata and as
#     ``client_reference_id``;
#   * the raw key never leaves the request frame.
#
# Env vars (required for live operation):
#   STRIPE_SECRET_KEY            (or legacy STRIPE_API_KEY)
#   STRIPE_PREMIUM_PRICE_ID      (or legacy STRIPE_PRICE_ID_PREMIUM)
#   TRADING_PUBLIC_BASE_URL      (e.g. https://your-host.example.com)
#
# Env vars (optional, with defaults):
#   STRIPE_CHECKOUT_SUCCESS_PATH (default /dashboard?checkout=success)
#   STRIPE_CHECKOUT_CANCEL_PATH  (default /dashboard?checkout=cancel)

PUBLIC_BASE_URL_ENV_VAR = "TRADING_PUBLIC_BASE_URL"
CHECKOUT_SUCCESS_PATH_ENV_VAR = "STRIPE_CHECKOUT_SUCCESS_PATH"
CHECKOUT_CANCEL_PATH_ENV_VAR = "STRIPE_CHECKOUT_CANCEL_PATH"
DEFAULT_CHECKOUT_SUCCESS_PATH = "/dashboard?checkout=success"
DEFAULT_CHECKOUT_CANCEL_PATH = "/dashboard?checkout=cancel"


def _build_checkout_redirect_urls() -> tuple[str, str]:
    """
    Return (success_url, cancel_url). Raises ``HTTPException(503)``
    when ``TRADING_PUBLIC_BASE_URL`` is unset — Stripe Checkout
    requires absolute URLs so we cannot guess.
    """
    base = (os.getenv(PUBLIC_BASE_URL_ENV_VAR, "") or "").strip()
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"checkout not configured: {PUBLIC_BASE_URL_ENV_VAR} "
                "is unset"
            ),
        )
    base = base.rstrip("/")
    success_path = (
        os.getenv(CHECKOUT_SUCCESS_PATH_ENV_VAR, "") or ""
    ).strip() or DEFAULT_CHECKOUT_SUCCESS_PATH
    cancel_path = (
        os.getenv(CHECKOUT_CANCEL_PATH_ENV_VAR, "") or ""
    ).strip() or DEFAULT_CHECKOUT_CANCEL_PATH
    if not success_path.startswith("/"):
        success_path = "/" + success_path
    if not cancel_path.startswith("/"):
        cancel_path = "/" + cancel_path
    return (base + success_path, base + cancel_path)


@app.post("/billing/checkout", tags=["billing"])
def billing_checkout(
    request: Request,
    api_key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Authenticated free → premium Checkout Session creator.

    Flow:
      1. ``require_api_key`` authenticates the caller. The raw key is
         consumed by the dependency and the SHA-256 hash is stashed
         on ``request.state.api_key_hash``.
      2. If the caller is already premium, return 409 — there's
         nothing to upgrade. The customer should manage the existing
         subscription via the Stripe customer portal (separate flow).
      3. Build Stripe Checkout success/cancel URLs from
         ``TRADING_PUBLIC_BASE_URL`` + the optional path env vars.
      4. Call ``billing.create_checkout_session_for_hash`` with the
         hash. The raw key is never forwarded to Stripe.
      5. Return ``checkout_session_id``, ``checkout_url``,
         ``key_hash``, ``tier_to``. The session URL is short-lived
         and is NOT persisted to any operator log.
    """
    # Phase 6.2 always sets api_key_hash on request.state when auth
    # succeeded; the explicit hash() call is defensive in case a
    # future refactor unsets it.
    cached_hash = getattr(
        getattr(request, "state", None), "api_key_hash", None,
    )
    key_hash = cached_hash or key_store.hash_api_key(api_key)
    if not key_hash:
        # Should be unreachable since require_api_key guarantees a key.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to derive key_hash for authenticated caller",
        )

    if _is_premium(api_key, request=request):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "this key is already premium; manage the existing "
                "subscription via Stripe directly"
            ),
        )

    try:
        success_url, cancel_url = _build_checkout_redirect_urls()
        result = create_checkout_session_for_hash(
            key_hash=key_hash,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except HTTPException:
        raise
    except BillingConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"checkout not configured: {exc}",
        )
    except BillingAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"checkout provider error: {exc}",
        )
    except ValueError as exc:
        # Should not happen since we control the inputs, but treat as
        # configuration if it does.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"checkout not configured: {exc}",
        )

    # Phase 8.4 — funnel: stage 2 of 3 (`upgrade_clicked`).
    # Best-effort; never blocks the response.
    try:
        from trading_bot.api.upgrade_events import (
            EVENT_UPGRADE_CLICKED, record_upgrade_funnel_event,
        )
        request_id = getattr(
            getattr(request, "state", None), "request_id", None,
        )
        record_upgrade_funnel_event(
            result["key_hash"], EVENT_UPGRADE_CLICKED,
            reason="checkout_initiated",
            endpoint="/billing/checkout",
            request_id=request_id,
        )
    except Exception as exc:
        log.debug("upgrade_funnel.clicked_emit_error", error=str(exc))

    return {
        "checkout_session_id": result["checkout_session_id"],
        "checkout_url": result["checkout_url"],
        "key_hash": result["key_hash"],
        "tier_to": result["tier_to"],
    }


@app.get("/reports/latest", tags=["reports"])
def latest_report(
    request: Request,
    api_key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Return the most recent daily alpha validation report.

    Phase 8.2 — premium callers receive the full sanitised report;
    free callers receive a curated subset (high-level summary +
    upgrade hint). The per-row stats, decile breakdowns, and
    shadow-filter simulation rows are part of the premium
    offering.

    Phase 9.1 — both tiers also receive an ``insights`` list
    derived from the latest report and (best-effort) the prior
    day's report. Free callers receive at most
    ``FREE_INSIGHT_LIMIT`` insights with evidence trimmed to the
    documented allow-list.
    """
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
    data = _sanitize_report(_parse_report_file(candidates[-1]))
    # Phase 9.1 — best-effort prior-day load. Any failure (missing
    # file, parse error, sanitiser hiccup) yields ``None`` so the
    # trend rule simply skips itself.
    prev = None
    if len(candidates) >= 2:
        try:
            prev = _sanitize_report(_parse_report_file(candidates[-2]))
        except Exception as exc:
            log.debug(
                "reports_latest.prev_load_failed",
                path=str(candidates[-2]), error=str(exc),
            )
            prev = None
    insights = build_insights(data, prev)

    if _is_premium_user(request):
        data["insights"] = insights
        return data
    free_body = _project_report_for_free(data)
    free_body["insights"] = truncate_for_free(insights)
    upgrade = _build_upgrade_payload(
        request, reason="limited_access", required=False, is_premium=False,
    )
    if upgrade is not None:
        free_body["upgrade"] = upgrade
    return free_body


@app.get("/reports/history", tags=["reports"])
def reports_history(
    request: Request,
    api_key: str = Depends(require_api_key),
):
    """
    Phase 8.2 — premium-only listing of every daily report on disk.

    Premium → ``{"count": N, "dates": [...]}`` sorted oldest →
    newest. Free → 403 with the documented body and the Phase 8.3
    upgrade payload (when Stripe is configured).

    Registered BEFORE ``/reports/{date}`` so FastAPI's path matcher
    treats "history" as a literal segment, not a date path-param.
    """
    if not _is_premium_user(request):
        return _premium_required_response(request, TIER_FREE)

    reports = _reports_dir()
    dates: list[str] = []
    if reports.is_dir():
        for p in sorted(reports.glob("alpha_report_*.json")):
            stem = p.stem  # alpha_report_2026-04-25
            if stem.startswith("alpha_report_"):
                candidate = stem[len("alpha_report_"):]
                if _DATE_RE.match(candidate):
                    dates.append(candidate)
    return {"count": len(dates), "dates": dates}


@app.get("/reports/{date}", tags=["reports"])
def report_for_date(
    date: str,
    request: Request,
    api_key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Return the daily report for the given YYYY-MM-DD.

    Free-tier accounts may request only dates within the last
    `MAX_FREE_TIER_DAYS` (default: today and the previous two).
    Older dates → 403 with the documented upgrade message.
    """
    _validate_date(date)
    _enforce_free_limits(
        is_premium=_is_premium(api_key, request=request),
        date_requested=date,
        request=request,
        api_key=api_key,
    )
    path = _reports_dir() / f"alpha_report_{date}.json"
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no report for {date}",
        )
    return _sanitize_report(_parse_report_file(path))


@app.get("/experiments/recent", tags=["experiments"])
def recent_experiments(
    request: Request,
    limit: int = Query(10, ge=1, le=100),
    api_key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Return the last N experiment manifest records.

    - Default `limit=10`, maximum `100`.
    - Free tier: when no `?limit=` is supplied, results are
      silently capped at MAX_FREE_TIER_EXPERIMENTS (3). When a free
      caller EXPLICITLY passes `?limit=` greater than the cap they
      get a 403 with the documented upgrade message — this
      distinguishes "I just want recent activity" from "I want more
      than my tier allows".
    - Empty manifest → `{"count": 0, "records": []}`.
    """
    is_premium = _is_premium(api_key, request=request)
    # Detect EXPLICIT use of the query param via raw query string —
    # FastAPI cannot distinguish a default from an explicit value
    # equal to the default.
    explicit_limit = limit if "limit" in request.query_params else None
    _enforce_free_limits(
        is_premium=is_premium,
        explicit_limit=explicit_limit,
        request=request,
        api_key=api_key,
    )
    effective_limit = (
        limit if is_premium else min(limit, MAX_FREE_TIER_EXPERIMENTS)
    )
    records = _read_manifest_records(_manifest_path())
    tail = records[-effective_limit:] if effective_limit > 0 else records
    sanitized = [_sanitize_manifest(r) for r in tail]
    return {"count": len(sanitized), "records": sanitized}


@app.get("/experiments/{n}", tags=["experiments"])
def experiment_by_index(
    n: int,
    request: Request,
    api_key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Return the nth-most-recent experiment manifest record.

    - `n=1` is the most recent; `n=2` is the one before; etc.
    - Free tier: `n` must be ≤ `MAX_FREE_TIER_EXPERIMENTS` (3);
      otherwise 403.
    """
    if n < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="n must be >= 1 (1 = most recent)",
        )
    _enforce_free_limits(
        is_premium=_is_premium(api_key, request=request),
        n_experiments=n,
        request=request,
        api_key=api_key,
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
  .free-tier-banner {
    background: #fff7e6;
    border: 1px solid #f0c36d;
    color: #7a4a00;
    padding: 0.6em 0.9em;
    margin: 1em 0;
    border-radius: 4px;
    font-weight: 600;
  }
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


def _render_insights_block(insights: Optional[list[dict]]) -> str:
    """
    Phase 9.1 — small dashboard section listing the precomputed
    insights. Pure HTML; the caller has already truncated the
    ``insights`` list to the appropriate tier.
    """
    if not insights:
        return ""
    items: list[str] = []
    for entry in insights:
        if not isinstance(entry, dict):
            continue
        title = _esc(str(entry.get("title", "")))
        summary = _esc(str(entry.get("summary", "")))
        action = _esc(str(entry.get("action", "")))
        severity = _esc(str(entry.get("severity", "info")))
        items.append(
            f'<li class="insight insight-{severity}">'
            f'<strong>{title}</strong>'
            f'<p class="insight-summary">{summary}</p>'
            f'<p class="insight-action">{action}</p>'
            f"</li>"
        )
    if not items:
        return ""
    return (
        "<section>"
        "<h2>Insights</h2>"
        f'<ul class="insights">{"".join(items)}</ul>'
        "</section>"
    )


def render_dashboard_html(
    report: Optional[dict],
    experiments: list[dict],
    *,
    tier: str = TIER_PREMIUM,
    banner_copy: Optional[str] = None,
    insights: Optional[list[dict]] = None,
) -> str:
    """
    Build the dashboard HTML from sanitized inputs.

    Pure-ish function: the only I/O is a single ``os.getenv`` lookup
    when ``banner_copy`` is left ``None`` (default), at which point
    the renderer resolves the Phase 5.7 ``TRADING_UPGRADE_BANNER_COPY``
    env var. Pass an explicit ``banner_copy`` from a test to make
    the call fully deterministic.

    The caller must pass already-sanitized report and experiments
    dicts (via `_sanitize_report` / `_sanitize_manifest`). Because
    of that contract, no amount of upstream leakage can spill into
    the HTML — this renderer simply cannot access fields that have
    already been stripped.

    `tier` controls the Phase 4.5 free-tier display rules:
      - "premium" (default): full data, every section.
      - "free": shadow_filter_simulation section is hidden, and the
        recent-experiments table is capped at MAX_FREE_TIER_EXPERIMENTS.
    """
    is_free = tier == TIER_FREE
    generated_at = _esc(datetime.now(timezone.utc).isoformat())

    # Free tier: cap experiments at the documented limit BEFORE
    # rendering. Newest entries win because the renderer reverses.
    if is_free and experiments:
        experiments = experiments[-MAX_FREE_TIER_EXPERIMENTS:]

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
        # Shadow-filter section is hidden for free tier.
        shadow_block = (
            ""
            if is_free
            else (
                "<h3>Shadow filter simulation</h3>"
                f"{_render_shadow_sim(report)}"
            )
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
            f"{shadow_block}"
            "</section>"
        )

    experiments_block = (
        "<section>"
        "<h2>Recent experiments</h2>"
        f"{_render_experiments(experiments)}"
        + (
            f'<p class="meta">Free tier — capped at '
            f"{MAX_FREE_TIER_EXPERIMENTS} rows. Upgrade for full history.</p>"
            if is_free else ""
        )
        + "</section>"
    )

    # Phase 5.4 — free-tier nudge banner. Rendered before the first
    # report block so it's the first thing a free user sees when
    # they load the dashboard. Phase 5.7: copy is operator-tunable
    # via ``TRADING_UPGRADE_BANNER_COPY`` and HTML-escaped at render
    # time.
    if is_free:
        resolved_copy = (
            banner_copy
            if isinstance(banner_copy, str) and banner_copy
            else _upgrade_banner_copy()
        )
        free_tier_banner = (
            '<p class="free-tier-banner">'
            f"{_esc(resolved_copy)}"
            "</p>"
        )
    else:
        free_tier_banner = ""

    insights_block = _render_insights_block(insights)

    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<title>Momentum Trading Bot — Analytics Dashboard</title>"
        f"{_DASHBOARD_CSS}"
        "</head><body>"
        "<h1>Momentum Trading Bot — Analytics Dashboard</h1>"
        f'<p class="meta">Read-only view. Generated at {generated_at}.</p>'
        f"{free_tier_banner}"
        f"{report_block}"
        f"{insights_block}"
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
)
def dashboard(
    request: Request,
    api_key: str = Depends(require_api_key),
) -> HTMLResponse:
    """
    Read-only HTML dashboard rendering the most recent daily report
    plus the last handful of experiment-manifest records.

    Renders gracefully when the reports directory or manifest are
    missing: the page still returns 200 with explicit empty states.
    Never exposes scorer_config, filesystem paths, raw webhook URL,
    or any execution control.

    Phase 4.5 free-tier rendering:
      - The shadow-filter simulation section is hidden.
      - The recent-experiments table is capped at
        `MAX_FREE_TIER_EXPERIMENTS` rows with a small upgrade note.
    """
    # Load latest report (best-effort — empty state wins on any error).
    report: Optional[dict] = None
    reports = _reports_dir()
    prev_report: Optional[dict] = None
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
            # Phase 9.1 — best-effort prior-day load for the trend
            # insight. Failure modes (missing / corrupt / unreadable)
            # all collapse to ``None`` so the trend rule simply
            # skips itself.
            if len(candidates) >= 2:
                try:
                    prev_raw = json.loads(
                        candidates[-2].read_text(encoding="utf-8"),
                    )
                    if isinstance(prev_raw, dict):
                        prev_report = _sanitize_report(prev_raw)
                except Exception as exc:
                    log.debug(
                        "dashboard.prev_report_load_failed",
                        path=str(candidates[-2]), error=str(exc),
                    )

    # Load last 10 experiments.
    records = _read_manifest_records(_manifest_path())
    experiments = [_sanitize_manifest(r) for r in records[-10:]] if records else []

    is_premium = _is_premium_user(request)
    tier = TIER_PREMIUM if is_premium else TIER_FREE
    # Phase 9.1 — compute insights from the FULL report (before the
    # free-tier projection trims regime_stats / etc.) so the rules
    # see every signal. Truncation for free callers happens after.
    insights = build_insights(report, prev_report) if report is not None else []
    # Phase 8.2 — when the caller is on the free tier, pass the
    # report through the same allow-list that gates /reports/latest
    # so the dashboard cannot accidentally surface the deep tier /
    # decile / shadow-filter sections that are part of the premium
    # offering. Premium callers see the full sanitised report.
    if not is_premium and report is not None:
        report = _project_report_for_free(report)
    if not is_premium:
        insights = truncate_for_free(insights)
    # Phase 5.7 + 5.8: resolve the banner copy ONCE so the exact
    # string rendered to the user is also the one we hash into the
    # telemetry row. Premium users skip the resolver entirely (no
    # banner, no event emission).
    banner_copy = _upgrade_banner_copy() if tier == TIER_FREE else None
    html = render_dashboard_html(
        report, experiments, tier=tier, banner_copy=banner_copy,
        insights=insights,
    )
    if tier == TIER_FREE:
        # Phase 5.5 + 5.8 — the banner is rendered in the HTML
        # above; emit one telemetry row per dashboard load so we
        # can measure how often free users actually see the
        # upgrade prompt. The resolved copy is hashed into the
        # row's ``copy_variant_hash`` field.
        _emit_upgrade_event(
            request, api_key, "dashboard_banner_seen",
            copy_variant=banner_copy,
        )
    return HTMLResponse(content=html, status_code=200)


# Nothing below this line. The api module deliberately imports
# nothing from trading_bot.core, trading_bot.main, or any execution
# path — that constraint is verified structurally by tests.
