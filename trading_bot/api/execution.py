"""
Phase 11 — Execution Layer.

Safely applies the highest-priority Phase 10.5 recommendation
under deterministic, reversible guardrails. Today only one
recommendation kind is *mechanically* actionable —
``tighten_free_limit`` — and even that change happens via an
**in-process** ``os.environ`` mutation: nothing on disk changes
except a single audit row in
``data/api_execution_log.jsonl``.

Every other recommendation kind (``amplify_top_trigger``,
``feature_top_insight``, ``double_down_best_source``,
``insufficient_data``) describes a marketing / copy / dashboard
action that requires a human; the execution layer surfaces them
as "informational" without trying to apply them automatically.

Operator workflow::

    # Read-only dry-run — print the plan, no side effects.
    python -m trading_bot.api.execution --apply

    # Actually apply: mutate os.environ in this process and
    # append one row to the action log.
    python -m trading_bot.api.execution --apply --force

The mutation is *transient*: it survives only as long as the
calling process. Operators who want a rolling-restart-safe
change must update their deployment env file separately. This
keeps the CLI's blast radius bounded — a misclick is undone the
moment the shell exits.

Guardrails
----------

* The free-tier daily request limit is the only knob the layer
  is willing to touch. Bounds are hard-coded:

    - absolute: 10 ≤ new ≤ 200
    - relative: |new − current| / current ≤ 0.30

  Both checks fire BEFORE the action log is written; a rejected
  plan still appears in the log (for audit) but with
  ``outcome="rejected_..."`` and the env var is *not* set.

* The reduction step for ``tighten_free_limit`` is fixed at 25 %
  (rounded to the nearest int) — well inside the ±30 % cap.
  Future rules can pick a different step size; the bound check
  is the same.

* ``--apply`` without ``--force`` writes nothing to disk and does
  not touch ``os.environ``. It exists so an operator can preview
  the action.

* No raw API key, no per-user identifier, no ``key_hash`` ever
  enters the action log: the layer reads aggregated metrics from
  ``growth_intel.summarize`` only.

Env vars (read-only)::

    TRADING_API_EXECUTION_LOG_PATH    JSONL audit log
                                      (default: data/api_execution_log.jsonl)

The free-limit env vars the layer mutates are read at plan time
and printed in the dry-run output:

    TRADING_FREE_DAILY_REQUEST_LIMIT  primary knob (Phase 8.1)
    TRADING_FREE_MAX_REQUESTS_PER_DAY legacy alias (Phase 5.4)

CLI::

    python -m trading_bot.api.execution --apply
    python -m trading_bot.api.execution --apply --force
    python -m trading_bot.api.execution --apply --json
    python -m trading_bot.api.execution --apply --upgrade-path ... --share-path ...
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import structlog

from trading_bot.api.growth_intel import (
    DEFAULT_MIN_IMPRESSIONS,
    DEFAULT_MIN_INBOUND,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    REC_AMPLIFY_TOP_TRIGGER,
    REC_DOUBLE_DOWN_SOURCE,
    REC_FEATURE_TOP_INSIGHT,
    REC_INSUFFICIENT_DATA,
    REC_TIGHTEN_FREE_LIMIT,
    generate_recommendations,
    summarize,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration — env vars + bounds + step size.
# ---------------------------------------------------------------------------

EXECUTION_LOG_ENV_VAR = "TRADING_API_EXECUTION_LOG_PATH"
DEFAULT_EXECUTION_LOG_PATH = "data/api_execution_log.jsonl"

#: The Phase 8.1 free-tier daily request cap. The execution layer
#: writes this env var on a successful ``--force`` apply.
FREE_LIMIT_ENV_VAR = "TRADING_FREE_DAILY_REQUEST_LIMIT"

#: Default value used when ``$FREE_LIMIT_ENV_VAR`` is unset. Mirrors
#: ``server.DEFAULT_FREE_DAILY_REQUEST_LIMIT`` so a fresh deployment
#: plans against the same baseline the server actually uses.
DEFAULT_FREE_LIMIT = 50

#: Hard absolute bounds on the free limit. Nothing below ``MIN``
#: (the free tier becomes useless) and nothing above ``MAX`` (the
#: SaaS becomes a free service).
FREE_LIMIT_MIN = 10
FREE_LIMIT_MAX = 200

#: Maximum proportional change the layer will accept in one apply
#: step. Limits blast radius — a runaway recommendation engine
#: cannot collapse the free tier in one click.
MAX_CHANGE_PCT = 0.30

#: Step size the ``tighten_free_limit`` rule applies on each pass:
#: 25 % reduction, rounded to the nearest integer. Sits comfortably
#: inside the ±30 % cap so the action always passes the bound
#: check.
TIGHTEN_REDUCTION_PCT = 0.25


# Outcome codes recorded on each action log row + returned by the
# planner. Pin them as public constants so dashboards and audit
# scripts can branch on them deterministically.
OUTCOME_APPLIED = "applied"
OUTCOME_DRY_RUN = "dry_run"
OUTCOME_REJECTED_OUT_OF_BOUNDS = "rejected_out_of_bounds"
OUTCOME_REJECTED_CHANGE_TOO_LARGE = "rejected_change_too_large"
OUTCOME_REJECTED_NO_CHANGE = "rejected_no_change"
OUTCOME_NO_ACTIONABLE = "no_actionable_recommendation"

VALID_OUTCOMES: frozenset[str] = frozenset({
    OUTCOME_APPLIED,
    OUTCOME_DRY_RUN,
    OUTCOME_REJECTED_OUT_OF_BOUNDS,
    OUTCOME_REJECTED_CHANGE_TOO_LARGE,
    OUTCOME_REJECTED_NO_CHANGE,
    OUTCOME_NO_ACTIONABLE,
})

#: Stable action types. Today only ``set_free_limit`` is wired up;
#: future rules add more.
ACTION_SET_FREE_LIMIT = "set_free_limit"
ACTION_NONE = "none"

VALID_ACTIONS: frozenset[str] = frozenset({
    ACTION_SET_FREE_LIMIT,
    ACTION_NONE,
})

#: The set of recommendation ids the layer is willing to apply
#: mechanically. Anything outside this set is treated as
#: informational and surfaced to the operator without action.
_APPLICABLE_REC_IDS: frozenset[str] = frozenset({
    REC_TIGHTEN_FREE_LIMIT,
})


_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _execution_log_path() -> Path:
    return Path(
        os.getenv(EXECUTION_LOG_ENV_VAR, DEFAULT_EXECUTION_LOG_PATH)
    )


def _now_iso_utc(now: Optional[datetime] = None) -> str:
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _current_free_limit() -> int:
    """Read the live free-limit env var with the same parsing rule
    the server uses."""
    raw = os.getenv(FREE_LIMIT_ENV_VAR)
    if raw is None:
        return DEFAULT_FREE_LIMIT
    s = str(raw).strip()
    if not s:
        return DEFAULT_FREE_LIMIT
    try:
        value = int(s)
    except (TypeError, ValueError):
        return DEFAULT_FREE_LIMIT
    if value <= 0:
        return DEFAULT_FREE_LIMIT
    return value


# ---------------------------------------------------------------------------
# Planner — pure: derives a plan from a recommendation, no I/O.
# ---------------------------------------------------------------------------


def load_recommendations(
    *,
    upgrade_path: Union[str, Path, None] = None,
    share_path: Union[str, Path, None] = None,
    min_impressions: int = DEFAULT_MIN_IMPRESSIONS,
    min_inbound: int = DEFAULT_MIN_INBOUND,
) -> list[dict]:
    """
    Convenience: build a Phase 10.4 summary and run the Phase 10.5
    rules in one call. Reads only the existing JSONL logs.
    """
    summary = summarize(
        upgrade_path=upgrade_path,
        share_path=share_path,
        min_impressions=min_impressions,
        min_inbound=min_inbound,
    )
    return generate_recommendations(summary)


def pick_actionable(recommendations: list[dict]) -> Optional[dict]:
    """
    Return the highest-priority recommendation the layer is
    willing to apply mechanically, or ``None`` when no entry on
    the list is actionable.

    The Phase 10.5 list is already sorted HIGH → MEDIUM → LOW with
    stable rule order inside each priority bucket, so the first
    actionable entry is the right one.
    """
    if not isinstance(recommendations, list):
        return None
    for rec in recommendations:
        if not isinstance(rec, dict):
            continue
        if rec.get("id") in _APPLICABLE_REC_IDS:
            return rec
    return None


def _plan_set_free_limit(
    rec: dict,
    *,
    current_value: Optional[int] = None,
) -> dict:
    """
    Build the concrete plan for ``tighten_free_limit``: take the
    live ``$FREE_LIMIT_ENV_VAR``, apply ``TIGHTEN_REDUCTION_PCT``,
    round to the nearest integer.
    """
    current = (
        int(current_value)
        if current_value is not None else _current_free_limit()
    )
    reduced = max(1, int(round(current * (1 - TIGHTEN_REDUCTION_PCT))))
    delta = reduced - current
    delta_pct = (delta / current) if current else 0.0

    return {
        "recommendation_id": rec.get("id"),
        "priority": rec.get("priority"),
        "action": ACTION_SET_FREE_LIMIT,
        "env_var": FREE_LIMIT_ENV_VAR,
        "previous_value": current,
        "new_value": reduced,
        "delta": delta,
        "delta_pct": round(delta_pct, 4),
        "rationale": rec.get("rationale", ""),
    }


def plan_action(
    rec: Optional[dict],
    *,
    current_value: Optional[int] = None,
) -> Optional[dict]:
    """
    Build a concrete action plan from an actionable recommendation.
    Returns ``None`` when ``rec`` is missing or not in the
    applicable set. Pure: no side effects.
    """
    if not isinstance(rec, dict):
        return None
    rid = rec.get("id")
    if rid not in _APPLICABLE_REC_IDS:
        return None
    if rid == REC_TIGHTEN_FREE_LIMIT:
        return _plan_set_free_limit(rec, current_value=current_value)
    # Unreachable today (only one applicable id); future rules
    # plug in here.
    return None


def validate_bounds(plan: dict) -> dict:
    """
    Apply the Phase 11 guardrails to a plan. Returns
    ``{"ok": True}`` on pass, otherwise
    ``{"ok": False, "outcome": "rejected_...", "reason": "..."}``.

    Three checks, ordered most-specific-first so the most useful
    rejection reason wins:

      1. ``OUTCOME_REJECTED_NO_CHANGE`` — the plan would be a
         no-op (new == previous). Treated as rejection so the CLI
         doesn't pretend it acted.
      2. ``OUTCOME_REJECTED_OUT_OF_BOUNDS`` — new value falls
         outside ``[FREE_LIMIT_MIN, FREE_LIMIT_MAX]``.
      3. ``OUTCOME_REJECTED_CHANGE_TOO_LARGE`` — relative change
         exceeds ``MAX_CHANGE_PCT``.
    """
    if not isinstance(plan, dict):
        return {
            "ok": False,
            "outcome": OUTCOME_REJECTED_NO_CHANGE,
            "reason": "no plan to validate",
        }

    if plan.get("action") != ACTION_SET_FREE_LIMIT:
        return {
            "ok": False,
            "outcome": OUTCOME_REJECTED_NO_CHANGE,
            "reason": f"action {plan.get('action')!r} is not applicable",
        }

    new_value = plan.get("new_value")
    previous = plan.get("previous_value")
    if not isinstance(new_value, int) or not isinstance(previous, int):
        return {
            "ok": False,
            "outcome": OUTCOME_REJECTED_NO_CHANGE,
            "reason": "plan is missing previous_value or new_value",
        }

    if new_value == previous:
        return {
            "ok": False,
            "outcome": OUTCOME_REJECTED_NO_CHANGE,
            "reason": "proposed value equals current value",
        }

    if new_value < FREE_LIMIT_MIN or new_value > FREE_LIMIT_MAX:
        return {
            "ok": False,
            "outcome": OUTCOME_REJECTED_OUT_OF_BOUNDS,
            "reason": (
                f"new_value {new_value} outside "
                f"[{FREE_LIMIT_MIN}, {FREE_LIMIT_MAX}]"
            ),
        }

    if previous <= 0:
        return {
            "ok": False,
            "outcome": OUTCOME_REJECTED_OUT_OF_BOUNDS,
            "reason": (
                f"previous_value {previous} is non-positive — "
                "cannot compute relative change"
            ),
        }

    delta_pct = abs(new_value - previous) / previous
    if delta_pct > MAX_CHANGE_PCT:
        return {
            "ok": False,
            "outcome": OUTCOME_REJECTED_CHANGE_TOO_LARGE,
            "reason": (
                f"|delta|/previous = {round(delta_pct, 4)} "
                f"exceeds MAX_CHANGE_PCT = {MAX_CHANGE_PCT}"
            ),
        }

    return {"ok": True}


# ---------------------------------------------------------------------------
# Action log writer.
# ---------------------------------------------------------------------------


def _build_log_record(
    plan: dict,
    *,
    outcome: str,
    rejection_reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """
    Construct the documented JSONL row. The record carries no
    ``api_key_hash`` field by design — the execution layer
    operates on aggregated metrics, not per-user data.
    """
    record = {
        "timestamp": _now_iso_utc(now),
        "recommendation_id": plan.get("recommendation_id"),
        "priority": plan.get("priority"),
        "action": plan.get("action"),
        "env_var": plan.get("env_var"),
        "previous_value": plan.get("previous_value"),
        "new_value": plan.get("new_value"),
        "delta": plan.get("delta"),
        "delta_pct": plan.get("delta_pct"),
        "outcome": outcome,
    }
    if rejection_reason:
        record["rejection_reason"] = str(rejection_reason)[:200]
    return record


def _append_log_record(
    record: dict,
    *,
    target: Optional[Path] = None,
) -> None:
    """Thread-safe JSONL append. Best-effort: a disk failure logs
    DEBUG and is swallowed so the CLI exit code still reflects
    the action's outcome rather than the audit-log's health."""
    path = target if target is not None else _execution_log_path()
    try:
        line = json.dumps(record, sort_keys=False, default=str)
    except Exception as exc:  # noqa: BLE001
        log.debug("execution.serialize_error", error=str(exc))
        return
    with _write_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "execution.write_error",
                path=str(path), error=str(exc),
            )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_action(
    plan: Optional[dict],
    *,
    force: bool = False,
    now: Optional[datetime] = None,
    log_target: Optional[Path] = None,
) -> dict:
    """
    Apply a plan under the Phase 11 guardrails.

    Returns a small status dict::

        {
          "outcome":          str,          # OUTCOME_*
          "applied":          bool,         # env var actually set
          "logged":           bool,         # action-log row written
          "plan":             plan | None,
          "rejection_reason": str | None,
        }

    Behaviour matrix:

      * ``plan is None``             → outcome=no_actionable_recommendation,
                                       no log row, no env mutation.
      * Any rejected validation       → outcome=rejected_*, log row IS
                                        written when ``force=True`` so
                                        the audit trail captures the
                                        rejected attempt; never
                                        mutates env.
      * ``force=False`` (dry run)     → outcome=dry_run, no log row,
                                        no env mutation.
      * ``force=True`` and validates  → outcome=applied, log row
                                        written, env var set in the
                                        current process only.
    """
    if plan is None:
        return {
            "outcome": OUTCOME_NO_ACTIONABLE,
            "applied": False,
            "logged": False,
            "plan": None,
            "rejection_reason": None,
        }

    check = validate_bounds(plan)
    if not check.get("ok"):
        outcome = check.get("outcome", OUTCOME_REJECTED_NO_CHANGE)
        reason = check.get("reason")
        logged = False
        if force:
            _append_log_record(
                _build_log_record(
                    plan, outcome=outcome,
                    rejection_reason=reason, now=now,
                ),
                target=log_target,
            )
            logged = True
        return {
            "outcome": outcome,
            "applied": False,
            "logged": logged,
            "plan": plan,
            "rejection_reason": reason,
        }

    if not force:
        return {
            "outcome": OUTCOME_DRY_RUN,
            "applied": False,
            "logged": False,
            "plan": plan,
            "rejection_reason": None,
        }

    # ----- Force apply -----
    env_var = plan.get("env_var")
    new_value = plan.get("new_value")
    if env_var and isinstance(new_value, int):
        # In-process mutation only. Survives the lifetime of this
        # Python process and no longer.
        os.environ[str(env_var)] = str(new_value)

    _append_log_record(
        _build_log_record(plan, outcome=OUTCOME_APPLIED, now=now),
        target=log_target,
    )
    return {
        "outcome": OUTCOME_APPLIED,
        "applied": True,
        "logged": True,
        "plan": plan,
        "rejection_reason": None,
    }


# ---------------------------------------------------------------------------
# Pretty-printing
# ---------------------------------------------------------------------------


def _fmt_pct(value: float) -> str:
    return f"{(value * 100):+.1f}%"


def format_status_text(status: dict, *, force: bool) -> str:
    """Render an ``apply_action`` status dict as plain text."""
    bar = "=" * 72
    lines: list[str] = []
    lines.append(bar)
    lines.append("EXECUTION LAYER")
    lines.append(bar)

    plan = status.get("plan")
    outcome = status.get("outcome", "?")

    if plan is None:
        lines.append(
            "no actionable recommendation — "
            "Phase 10.5 returned no rule the execution layer can "
            "apply mechanically."
        )
        lines.append(bar)
        return "\n".join(lines)

    lines.append(
        f"recommendation : {plan.get('recommendation_id', '?')} "
        f"[{str(plan.get('priority', '?')).upper()}]"
    )
    lines.append(f"action         : {plan.get('action', '?')}")
    if plan.get("env_var"):
        lines.append(f"env_var        : {plan['env_var']}")
    lines.append(
        f"previous_value : {plan.get('previous_value', '?')}"
    )
    lines.append(
        f"new_value      : {plan.get('new_value', '?')}"
    )
    lines.append(
        f"delta          : "
        f"{plan.get('delta', '?')} "
        f"({_fmt_pct(plan.get('delta_pct', 0.0))})"
    )
    if plan.get("rationale"):
        lines.append(f"rationale      : {plan['rationale']}")
    lines.append("")

    mode = "FORCE" if force else "DRY-RUN"
    lines.append(f"mode           : {mode}")
    lines.append(f"outcome        : {outcome}")
    if status.get("rejection_reason"):
        lines.append(
            f"rejection      : {status['rejection_reason']}"
        )
    lines.append(
        f"applied        : "
        f"{'yes' if status.get('applied') else 'no'}"
    )
    lines.append(
        f"logged         : "
        f"{'yes' if status.get('logged') else 'no'}"
    )

    if not force and outcome == OUTCOME_DRY_RUN:
        lines.append("")
        lines.append(
            "(dry-run — re-run with --force to apply. The change "
            "is process-local; update your deployment env file "
            "for a rolling-restart-safe rollout.)"
        )

    lines.append(bar)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.execution",
        description=(
            "Phase 11 execution layer — apply the highest-priority "
            "Phase 10.5 recommendation under deterministic bounds. "
            "Read-only by default; pass --force to mutate the live "
            "env var and append one row to the action log. The "
            "free-tier limit is the only knob the layer is willing "
            "to touch."
        ),
    )
    parser.add_argument(
        "--apply", action="store_true",
        help=(
            "Plan + (in dry-run) print the proposed action. "
            "Required: without --apply the CLI prints help and "
            "exits 2."
        ),
    )
    parser.add_argument(
        "--force", action="store_true",
        help=(
            "Combined with --apply, actually mutate "
            "$TRADING_FREE_DAILY_REQUEST_LIMIT in the current "
            "process and append one row to the action log."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a JSON status dict instead of plain text.",
    )
    parser.add_argument(
        "--upgrade-path", default=None,
        help=(
            "Override the upgrade-events JSONL path "
            "(forwarded to growth_intel)."
        ),
    )
    parser.add_argument(
        "--share-path", default=None,
        help=(
            "Override the share-events JSONL path "
            "(forwarded to growth_intel)."
        ),
    )
    return parser


def _exit_code_for(outcome: str) -> int:
    """Map outcome → process exit code so CI / scripts can branch.

      * applied / dry_run            → 0  (success)
      * no_actionable_recommendation → 0  (nothing to do is fine)
      * rejected_*                   → 3  (operator must investigate)
    """
    if outcome in (
        OUTCOME_APPLIED, OUTCOME_DRY_RUN, OUTCOME_NO_ACTIONABLE,
    ):
        return 0
    return 3


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.apply:
        parser.print_help()
        return 2

    recommendations = load_recommendations(
        upgrade_path=args.upgrade_path,
        share_path=args.share_path,
    )
    actionable = pick_actionable(recommendations)
    plan = plan_action(actionable)
    status = apply_action(plan, force=args.force)

    if args.json:
        print(json.dumps(status, indent=2, sort_keys=False, default=str))
    else:
        print(format_status_text(status, force=args.force))

    return _exit_code_for(status.get("outcome", OUTCOME_NO_ACTIONABLE))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
