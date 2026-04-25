"""
Phase 11 tests — Execution Layer.

Covers:
  * dry-run prints + exits 0 without writing the log or mutating env;
  * --force mutates $TRADING_FREE_DAILY_REQUEST_LIMIT and writes one
    correctly-shaped action-log row;
  * absolute bounds (10 ≤ new ≤ 200) are enforced;
  * relative bounds (|delta|/previous ≤ 0.30) are enforced;
  * rejected attempts are logged in --force mode (audit trail) but
    never mutate env;
  * "no actionable recommendation" exits 0 and writes nothing;
  * the action log carries no api_key_hash and no raw-key marker;
  * the module does not reach into Core.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_bot.api import execution as ex
from trading_bot.api.execution import (
    ACTION_NONE,
    ACTION_SET_FREE_LIMIT,
    DEFAULT_EXECUTION_LOG_PATH,
    DEFAULT_FREE_LIMIT,
    EXECUTION_LOG_ENV_VAR,
    FREE_LIMIT_ENV_VAR,
    FREE_LIMIT_MAX,
    FREE_LIMIT_MIN,
    MAX_CHANGE_PCT,
    OUTCOME_APPLIED,
    OUTCOME_DRY_RUN,
    OUTCOME_NO_ACTIONABLE,
    OUTCOME_REJECTED_CHANGE_TOO_LARGE,
    OUTCOME_REJECTED_NO_CHANGE,
    OUTCOME_REJECTED_OUT_OF_BOUNDS,
    TIGHTEN_REDUCTION_PCT,
    VALID_ACTIONS,
    VALID_OUTCOMES,
    apply_action,
    load_recommendations,
    pick_actionable,
    plan_action,
    validate_bounds,
)
from trading_bot.api.growth_intel import (
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    REC_AMPLIFY_TOP_TRIGGER,
    REC_DOUBLE_DOWN_SOURCE,
    REC_FEATURE_TOP_INSIGHT,
    REC_INSUFFICIENT_DATA,
    REC_TIGHTEN_FREE_LIMIT,
)
from trading_bot.api.share_events import (
    EVENT_INBOUND_VISIT,
    EVENT_SHARE_GENERATED,
    SHARE_EVENTS_LOG_ENV_VAR,
)
from trading_bot.api.upgrade_events import (
    EVENT_UPGRADE_COMPLETED,
    EVENT_UPGRADE_SHOWN,
    UPGRADE_EVENTS_LOG_ENV_VAR,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


@pytest.fixture()
def execution_log(monkeypatch, tmp_path: Path) -> Path:
    p = tmp_path / "execution_log.jsonl"
    monkeypatch.setenv(EXECUTION_LOG_ENV_VAR, str(p))
    return p


@pytest.fixture()
def upgrade_log(monkeypatch, tmp_path: Path) -> Path:
    p = tmp_path / "upgrade.jsonl"
    monkeypatch.setenv(UPGRADE_EVENTS_LOG_ENV_VAR, str(p))
    return p


@pytest.fixture()
def share_log(monkeypatch, tmp_path: Path) -> Path:
    p = tmp_path / "share.jsonl"
    monkeypatch.setenv(SHARE_EVENTS_LOG_ENV_VAR, str(p))
    return p


@pytest.fixture()
def restore_free_limit_env(monkeypatch):
    """Make sure no test leaks a mutated free-limit env var."""
    monkeypatch.delenv(FREE_LIMIT_ENV_VAR, raising=False)
    yield
    monkeypatch.delenv(FREE_LIMIT_ENV_VAR, raising=False)


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        out.append(json.loads(s))
    return out


def _seed_strong_funnel(upgrade_log: Path, share_log: Path) -> None:
    """Seed an upgrade log that produces a tighten_free_limit
    recommendation under default thresholds."""
    rows: list[dict] = []
    for i in range(20):
        rows.append({
            "timestamp": "2026-04-25T12:00:00.000000Z",
            "api_key_hash": _hash(f"usage_{i}"),
            "event": EVENT_UPGRADE_SHOWN,
            "tier": "free",
            "request_id": None,
            "reason": "usage_limit",
            "endpoint": "/reports/latest",
        })
    for i in range(10):
        rows.append({
            "timestamp": "2026-04-25T12:01:00.000000Z",
            "api_key_hash": _hash(f"usage_{i}"),
            "event": EVENT_UPGRADE_COMPLETED,
            "tier": "free",
            "request_id": None,
            "reason": "usage_limit",
            "endpoint": "/reports/latest",
        })
    for i in range(20):
        rows.append({
            "timestamp": "2026-04-25T12:00:00.000000Z",
            "api_key_hash": _hash(f"limited_{i}"),
            "event": EVENT_UPGRADE_SHOWN,
            "tier": "free",
            "request_id": None,
            "reason": "limited_access",
            "endpoint": "/reports/latest",
        })
    for i in range(2):
        rows.append({
            "timestamp": "2026-04-25T12:01:00.000000Z",
            "api_key_hash": _hash(f"limited_{i}"),
            "event": EVENT_UPGRADE_COMPLETED,
            "tier": "free",
            "request_id": None,
            "reason": "limited_access",
            "endpoint": "/reports/latest",
        })
    upgrade_log.parent.mkdir(parents=True, exist_ok=True)
    upgrade_log.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    share_log.parent.mkdir(parents=True, exist_ok=True)
    share_log.write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# Constants & schema pinning
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_log_path_under_data(self):
        assert DEFAULT_EXECUTION_LOG_PATH == "data/api_execution_log.jsonl"

    def test_bounds_pinned(self):
        assert FREE_LIMIT_MIN == 10
        assert FREE_LIMIT_MAX == 200
        assert MAX_CHANGE_PCT == 0.30

    def test_outcomes_and_actions_documented(self):
        for code in (
            OUTCOME_APPLIED,
            OUTCOME_DRY_RUN,
            OUTCOME_REJECTED_OUT_OF_BOUNDS,
            OUTCOME_REJECTED_CHANGE_TOO_LARGE,
            OUTCOME_REJECTED_NO_CHANGE,
            OUTCOME_NO_ACTIONABLE,
        ):
            assert code in VALID_OUTCOMES
        assert {ACTION_SET_FREE_LIMIT, ACTION_NONE} == VALID_ACTIONS


# ---------------------------------------------------------------------------
# pick_actionable
# ---------------------------------------------------------------------------


class TestPickActionable:
    def test_picks_first_applicable_in_priority_order(self):
        recs = [
            {"id": REC_AMPLIFY_TOP_TRIGGER, "priority": PRIORITY_HIGH},
            {"id": REC_TIGHTEN_FREE_LIMIT, "priority": PRIORITY_HIGH},
            {"id": REC_FEATURE_TOP_INSIGHT, "priority": PRIORITY_MEDIUM},
        ]
        picked = pick_actionable(recs)
        assert picked is not None
        assert picked["id"] == REC_TIGHTEN_FREE_LIMIT

    def test_returns_none_when_no_applicable_recs(self):
        recs = [
            {"id": REC_AMPLIFY_TOP_TRIGGER, "priority": PRIORITY_HIGH},
            {"id": REC_FEATURE_TOP_INSIGHT, "priority": PRIORITY_MEDIUM},
            {"id": REC_DOUBLE_DOWN_SOURCE, "priority": PRIORITY_LOW},
            {"id": REC_INSUFFICIENT_DATA, "priority": PRIORITY_LOW},
        ]
        assert pick_actionable(recs) is None

    def test_handles_non_list_inputs(self):
        assert pick_actionable(None) is None  # type: ignore[arg-type]
        assert pick_actionable("not a list") is None  # type: ignore[arg-type]
        assert pick_actionable([]) is None


# ---------------------------------------------------------------------------
# plan_action
# ---------------------------------------------------------------------------


class TestPlanAction:
    def test_plan_for_tighten_free_limit_against_default_baseline(
        self, restore_free_limit_env,
    ):
        rec = {
            "id": REC_TIGHTEN_FREE_LIMIT,
            "priority": PRIORITY_HIGH,
            "rationale": "test",
        }
        plan = plan_action(rec)
        assert plan is not None
        assert plan["previous_value"] == DEFAULT_FREE_LIMIT
        assert plan["new_value"] == round(
            DEFAULT_FREE_LIMIT * (1 - TIGHTEN_REDUCTION_PCT),
        )
        assert plan["delta"] == plan["new_value"] - plan["previous_value"]
        assert plan["delta_pct"] < 0
        assert plan["action"] == ACTION_SET_FREE_LIMIT
        assert plan["env_var"] == FREE_LIMIT_ENV_VAR
        assert plan["recommendation_id"] == REC_TIGHTEN_FREE_LIMIT
        assert plan["rationale"] == "test"

    def test_plan_uses_explicit_current_value(
        self, restore_free_limit_env,
    ):
        plan = plan_action(
            {"id": REC_TIGHTEN_FREE_LIMIT, "priority": PRIORITY_HIGH},
            current_value=100,
        )
        assert plan is not None
        assert plan["previous_value"] == 100
        assert plan["new_value"] == 75

    def test_plan_uses_live_env_var_when_set(
        self, monkeypatch, restore_free_limit_env,
    ):
        monkeypatch.setenv(FREE_LIMIT_ENV_VAR, "120")
        plan = plan_action(
            {"id": REC_TIGHTEN_FREE_LIMIT, "priority": PRIORITY_HIGH},
        )
        assert plan["previous_value"] == 120
        assert plan["new_value"] == 90

    def test_plan_returns_none_for_non_actionable_rec(self):
        for rid in (
            REC_AMPLIFY_TOP_TRIGGER,
            REC_FEATURE_TOP_INSIGHT,
            REC_DOUBLE_DOWN_SOURCE,
            REC_INSUFFICIENT_DATA,
        ):
            assert plan_action({"id": rid, "priority": "high"}) is None

    def test_plan_returns_none_for_non_dict_input(self):
        assert plan_action(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_bounds
# ---------------------------------------------------------------------------


class TestValidateBounds:
    def _plan(self, *, prev: int, new: int) -> dict:
        return {
            "recommendation_id": REC_TIGHTEN_FREE_LIMIT,
            "priority": PRIORITY_HIGH,
            "action": ACTION_SET_FREE_LIMIT,
            "env_var": FREE_LIMIT_ENV_VAR,
            "previous_value": prev,
            "new_value": new,
            "delta": new - prev,
            "delta_pct": (new - prev) / prev if prev else 0.0,
        }

    def test_happy_path(self):
        check = validate_bounds(self._plan(prev=50, new=38))
        assert check == {"ok": True}

    def test_below_absolute_minimum_rejected(self):
        check = validate_bounds(self._plan(prev=12, new=9))
        assert not check["ok"]
        assert check["outcome"] == OUTCOME_REJECTED_OUT_OF_BOUNDS

    def test_above_absolute_maximum_rejected(self):
        check = validate_bounds(self._plan(prev=180, new=250))
        assert not check["ok"]
        assert check["outcome"] == OUTCOME_REJECTED_OUT_OF_BOUNDS

    def test_change_too_large_rejected(self):
        # 100 → 50: 50 % drop, exceeds the 30 % cap.
        check = validate_bounds(self._plan(prev=100, new=50))
        assert not check["ok"]
        assert check["outcome"] == OUTCOME_REJECTED_CHANGE_TOO_LARGE

    def test_change_at_exactly_the_cap_passes(self):
        # 100 → 70: exactly 30 % drop — still inside the bound.
        check = validate_bounds(self._plan(prev=100, new=70))
        assert check["ok"]

    def test_no_change_rejected(self):
        check = validate_bounds(self._plan(prev=50, new=50))
        assert not check["ok"]
        assert check["outcome"] == OUTCOME_REJECTED_NO_CHANGE

    def test_non_dict_plan_rejected(self):
        assert not validate_bounds(None)["ok"]  # type: ignore[arg-type]

    def test_unrecognised_action_rejected(self):
        check = validate_bounds({
            "action": "drop_database",
            "previous_value": 50,
            "new_value": 38,
        })
        assert not check["ok"]
        assert check["outcome"] == OUTCOME_REJECTED_NO_CHANGE


# ---------------------------------------------------------------------------
# apply_action — dry-run, force-apply, rejection paths
# ---------------------------------------------------------------------------


class TestApplyDryRun:
    def test_dry_run_does_not_mutate_env(
        self, restore_free_limit_env, execution_log: Path,
    ):
        plan = plan_action(
            {"id": REC_TIGHTEN_FREE_LIMIT, "priority": PRIORITY_HIGH},
        )
        status = apply_action(plan, force=False)
        assert status["outcome"] == OUTCOME_DRY_RUN
        assert status["applied"] is False
        assert status["logged"] is False
        assert FREE_LIMIT_ENV_VAR not in os.environ
        assert _read_log(execution_log) == []

    def test_dry_run_status_carries_plan(
        self, restore_free_limit_env, execution_log: Path,
    ):
        plan = plan_action(
            {"id": REC_TIGHTEN_FREE_LIMIT, "priority": PRIORITY_HIGH},
            current_value=100,
        )
        status = apply_action(plan, force=False)
        assert status["plan"]["previous_value"] == 100
        assert status["plan"]["new_value"] == 75


class TestApplyForce:
    def test_force_mutates_env_and_writes_log(
        self, restore_free_limit_env, execution_log: Path,
    ):
        plan = plan_action(
            {
                "id": REC_TIGHTEN_FREE_LIMIT,
                "priority": PRIORITY_HIGH,
                "rationale": "test",
            },
            current_value=100,
        )
        ts = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        status = apply_action(plan, force=True, now=ts)

        assert status["outcome"] == OUTCOME_APPLIED
        assert status["applied"] is True
        assert status["logged"] is True
        assert os.environ[FREE_LIMIT_ENV_VAR] == "75"

        rows = _read_log(execution_log)
        assert len(rows) == 1
        rec = rows[0]
        assert rec["recommendation_id"] == REC_TIGHTEN_FREE_LIMIT
        assert rec["priority"] == PRIORITY_HIGH
        assert rec["action"] == ACTION_SET_FREE_LIMIT
        assert rec["env_var"] == FREE_LIMIT_ENV_VAR
        assert rec["previous_value"] == 100
        assert rec["new_value"] == 75
        assert rec["delta"] == -25
        assert rec["outcome"] == OUTCOME_APPLIED
        assert rec["timestamp"].endswith("Z")
        assert "api_key_hash" not in rec

    def test_repeated_force_runs_compose_not_collapse(
        self, restore_free_limit_env, execution_log: Path,
    ):
        s1 = apply_action(
            plan_action(
                {"id": REC_TIGHTEN_FREE_LIMIT, "priority": PRIORITY_HIGH},
                current_value=100,
            ),
            force=True,
        )
        assert s1["outcome"] == OUTCOME_APPLIED
        assert os.environ[FREE_LIMIT_ENV_VAR] == "75"

        s2 = apply_action(
            plan_action(
                {"id": REC_TIGHTEN_FREE_LIMIT, "priority": PRIORITY_HIGH},
            ),
            force=True,
        )
        assert s2["outcome"] == OUTCOME_APPLIED
        assert os.environ[FREE_LIMIT_ENV_VAR] == "56"

        rows = _read_log(execution_log)
        assert len(rows) == 2
        assert rows[0]["new_value"] == 75
        assert rows[1]["new_value"] == 56


class TestApplyRejection:
    def test_out_of_bounds_low_logs_but_does_not_mutate(
        self, restore_free_limit_env, execution_log: Path,
    ):
        plan = {
            "recommendation_id": REC_TIGHTEN_FREE_LIMIT,
            "priority": PRIORITY_HIGH,
            "action": ACTION_SET_FREE_LIMIT,
            "env_var": FREE_LIMIT_ENV_VAR,
            "previous_value": 12,
            "new_value": 9,
            "delta": -3,
            "delta_pct": -0.25,
        }
        status = apply_action(plan, force=True)
        assert status["outcome"] == OUTCOME_REJECTED_OUT_OF_BOUNDS
        assert status["applied"] is False
        assert status["logged"] is True
        assert FREE_LIMIT_ENV_VAR not in os.environ
        rows = _read_log(execution_log)
        assert len(rows) == 1
        assert rows[0]["outcome"] == OUTCOME_REJECTED_OUT_OF_BOUNDS
        assert "rejection_reason" in rows[0]

    def test_out_of_bounds_high_logs_but_does_not_mutate(
        self, restore_free_limit_env, execution_log: Path,
    ):
        plan = {
            "recommendation_id": REC_TIGHTEN_FREE_LIMIT,
            "priority": PRIORITY_HIGH,
            "action": ACTION_SET_FREE_LIMIT,
            "env_var": FREE_LIMIT_ENV_VAR,
            "previous_value": 200,
            "new_value": 250,
            "delta": 50,
            "delta_pct": 0.25,
        }
        status = apply_action(plan, force=True)
        assert status["outcome"] == OUTCOME_REJECTED_OUT_OF_BOUNDS
        assert FREE_LIMIT_ENV_VAR not in os.environ

    def test_change_too_large_logs_but_does_not_mutate(
        self, restore_free_limit_env, execution_log: Path,
    ):
        plan = {
            "recommendation_id": REC_TIGHTEN_FREE_LIMIT,
            "priority": PRIORITY_HIGH,
            "action": ACTION_SET_FREE_LIMIT,
            "env_var": FREE_LIMIT_ENV_VAR,
            "previous_value": 100,
            "new_value": 50,
            "delta": -50,
            "delta_pct": -0.50,
        }
        status = apply_action(plan, force=True)
        assert status["outcome"] == OUTCOME_REJECTED_CHANGE_TOO_LARGE
        assert FREE_LIMIT_ENV_VAR not in os.environ

    def test_dry_run_rejection_does_not_log(
        self, restore_free_limit_env, execution_log: Path,
    ):
        plan = {
            "recommendation_id": REC_TIGHTEN_FREE_LIMIT,
            "priority": PRIORITY_HIGH,
            "action": ACTION_SET_FREE_LIMIT,
            "env_var": FREE_LIMIT_ENV_VAR,
            "previous_value": 12,
            "new_value": 9,
            "delta": -3,
            "delta_pct": -0.25,
        }
        status = apply_action(plan, force=False)
        assert status["outcome"] == OUTCOME_REJECTED_OUT_OF_BOUNDS
        assert status["logged"] is False
        assert _read_log(execution_log) == []


class TestApplyNoActionable:
    def test_no_plan_returns_zero_outcome(
        self, restore_free_limit_env, execution_log: Path,
    ):
        status = apply_action(None, force=True)
        assert status["outcome"] == OUTCOME_NO_ACTIONABLE
        assert status["applied"] is False
        assert status["logged"] is False
        assert FREE_LIMIT_ENV_VAR not in os.environ
        assert _read_log(execution_log) == []


# ---------------------------------------------------------------------------
# load_recommendations end-to-end
# ---------------------------------------------------------------------------


class TestLoadRecommendations:
    def test_strong_funnel_yields_tighten_free_limit(
        self, upgrade_log: Path, share_log: Path, execution_log: Path,
    ):
        _seed_strong_funnel(upgrade_log, share_log)
        recs = load_recommendations()
        ids = {r["id"] for r in recs}
        assert REC_TIGHTEN_FREE_LIMIT in ids


# ---------------------------------------------------------------------------
# Action-log leak guard
# ---------------------------------------------------------------------------


class TestNoRawKeyInLog:
    def test_log_record_carries_no_key_hash(
        self, restore_free_limit_env, execution_log: Path,
    ):
        plan = plan_action(
            {
                "id": REC_TIGHTEN_FREE_LIMIT,
                "priority": PRIORITY_HIGH,
                "rationale": "rate=50.0% vs overall=20.0%",
            },
            current_value=100,
        )
        status = apply_action(plan, force=True)
        assert status["outcome"] == OUTCOME_APPLIED

        body = execution_log.read_text(encoding="utf-8")
        assert "api_key_hash" not in body
        assert "key_hash" not in body
        for token in re.findall(r"[0-9a-f]{32}", body):
            assert False, f"unexpected hash-shaped token in log: {token!r}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_no_args_prints_help_returns_two(
        self, restore_free_limit_env, capsys,
    ):
        rc = ex.main([])
        assert rc == 2
        out = capsys.readouterr().out
        assert "execution" in out

    def test_apply_dry_run_with_strong_funnel(
        self,
        restore_free_limit_env,
        upgrade_log: Path,
        share_log: Path,
        execution_log: Path,
        capsys,
    ):
        _seed_strong_funnel(upgrade_log, share_log)
        rc = ex.main(["--apply"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "EXECUTION LAYER" in out
        assert "DRY-RUN" in out
        assert "tighten_free_limit" in out
        assert FREE_LIMIT_ENV_VAR not in os.environ
        assert _read_log(execution_log) == []

    def test_apply_force_with_strong_funnel(
        self,
        restore_free_limit_env,
        upgrade_log: Path,
        share_log: Path,
        execution_log: Path,
        capsys,
    ):
        _seed_strong_funnel(upgrade_log, share_log)
        rc = ex.main(["--apply", "--force"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "FORCE" in out
        assert "applied" in out
        # 50 * 0.75 → 38.
        assert os.environ[FREE_LIMIT_ENV_VAR] == "38"
        rows = _read_log(execution_log)
        assert len(rows) == 1
        assert rows[0]["outcome"] == OUTCOME_APPLIED

    def test_apply_force_json_envelope(
        self,
        restore_free_limit_env,
        upgrade_log: Path,
        share_log: Path,
        execution_log: Path,
        capsys,
    ):
        _seed_strong_funnel(upgrade_log, share_log)
        rc = ex.main(["--apply", "--force", "--json"])
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["outcome"] == OUTCOME_APPLIED
        assert parsed["applied"] is True
        assert parsed["logged"] is True
        assert parsed["plan"]["new_value"] == 38

    def test_apply_no_actionable_returns_zero_writes_nothing(
        self,
        restore_free_limit_env,
        tmp_path: Path,
        execution_log: Path,
        capsys,
    ):
        rc = ex.main([
            "--apply", "--force",
            "--upgrade-path", str(tmp_path / "absent.jsonl"),
            "--share-path", str(tmp_path / "absent.jsonl"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no actionable recommendation" in out
        assert FREE_LIMIT_ENV_VAR not in os.environ
        assert _read_log(execution_log) == []

    def test_rejected_plan_exits_three(
        self,
        restore_free_limit_env,
        monkeypatch,
        upgrade_log: Path,
        share_log: Path,
        execution_log: Path,
        capsys,
    ):
        """If we plant the live limit at the absolute floor, the
        25 % reduction step would land below FREE_LIMIT_MIN, and
        the CLI exits with code 3 to flag the rejection."""
        _seed_strong_funnel(upgrade_log, share_log)
        # 11 * 0.75 = 8.25 → rounds to 8, below FREE_LIMIT_MIN=10.
        monkeypatch.setenv(FREE_LIMIT_ENV_VAR, "11")
        rc = ex.main(["--apply", "--force"])
        out = capsys.readouterr().out
        assert rc == 3
        assert OUTCOME_REJECTED_OUT_OF_BOUNDS in out
        # Env var was NOT mutated past the floor.
        assert os.environ[FREE_LIMIT_ENV_VAR] == "11"


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_module_does_not_import_core(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "execution.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution.",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert forbidden not in src, (
                f"execution.py reaches into Core: {forbidden!r}"
            )
