"""
Phase 10.4 — tokenised public share links for individual signals.

Extends the Phase 10.1/10.3 shareability layer (``share.py`` copy
builders + ``share_events.py`` JSONL telemetry) with durable,
token-addressable share links: an authenticated user mints a token
for one signal, and anyone holding the token can fetch a SANITISED
snapshot of that signal — no API key required.

Sanitisation contract (the whole point of this module):

  * The public payload contains ONLY
    ``{symbol, direction, score, gap_pct, regime, date,
    referrer_label}``.
  * ``entry`` / ``stop_loss`` / ``take_profit`` / ``indicators`` /
    ``rationale`` — the premium fields — are snapshotted NOWHERE.
    The link record itself stores only the sanitised shape, so a
    future bug in the read path cannot leak what was never written.
  * The only optional person-adjacent field is ``referrer_label``,
    a display label the SHARER chooses (capped + character-stripped);
    no email, no IP, no user-agent, no raw API key ever enters a
    record — the creator is stored as the usual SHA-256[:32] hash.

Storage: append-only JSONL (same posture as ``share_events.py`` /
the key manifest). The last record for a token wins, records carry
an ``expires_at`` and lookups treat expired records as missing.

Env vars:

    TRADING_API_SHARE_LINKS_PATH   JSONL store
                                   (default: data/api_share_links.jsonl)
    TRADING_SHARE_LINK_TTL_DAYS    link lifetime in days (default: 30)

This module imports nothing from FastAPI so it stays importable
from CLIs, tests, and offline analysis tools.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SHARE_LINKS_PATH_ENV_VAR = "TRADING_API_SHARE_LINKS_PATH"
DEFAULT_SHARE_LINKS_PATH = "data/api_share_links.jsonl"

SHARE_LINK_TTL_ENV_VAR = "TRADING_SHARE_LINK_TTL_DAYS"
DEFAULT_SHARE_LINK_TTL_DAYS = 30

#: URL-safe token alphabet — matches ``secrets.token_urlsafe`` output
#: and the share-events ``src`` sanitiser's safe set.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

#: Number of random bytes behind each token (16 bytes → 22 chars,
#: 128 bits of entropy — unguessable, comfortably short for a URL).
_TOKEN_BYTES = 16

#: Display-label sanitiser: letters, digits, spaces and light
#: punctuation only; hard cap keeps the card layout and the JSONL
#: rows bounded.
REFERRER_LABEL_MAX_LENGTH = 40
_LABEL_STRIP_RE = re.compile(r"[^A-Za-z0-9 ._'\-]")

#: Directions copied verbatim from the SaaS strategy module — kept
#: as a local allow-list so a corrupt report can never inject
#: arbitrary strings into the public payload.
_VALID_DIRECTIONS: frozenset[str] = frozenset({
    "bullish", "bearish", "neutral",
})

#: Fields that must NEVER appear in a public share payload. Used by
#: the sanitiser's belt-and-braces final check (and by tests).
FORBIDDEN_PUBLIC_FIELDS: frozenset[str] = frozenset({
    "entry", "stop_loss", "take_profit", "indicators", "rationale",
    "api_key", "api_key_hash", "email",
})

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def links_path() -> Path:
    return Path(
        os.getenv(SHARE_LINKS_PATH_ENV_VAR, DEFAULT_SHARE_LINKS_PATH)
    )


def link_ttl_days() -> int:
    raw = (os.getenv(SHARE_LINK_TTL_ENV_VAR) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SHARE_LINK_TTL_DAYS
    return value if value > 0 else DEFAULT_SHARE_LINK_TTL_DAYS


def generate_token() -> str:
    """128-bit URL-safe token; always matches ``TOKEN_RE``."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def sanitize_token(raw: Optional[str]) -> Optional[str]:
    """Return the token if well-formed, else ``None`` (fail closed)."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    return s if TOKEN_RE.match(s) else None


def sanitize_referrer_label(raw: Optional[str]) -> Optional[str]:
    """
    Strip a sharer-supplied display label to the safe character set
    and cap its length. Empty / non-string input → ``None`` (the
    field is optional everywhere downstream).
    """
    if not isinstance(raw, str):
        return None
    cleaned = _LABEL_STRIP_RE.sub("", raw).strip()
    cleaned = cleaned[:REFERRER_LABEL_MAX_LENGTH].strip()
    return cleaned or None


def _now(now: Optional[datetime] = None) -> datetime:
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _safe_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


# ---------------------------------------------------------------------------
# Sanitiser — full signal + report context → public card payload
# ---------------------------------------------------------------------------


def derive_regime(summary: Optional[dict]) -> str:
    """
    Derive a market-regime label from the report summary's real
    bullish/bearish counts. Deliberately coarse — it is a public
    teaser, not a premium analytic.
    """
    if not isinstance(summary, dict):
        return "mixed"
    try:
        bull = int(summary.get("bullish_count") or 0)
    except (TypeError, ValueError):
        bull = 0
    try:
        bear = int(summary.get("bearish_count") or 0)
    except (TypeError, ValueError):
        bear = 0
    if bull > bear:
        return "risk-on"
    if bear > bull:
        return "risk-off"
    return "mixed"


def build_public_signal(
    signal: dict,
    *,
    report: Optional[dict] = None,
    referrer_label: Optional[str] = None,
) -> dict:
    """
    Project one FULL signal dict (Phase SaaS ``Signal.to_dict``
    shape) down to the public share card payload::

        {
          "symbol":         "NVDA",
          "direction":      "bullish" | "bearish" | "neutral",
          "score":          0-100 int (confidence × 100),
          "gap_pct":        float | None (momentum % vs 50-day SMA),
          "regime":         "risk-on" | "risk-off" | "mixed",
          "date":           "YYYY-MM-DD",
          "referrer_label": str | None,
        }

    NO entry / stop / target / indicators / rationale — premium
    data stays paid. The final assertion is a belt-and-braces
    guarantee that a future edit cannot silently widen the payload.
    """
    if not isinstance(signal, dict):
        raise ValueError("signal must be a dict")
    rep = report if isinstance(report, dict) else {}

    symbol = str(signal.get("symbol") or "").strip().upper()[:8] or "?"

    direction = str(signal.get("direction") or "").strip().lower()
    if direction not in _VALID_DIRECTIONS:
        direction = "neutral"

    confidence = _safe_float(signal.get("confidence")) or 0.0
    score = int(round(max(0.0, min(1.0, confidence)) * 100))

    indicators = signal.get("indicators")
    momentum = None
    if isinstance(indicators, dict):
        momentum = _safe_float(indicators.get("momentum_pct"))
    gap_pct = round(momentum * 100, 2) if momentum is not None else None

    date = str(rep.get("report_date") or "").strip()[:10] or None

    public = {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "gap_pct": gap_pct,
        "regime": derive_regime(rep.get("summary")),
        "date": date,
        "referrer_label": sanitize_referrer_label(referrer_label),
    }
    leaked = FORBIDDEN_PUBLIC_FIELDS.intersection(public)
    if leaked:  # pragma: no cover — structural guarantee
        raise RuntimeError(f"public share payload leaked fields: {leaked}")
    return public


# ---------------------------------------------------------------------------
# Store — append-only JSONL, last record per token wins
# ---------------------------------------------------------------------------


def create_share_link(
    *,
    key_hash: str,
    signal: dict,
    report: Optional[dict] = None,
    referrer_label: Optional[str] = None,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> dict:
    """
    Mint a share token for one signal and persist the SANITISED
    snapshot. Raises ``ValueError`` on a missing creator hash and
    ``OSError`` when the store cannot be written — link creation
    (unlike telemetry) must fail loudly: handing out a token that
    was never persisted would 404 for every recipient.

    Returns the full stored record (including ``token``).
    """
    if not isinstance(key_hash, str) or not key_hash.strip():
        raise ValueError("key_hash is required (pre-hashed, never raw)")

    created = _now(now)
    record = {
        "schema_version": "share-link-v1",
        "token": generate_token(),
        "created_at": _iso(created),
        "expires_at": _iso(created + timedelta(days=link_ttl_days())),
        "api_key_hash": key_hash.strip(),
        "signal": build_public_signal(
            signal, report=report, referrer_label=referrer_label,
        ),
    }
    target = path if path is not None else links_path()
    line = json.dumps(record, sort_keys=False, default=str)
    with _write_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return record


def _read_records(path: Path) -> list[dict]:
    """Load the JSONL store. Missing / corrupt file → []."""
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def lookup_share_link(
    token: Optional[str],
    *,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> Optional[dict]:
    """
    Resolve a token to its stored record.

    Returns ``None`` for malformed tokens, unknown tokens, and
    EXPIRED records — the caller cannot distinguish the three, by
    design (no existence oracle). The last record for a token wins.
    """
    clean = sanitize_token(token)
    if clean is None:
        return None
    target = path if path is not None else links_path()
    found: Optional[dict] = None
    for rec in _read_records(target):
        if rec.get("token") == clean:
            found = rec
    if found is None:
        return None
    expires = _parse_iso(found.get("expires_at"))
    if expires is None or _now(now) >= expires:
        return None
    if not isinstance(found.get("signal"), dict):
        return None
    return found
