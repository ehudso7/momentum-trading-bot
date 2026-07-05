"""
Phase 12 tests — the distinct, code-enforced Elite tier.

Covers the full tier → entitlement matrix as implemented:

    entitlement          free        pro         elite
    -------------------  ----------  ----------  ----------
    rate limit / minute  60          120         300
    reports window       3 days      30 days     unlimited
    experiments cap      3           25          unlimited
    insights             truncated   full        full + "elite" block

plus the plumbing beneath it:

- Plan-aware entitlement store: v2 on-disk format
  ``{"version": 2, "hashes": {"<hash>": {"plan": "pro"|"elite"}}}``,
  transparent acceptance of the legacy flat-list format (every entry
  → plan "pro"), v2 written on the next save, and the ``get_plan_for_hash``
  accessor.
- Webhook fulfilment: ``checkout.session.completed`` stores
  ``metadata[plan]`` (validated, default "pro");
  ``customer.subscription.updated`` with an active status never loses
  the stored plan; a non-active update removes the entitlement.
- ``/keys/provision`` rotation carries the PLAN (not just
  premium-ness) to the replacement hash.
- Tier resolution: ``request.state.api_key_tier`` is
  "free" | "pro" | "elite"; ``TRADING_API_ELITE_KEYS`` maps to elite
  and wins over ``TRADING_API_PREMIUM_KEYS``.
- ``GET /billing/status`` payload: tier / premium / plan / plan_source.
- ``POST /billing/checkout`` plan-switch 409 points at the billing
  portal ("Manage subscription") instead of a second subscription.

All tests write fixture files to a temporary directory and point the
API's env vars at them so no test touches the real reports/ or data/
directories (same pattern as tests/test_api_provisioning.py).
"""

from __future__ import annotations

import json
from datetime import date as _date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trading_bot.api import billing, key_store
from trading_bot.api.insights import build_elite_extras, truncate_for_free
from trading_bot.api.server import (
    API_KEY_ENV_VAR,
    DEFAULT_RATE_LIMITS_PER_MINUTE,
    ELITE_KEYS_ENV_VAR,
    MAX_FREE_TIER_DAYS,
    MAX_FREE_TIER_EXPERIMENTS,
    MAX_PRO_TIER_DAYS,
    MAX_PRO_TIER_EXPERIMENTS,
    PROVISION_SECRET_ENV_VAR,
    PROVISION_SECRET_HEADER,
    RATE_LIMIT_ENV_VAR,
    TIER_ELITE,
    TIER_EXPERIMENT_LIMITS,
    TIER_FREE,
    TIER_PRO,
    TIER_REPORT_WINDOW_DAYS,
    _canonical_tier,
    _rate_limit_for_tier,
    _reset_rate_limit_bucket,
    app,
)


PROVISION_SECRET = "phase12-provision-secret-XYZ"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def phase12_env(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    """Every test starts from a known env state with tmp-backed logs."""
    for name in (
        API_KEY_ENV_VAR,
        "TRADING_API_REPORTS_DIR",
        "TRADING_API_MANIFEST_PATH",
        "TRADING_API_ALLOWED_ORIGINS",
        RATE_LIMIT_ENV_VAR,
        "TRADING_API_RATE_LIMIT_PER_MINUTE_FREE",
        "TRADING_API_RATE_LIMIT_PER_MINUTE_PRO",
        "TRADING_API_RATE_LIMIT_PER_MINUTE_ELITE",
        "TRADING_API_PREMIUM_KEYS",
        ELITE_KEYS_ENV_VAR,
        "STRIPE_API_KEY",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_ID_PREMIUM",
        "STRIPE_PREMIUM_PRICE_ID",
        "STRIPE_PRO_PRICE_ID",
        "STRIPE_ELITE_PRICE_ID",
        "TRADING_PUBLIC_BASE_URL",
        "TRADING_FREE_MAX_REQUESTS_PER_DAY",
        "TRADING_FREE_MAX_REPORT_CALLS",
        "TRADING_USAGE_ENFORCEMENT_ENABLED",
        PROVISION_SECRET_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)

    reports_dir = tmp_path / "reports"
    manifest = tmp_path / "alpha_experiments.jsonl"
    keys_manifest = tmp_path / "api_keys_manifest.jsonl"
    keys_revoked = tmp_path / "api_keys_revoked.jsonl"
    stripe_cache = tmp_path / "stripe_premium_keys.json"
    usage_log = tmp_path / "usage.jsonl"
    audit_log = tmp_path / "audit.jsonl"
    upgrade_log = tmp_path / "upgrade_events.jsonl"
    webhook_events = tmp_path / "stripe_webhook_events.jsonl"

    monkeypatch.setenv("TRADING_API_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("TRADING_API_MANIFEST_PATH", str(manifest))
    monkeypatch.setenv("TRADING_API_KEYS_MANIFEST_PATH", str(keys_manifest))
    monkeypatch.setenv("TRADING_API_KEYS_REVOKED_PATH", str(keys_revoked))
    monkeypatch.setenv("TRADING_STRIPE_PREMIUM_CACHE_PATH", str(stripe_cache))
    monkeypatch.setenv("TRADING_API_USAGE_LOG_PATH", str(usage_log))
    monkeypatch.setenv("TRADING_API_AUDIT_LOG_PATH", str(audit_log))
    monkeypatch.setenv("TRADING_API_UPGRADE_EVENTS_LOG_PATH", str(upgrade_log))
    monkeypatch.setenv(
        "TRADING_STRIPE_WEBHOOK_EVENTS_PATH", str(webhook_events),
    )
    monkeypatch.setenv(PROVISION_SECRET_ENV_VAR, PROVISION_SECRET)

    billing.reset_cache_for_tests()
    key_store.reset_caches_for_tests()
    _reset_rate_limit_bucket()

    yield {
        "reports_dir": reports_dir,
        "manifest": manifest,
        "stripe_cache": stripe_cache,
    }

    _reset_rate_limit_bucket()
    billing.reset_cache_for_tests()
    key_store.reset_caches_for_tests()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _issue_key(tier: str = "free", label: str = "phase12") -> tuple[str, str]:
    from trading_bot.api.keys import issue_key

    result = issue_key(tier=tier, label=label)
    return result["api_key"], result["key_hash"]


def _paid_key(plan: str, monkeypatch) -> tuple[str, str]:
    """Manifest key + Stripe-cache entitlement carrying ``plan``."""
    raw, key_hash = _issue_key(label=f"phase12-{plan}")
    billing.add_premium_hash(key_hash, plan=plan)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_phase12")
    return raw, key_hash


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def _set_today(monkeypatch, today: _date) -> None:
    from trading_bot.api import server as srv_mod

    monkeypatch.setattr(srv_mod, "_today_utc", lambda: today)


def _write_report(reports_dir: Path, date: str, **overrides) -> Path:
    """Minimal daily report with regime + trend signal sources."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    base: dict[str, Any] = {
        "report_type": "daily_alpha_validation",
        "report_date": date,
        "scorer_fingerprint": "f" * 64,
        "totals": {"alpha_rows": 100, "buy_rows": 25, "skip_rows": 75},
        "regime_stats": {
            "trending": {"hits": 12},
            "choppy": {"hits": 5},
            "squeeze": {"hits": 3},
        },
        "promotion_readiness": {
            "ready": False, "consecutive_passing_days": 2,
        },
        "guardrails": {"status": "ok", "reasons": ["healthy"]},
    }
    base.update(overrides)
    path = reports_dir / f"alpha_report_{date}.json"
    path.write_text(json.dumps(base))
    return path


def _write_manifest_records(path: Path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        json.dumps({
            "timestamp": f"2026-04-{(i % 28) + 1:02d}T17:00:00",
            "report_date": f"2026-04-{(i % 28) + 1:02d}",
            "scorer_fingerprint": "a" * 64,
            "totals": {"alpha_rows": i},
        })
        for i in range(n)
    ]
    path.write_text("\n".join(rows) + "\n")


# ===========================================================================
# Entitlement store — legacy → v2 migration + plan accessor
# ===========================================================================


class TestPlanAwareCacheMigration:
    def test_legacy_flat_list_reads_as_plan_pro(self, phase12_env):
        cache = phase12_env["stripe_cache"]
        h1, h2 = "a" * 32, "b" * 32
        cache.write_text(json.dumps([h1, h2]), encoding="utf-8")
        billing.reset_cache_for_tests()
        assert billing.is_premium_hash(h1) is True
        assert billing.is_premium_hash(h2) is True
        assert billing.get_plan_for_hash(h1) == "pro"
        assert billing.get_plan_for_hash(h2) == "pro"
        assert billing.current_premium_key_hashes() == {h1, h2}

    def test_legacy_flat_list_left_untouched_on_pure_read(self, phase12_env):
        """Reading alone never rewrites the file — production hashes
        in the legacy list stay byte-identical until the next save."""
        cache = phase12_env["stripe_cache"]
        original = json.dumps(["c" * 32])
        cache.write_text(original, encoding="utf-8")
        billing.reset_cache_for_tests()
        assert billing.is_premium_hash("c" * 32) is True
        assert cache.read_text(encoding="utf-8") == original

    def test_next_save_migrates_legacy_list_to_v2(self, phase12_env):
        cache = phase12_env["stripe_cache"]
        legacy_hash = "d" * 32
        cache.write_text(json.dumps([legacy_hash]), encoding="utf-8")
        billing.reset_cache_for_tests()
        billing.add_premium_hash("e" * 32, plan="elite")
        data = json.loads(cache.read_text(encoding="utf-8"))
        assert data["version"] == 2
        assert data["hashes"][legacy_hash] == {"plan": "pro"}
        assert data["hashes"]["e" * 32] == {"plan": "elite"}

    def test_v2_round_trip_preserves_plans(self, phase12_env):
        billing.add_premium_hash("1" * 32, plan="elite")
        billing.add_premium_hash("2" * 32)  # default plan
        billing.reset_cache_for_tests()  # force re-read from disk
        assert billing.get_plan_for_hash("1" * 32) == "elite"
        assert billing.get_plan_for_hash("2" * 32) == "pro"

    def test_unknown_plan_label_degrades_to_pro_not_lost(self, phase12_env):
        cache = phase12_env["stripe_cache"]
        cache.write_text(json.dumps({
            "version": 2,
            "hashes": {"9" * 32: {"plan": "platinum"}},
        }), encoding="utf-8")
        billing.reset_cache_for_tests()
        assert billing.is_premium_hash("9" * 32) is True
        assert billing.get_plan_for_hash("9" * 32) == "pro"

    def test_invalid_plan_argument_defaults_to_pro(self, phase12_env):
        billing.add_premium_hash("3" * 32, plan="diamond")
        assert billing.get_plan_for_hash("3" * 32) == "pro"

    def test_get_plan_for_missing_hash_is_none(self, phase12_env):
        assert billing.get_plan_for_hash("f" * 32) is None
        assert billing.get_plan_for_hash("") is None
        assert billing.get_plan_for_hash(None) is None

    def test_remove_drops_plan(self, phase12_env):
        billing.add_premium_hash("4" * 32, plan="elite")
        billing.remove_premium_hash("4" * 32)
        assert billing.get_plan_for_hash("4" * 32) is None
        assert billing.is_premium_hash("4" * 32) is False


# ===========================================================================
# Webhook fulfilment — plan storage + subscription.updated semantics
# ===========================================================================


class TestWebhookPlanFulfilment:
    def _completed_event(self, key_hash: str, plan=None, event_id="evt_1"):
        metadata: dict[str, Any] = {"key_hash": key_hash}
        if plan is not None:
            metadata["plan"] = plan
        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {"object": {"metadata": metadata}},
        }

    def test_checkout_completed_stores_elite_plan(self):
        _, key_hash = _issue_key()
        result = billing.handle_webhook_event(
            self._completed_event(key_hash, plan="elite"),
        )
        assert result["action"] == "added"
        assert result["plan"] == "elite"
        assert billing.get_plan_for_hash(key_hash) == "elite"

    def test_checkout_completed_without_plan_defaults_to_pro(self):
        _, key_hash = _issue_key()
        result = billing.handle_webhook_event(
            self._completed_event(key_hash),
        )
        assert result["action"] == "added"
        assert billing.get_plan_for_hash(key_hash) == "pro"

    def test_checkout_completed_invalid_plan_defaults_to_pro(self):
        _, key_hash = _issue_key()
        billing.handle_webhook_event(
            self._completed_event(key_hash, plan="platinum"),
        )
        assert billing.get_plan_for_hash(key_hash) == "pro"

    def test_subscription_created_stores_plan_from_metadata(self):
        _, key_hash = _issue_key()
        result = billing.handle_webhook_event({
            "id": "evt_sub_created",
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"key_hash": key_hash, "plan": "elite"},
            }},
        })
        assert result["action"] == "added"
        assert billing.get_plan_for_hash(key_hash) == "elite"

    def test_subscription_updated_active_preserves_stored_plan(self):
        """A plan-less subscription.updated must NOT downgrade elite."""
        _, key_hash = _issue_key()
        billing.add_premium_hash(key_hash, plan="elite")
        result = billing.handle_webhook_event({
            "id": "evt_sub_updated_1",
            "type": "customer.subscription.updated",
            "data": {"object": {
                "status": "active",
                "metadata": {"key_hash": key_hash},
            }},
        })
        assert result["action"] == "added"
        assert billing.get_plan_for_hash(key_hash) == "elite"

    def test_subscription_updated_with_plan_switches_plan(self):
        _, key_hash = _issue_key()
        billing.add_premium_hash(key_hash, plan="pro")
        billing.handle_webhook_event({
            "id": "evt_sub_updated_2",
            "type": "customer.subscription.updated",
            "data": {"object": {
                "status": "active",
                "metadata": {"key_hash": key_hash, "plan": "elite"},
            }},
        })
        assert billing.get_plan_for_hash(key_hash) == "elite"

    def test_subscription_updated_non_active_removes_entitlement(self):
        _, key_hash = _issue_key()
        billing.add_premium_hash(key_hash, plan="elite")
        result = billing.handle_webhook_event({
            "id": "evt_sub_updated_3",
            "type": "customer.subscription.updated",
            "data": {"object": {
                "status": "past_due",
                "metadata": {"key_hash": key_hash},
            }},
        })
        assert result["action"] == "removed"
        assert billing.is_premium_hash(key_hash) is False
        assert billing.get_plan_for_hash(key_hash) is None

    def test_subscription_deleted_removes_entitlement(self):
        _, key_hash = _issue_key()
        billing.add_premium_hash(key_hash, plan="elite")
        result = billing.handle_webhook_event({
            "id": "evt_sub_deleted",
            "type": "customer.subscription.deleted",
            "data": {"object": {"metadata": {"key_hash": key_hash}}},
        })
        assert result["action"] == "removed"
        assert billing.get_plan_for_hash(key_hash) is None


# ===========================================================================
# /keys/provision — rotation carries the plan
# ===========================================================================


class TestProvisionCarriesPlan:
    def _provision(self, client: TestClient, user_ref="phase12-user"):
        return client.post(
            "/keys/provision",
            json={"user_ref": user_ref},
            headers={PROVISION_SECRET_HEADER: PROVISION_SECRET},
        )

    def test_rotation_carries_elite_plan(self, client: TestClient):
        first = self._provision(client).json()
        first_hash = key_store.hash_api_key(first["api_key"])
        billing.add_premium_hash(first_hash, plan="elite")

        second = self._provision(client).json()
        second_hash = key_store.hash_api_key(second["api_key"])
        assert second["tier"] == "elite"
        assert second["rotated"] is True
        assert billing.get_plan_for_hash(second_hash) == "elite"
        # Stale hash dropped; old key revoked.
        assert billing.get_plan_for_hash(first_hash) is None
        assert key_store.is_revoked(first_hash) is True

    def test_rotation_carries_pro_plan(self, client: TestClient):
        first = self._provision(client).json()
        billing.add_premium_hash(
            key_store.hash_api_key(first["api_key"]), plan="pro",
        )
        second = self._provision(client).json()
        assert second["tier"] == "pro"
        assert billing.get_plan_for_hash(
            key_store.hash_api_key(second["api_key"]),
        ) == "pro"

    def test_elite_key_still_elite_via_auth_after_rotation(
        self, client: TestClient, monkeypatch,
    ):
        first = self._provision(client).json()
        billing.add_premium_hash(
            key_store.hash_api_key(first["api_key"]), plan="elite",
        )
        new_raw = self._provision(client).json()["api_key"]
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_phase12")
        r = client.get("/billing/status", headers=_auth(new_raw))
        assert r.status_code == 200
        assert r.json() == {
            "tier": "elite",
            "premium": True,
            "plan": "elite",
            "plan_source": "manifest",
        }


# ===========================================================================
# Tier resolution — pro vs elite keys
# ===========================================================================


class TestTierResolution:
    def test_stripe_cache_plan_resolves_pro_and_elite(
        self, client: TestClient, monkeypatch,
    ):
        pro_raw, _ = _paid_key("pro", monkeypatch)
        elite_raw, _ = _paid_key("elite", monkeypatch)
        assert client.get(
            "/billing/status", headers=_auth(pro_raw),
        ).json()["tier"] == "pro"
        assert client.get(
            "/billing/status", headers=_auth(elite_raw),
        ).json()["tier"] == "elite"

    def test_elite_env_allowlist_resolves_elite(
        self, client: TestClient, monkeypatch,
    ):
        monkeypatch.setenv(ELITE_KEYS_ENV_VAR, "elite-env-key")
        r = client.get(
            "/billing/status", headers=_auth("elite-env-key"),
        )
        assert r.status_code == 200
        assert r.json() == {
            "tier": "elite",
            "premium": True,
            "plan": "elite",
            "plan_source": "premium_allowlist",
        }

    def test_elite_allowlist_wins_over_premium_allowlist(
        self, client: TestClient, monkeypatch,
    ):
        monkeypatch.setenv("TRADING_API_PREMIUM_KEYS", "both-lists-key")
        monkeypatch.setenv(ELITE_KEYS_ENV_VAR, "both-lists-key")
        r = client.get("/billing/status", headers=_auth("both-lists-key"))
        assert r.json()["tier"] == "elite"

    def test_premium_allowlist_resolves_pro(
        self, client: TestClient, monkeypatch,
    ):
        monkeypatch.setenv("TRADING_API_PREMIUM_KEYS", "pro-env-key")
        r = client.get("/billing/status", headers=_auth("pro-env-key"))
        assert r.json() == {
            "tier": "pro",
            "premium": True,
            "plan": "pro",
            "plan_source": "premium_allowlist",
        }

    def test_free_manifest_key_resolves_free(self, client: TestClient):
        raw, _ = _issue_key()
        r = client.get("/billing/status", headers=_auth(raw))
        assert r.json() == {
            "tier": "free",
            "premium": False,
            "plan": "free",
            "plan_source": "manifest",
        }

    def test_legacy_premium_label_canonicalises_to_pro(self):
        assert _canonical_tier("premium") == TIER_PRO
        assert _canonical_tier("elite") == TIER_ELITE
        assert _canonical_tier("pro") == TIER_PRO
        assert _canonical_tier("free") == TIER_FREE
        assert _canonical_tier("gold") is None
        assert _canonical_tier(None) is None


# ===========================================================================
# Rate limits — 60 / 120 / 300 with env overrides
# ===========================================================================


class TestTierRateLimits:
    def test_defaults(self):
        assert _rate_limit_for_tier(TIER_FREE) == 60
        assert _rate_limit_for_tier(TIER_PRO) == 120
        assert _rate_limit_for_tier(TIER_ELITE) == 300
        assert DEFAULT_RATE_LIMITS_PER_MINUTE == {
            "free": 60, "pro": 120, "elite": 300,
        }

    def test_unknown_or_anonymous_tier_gets_free_budget(self):
        assert _rate_limit_for_tier(None) == 60
        assert _rate_limit_for_tier("gold") == 60
        # Legacy "premium" label maps to the pro budget.
        assert _rate_limit_for_tier("premium") == 120

    def test_per_tier_env_overrides(self, monkeypatch):
        monkeypatch.setenv("TRADING_API_RATE_LIMIT_PER_MINUTE_FREE", "10")
        monkeypatch.setenv("TRADING_API_RATE_LIMIT_PER_MINUTE_PRO", "20")
        monkeypatch.setenv("TRADING_API_RATE_LIMIT_PER_MINUTE_ELITE", "30")
        assert _rate_limit_for_tier(TIER_FREE) == 10
        assert _rate_limit_for_tier(TIER_PRO) == 20
        assert _rate_limit_for_tier(TIER_ELITE) == 30

    def test_base_env_is_shared_fallback(self, monkeypatch):
        monkeypatch.setenv(RATE_LIMIT_ENV_VAR, "77")
        assert _rate_limit_for_tier(TIER_FREE) == 77
        assert _rate_limit_for_tier(TIER_PRO) == 77
        assert _rate_limit_for_tier(TIER_ELITE) == 77
        # A per-tier var still wins over the base.
        monkeypatch.setenv("TRADING_API_RATE_LIMIT_PER_MINUTE_ELITE", "500")
        assert _rate_limit_for_tier(TIER_ELITE) == 500
        assert _rate_limit_for_tier(TIER_PRO) == 77

    def test_garbage_env_values_fall_through(self, monkeypatch):
        monkeypatch.setenv("TRADING_API_RATE_LIMIT_PER_MINUTE_PRO", "zero")
        monkeypatch.setenv(RATE_LIMIT_ENV_VAR, "-5")
        assert _rate_limit_for_tier(TIER_PRO) == 120
        assert _rate_limit_for_tier(TIER_FREE) == 60

    def _burst(self, client: TestClient, raw: str, n: int) -> list[int]:
        return [
            client.get("/billing/status", headers=_auth(raw)).status_code
            for _ in range(n)
        ]

    def test_middleware_enforces_boundary_per_tier(
        self, client: TestClient, monkeypatch,
    ):
        """Exact boundary per tier: N requests pass, N+1 → 429."""
        monkeypatch.setenv("TRADING_API_RATE_LIMIT_PER_MINUTE_FREE", "2")
        monkeypatch.setenv("TRADING_API_RATE_LIMIT_PER_MINUTE_PRO", "4")
        monkeypatch.setenv("TRADING_API_RATE_LIMIT_PER_MINUTE_ELITE", "6")

        free_raw, _ = _issue_key(label="rl-free")
        pro_raw, _ = _paid_key("pro", monkeypatch)
        elite_raw, _ = _paid_key("elite", monkeypatch)

        for raw, limit in (
            (free_raw, 2), (pro_raw, 4), (elite_raw, 6),
        ):
            _reset_rate_limit_bucket()
            statuses = self._burst(client, raw, limit + 1)
            assert statuses[:limit] == [200] * limit, (raw, statuses)
            assert statuses[limit] == 429, (raw, statuses)

    def test_free_default_budget_unchanged_at_60(self):
        # The pre-Phase-12 default (60) is exactly the free budget —
        # no regression for existing deployments.
        assert _rate_limit_for_tier(TIER_FREE) == 60


# ===========================================================================
# Reports window — 3 / 30 / unlimited
# ===========================================================================


class TestPhase12ReportWindows:
    TODAY = _date(2026, 7, 1)

    def _get(self, client, raw, date):
        return client.get(f"/reports/{date}", headers=_auth(raw))

    def test_matrix_constants(self):
        assert TIER_REPORT_WINDOW_DAYS == {
            "free": MAX_FREE_TIER_DAYS,
            "pro": MAX_PRO_TIER_DAYS,
            "elite": None,
        }
        assert MAX_FREE_TIER_DAYS == 3
        assert MAX_PRO_TIER_DAYS == 30

    def test_free_window_is_three_days(
        self, client: TestClient, phase12_env, monkeypatch,
    ):
        _set_today(monkeypatch, self.TODAY)
        raw, _ = _issue_key(label="win-free")
        for date in ("2026-07-01", "2026-06-30", "2026-06-29"):
            _write_report(phase12_env["reports_dir"], date)
            assert self._get(client, raw, date).status_code == 200, date
        _write_report(phase12_env["reports_dir"], "2026-06-28")
        r = self._get(client, raw, "2026-06-28")
        assert r.status_code == 403

    def test_pro_window_is_thirty_days(
        self, client: TestClient, phase12_env, monkeypatch,
    ):
        _set_today(monkeypatch, self.TODAY)
        raw, _ = _paid_key("pro", monkeypatch)
        # Day 30 of the window (oldest allowed): 2026-06-02.
        for date in ("2026-06-28", "2026-06-02"):
            _write_report(phase12_env["reports_dir"], date)
            assert self._get(client, raw, date).status_code == 200, date
        # Day 31: blocked.
        _write_report(phase12_env["reports_dir"], "2026-06-01")
        assert self._get(client, raw, "2026-06-01").status_code == 403
        # Future dates: blocked for pro too.
        _write_report(phase12_env["reports_dir"], "2026-07-02")
        assert self._get(client, raw, "2026-07-02").status_code == 403

    def test_elite_window_is_unlimited(
        self, client: TestClient, phase12_env, monkeypatch,
    ):
        _set_today(monkeypatch, self.TODAY)
        raw, _ = _paid_key("elite", monkeypatch)
        for date in ("2026-06-01", "2020-01-01"):
            _write_report(phase12_env["reports_dir"], date)
            assert self._get(client, raw, date).status_code == 200, date


# ===========================================================================
# Experiments cap — 3 / 25 / unlimited
# ===========================================================================


class TestPhase12ExperimentCaps:
    def test_matrix_constants(self):
        assert TIER_EXPERIMENT_LIMITS == {
            "free": MAX_FREE_TIER_EXPERIMENTS,
            "pro": MAX_PRO_TIER_EXPERIMENTS,
            "elite": None,
        }
        assert MAX_FREE_TIER_EXPERIMENTS == 3
        assert MAX_PRO_TIER_EXPERIMENTS == 25

    def test_free_capped_at_three(self, client: TestClient, phase12_env):
        _write_manifest_records(phase12_env["manifest"], 40)
        raw, _ = _issue_key(label="exp-free")
        # Implicit limit silently capped at 3.
        r = client.get("/experiments/recent", headers=_auth(raw))
        assert r.json()["count"] == 3
        # Explicit limit above the cap → 403.
        r = client.get("/experiments/recent?limit=4", headers=_auth(raw))
        assert r.status_code == 403
        # n above the cap → 403.
        assert client.get(
            "/experiments/4", headers=_auth(raw),
        ).status_code == 403

    def test_pro_capped_at_twenty_five(
        self, client: TestClient, phase12_env, monkeypatch,
    ):
        _write_manifest_records(phase12_env["manifest"], 40)
        raw, _ = _paid_key("pro", monkeypatch)
        # Explicit limit at the boundary passes…
        r = client.get("/experiments/recent?limit=25", headers=_auth(raw))
        assert r.status_code == 200
        assert r.json()["count"] == 25
        # …one past it is rejected.
        assert client.get(
            "/experiments/recent?limit=26", headers=_auth(raw),
        ).status_code == 403
        # Any explicit limit above the cap is rejected, not clamped.
        r = client.get("/experiments/recent?limit=100", headers=_auth(raw))
        assert r.status_code == 403
        # n boundary: 25 OK, 26 → 403.
        assert client.get(
            "/experiments/25", headers=_auth(raw),
        ).status_code == 200
        assert client.get(
            "/experiments/26", headers=_auth(raw),
        ).status_code == 403

    def test_pro_default_limit_untouched(
        self, client: TestClient, phase12_env, monkeypatch,
    ):
        _write_manifest_records(phase12_env["manifest"], 40)
        raw, _ = _paid_key("pro", monkeypatch)
        r = client.get("/experiments/recent", headers=_auth(raw))
        assert r.status_code == 200
        assert r.json()["count"] == 10  # FastAPI default limit

    def test_elite_uncapped(
        self, client: TestClient, phase12_env, monkeypatch,
    ):
        _write_manifest_records(phase12_env["manifest"], 40)
        raw, _ = _paid_key("elite", monkeypatch)
        r = client.get("/experiments/recent?limit=40", headers=_auth(raw))
        assert r.status_code == 200
        assert r.json()["count"] == 40
        assert client.get(
            "/experiments/40", headers=_auth(raw),
        ).status_code == 200
        # Past the manifest length → documented 404, never 403.
        r = client.get("/experiments/99", headers=_auth(raw))
        assert r.status_code == 404


# ===========================================================================
# Insights — truncation matrix + the elite block
# ===========================================================================


class TestPhase12InsightsMatrix:
    def _seed_reports(self, reports_dir: Path) -> None:
        _write_report(
            reports_dir, "2026-06-30",
            totals={"alpha_rows": 90, "buy_rows": 20, "skip_rows": 70},
        )
        _write_report(reports_dir, "2026-07-01")

    def test_free_insights_truncated_no_elite_block(
        self, client: TestClient, phase12_env, monkeypatch,
    ):
        _set_today(monkeypatch, _date(2026, 7, 1))
        self._seed_reports(phase12_env["reports_dir"])
        raw, _ = _issue_key(label="ins-free")
        body = client.get("/reports/latest", headers=_auth(raw)).json()
        assert "elite" not in body
        insights = body["insights"]
        assert 0 < len(insights) <= 2  # FREE_INSIGHT_LIMIT
        # Evidence projected through the free allow-list: the trend
        # insight keeps only delta/direction.
        trend = next(i for i in insights if i["id"] == "trend.buy_delta")
        assert set(trend["evidence"]) <= {"delta", "direction"}
        # Deep report sections stay premium.
        assert "regime_stats" not in body

    def test_pro_insights_full_no_elite_block(
        self, client: TestClient, phase12_env, monkeypatch,
    ):
        _set_today(monkeypatch, _date(2026, 7, 1))
        self._seed_reports(phase12_env["reports_dir"])
        raw, _ = _paid_key("pro", monkeypatch)
        body = client.get("/reports/latest", headers=_auth(raw)).json()
        assert "elite" not in body
        insights = body["insights"]
        assert len(insights) == 3
        trend = next(i for i in insights if i["id"] == "trend.buy_delta")
        # Full premium evidence — not the free projection.
        assert "curr_buy_rows" in trend["evidence"]
        assert "percent_change" in trend["evidence"]
        assert "regime_stats" in body

    def test_elite_gets_full_insights_plus_elite_block(
        self, client: TestClient, phase12_env, monkeypatch,
    ):
        _set_today(monkeypatch, _date(2026, 7, 1))
        self._seed_reports(phase12_env["reports_dir"])
        raw, _ = _paid_key("elite", monkeypatch)
        body = client.get("/reports/latest", headers=_auth(raw)).json()
        assert len(body["insights"]) == 3
        elite = body["elite"]
        # Full regime ranking — computed by the dominant-regime rule
        # but dropped from its evidence (only dominant + runner-up
        # survive there).
        assert elite["regime_ranking"] == [
            {"regime": "trending", "hits": 12, "share": 0.6},
            {"regime": "choppy", "hits": 5, "share": 0.25},
            {"regime": "squeeze", "hits": 3, "share": 0.15},
        ]
        assert elite["trend_detail"] == {
            "curr_buy_rows": 25,
            "prev_buy_rows": 20,
            "delta": 5,
            "percent_change": 25.0,
        }

    def test_build_elite_extras_is_real_data_only(self):
        # No regime stats, no prior report → nothing to say → None.
        assert build_elite_extras({"totals": {"buy_rows": 5}}) is None
        assert build_elite_extras(None) is None
        assert build_elite_extras("nope") is None

    def test_truncate_for_free_unchanged_contract(self):
        insights = [{
            "id": "trend.buy_delta",
            "title": "t", "summary": "s", "confidence": 0.5,
            "severity": "info",
            "evidence": {"delta": 1, "direction": "up", "curr_buy_rows": 9},
            "action": "a",
        }]
        out = truncate_for_free(insights)
        assert out[0]["evidence"] == {"delta": 1, "direction": "up"}


# ===========================================================================
# Daily usage caps — pro/elite bypass exactly as premium did
# ===========================================================================


class TestPhase12DailyCapBypass:
    def test_paid_tiers_bypass_free_daily_caps(
        self, client: TestClient, phase12_env, monkeypatch,
    ):
        _write_report(phase12_env["reports_dir"], "2026-07-01")
        _set_today(monkeypatch, _date(2026, 7, 1))
        monkeypatch.setenv("TRADING_FREE_MAX_REQUESTS_PER_DAY", "1")
        monkeypatch.setenv("TRADING_FREE_DAILY_REQUEST_LIMIT", "1")

        pro_raw, _ = _paid_key("pro", monkeypatch)
        elite_raw, _ = _paid_key("elite", monkeypatch)
        for raw in (pro_raw, elite_raw):
            for _ in range(3):
                r = client.get("/reports/latest", headers=_auth(raw))
                assert r.status_code == 200
            # Paid callers never see the free-tier counters.
            assert "X-Free-Tier-Usage" not in r.headers


# ===========================================================================
# /billing/checkout — plan-switch 409
# ===========================================================================


class TestCheckoutPlanSwitch409:
    def _stripe_ready(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_phase12")
        monkeypatch.setenv("STRIPE_PREMIUM_PRICE_ID", "price_premium")
        monkeypatch.setenv("TRADING_PUBLIC_BASE_URL", "https://api.example.com")

    def test_pro_requesting_elite_points_at_billing_portal(
        self, client: TestClient, monkeypatch,
    ):
        self._stripe_ready(monkeypatch)
        raw, _ = _paid_key("pro", monkeypatch)
        r = client.post(
            "/billing/checkout", json={"plan": "elite"}, headers=_auth(raw),
        )
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert "Manage subscription" in detail
        assert "'pro'" in detail and "'elite'" in detail
        assert "second subscription" in detail

    def test_elite_requesting_pro_points_at_billing_portal(
        self, client: TestClient, monkeypatch,
    ):
        self._stripe_ready(monkeypatch)
        raw, _ = _paid_key("elite", monkeypatch)
        r = client.post(
            "/billing/checkout", json={"plan": "pro"}, headers=_auth(raw),
        )
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert "Manage subscription" in detail
        assert "'elite'" in detail and "'pro'" in detail

    def test_same_plan_or_no_plan_keeps_legacy_409(
        self, client: TestClient, monkeypatch,
    ):
        self._stripe_ready(monkeypatch)
        raw, _ = _paid_key("pro", monkeypatch)
        # Same plan requested.
        r = client.post(
            "/billing/checkout", json={"plan": "pro"}, headers=_auth(raw),
        )
        assert r.status_code == 409
        assert "already premium" in r.json()["detail"]
        # No plan requested.
        r = client.post("/billing/checkout", headers=_auth(raw))
        assert r.status_code == 409
        assert "already premium" in r.json()["detail"]

    def test_free_caller_unaffected(self, client: TestClient, monkeypatch):
        self._stripe_ready(monkeypatch)

        calls = []

        def fake_poster(*, url, data, auth, timeout):
            calls.append(data)
            return {"id": "cs_x", "url": "https://checkout.stripe.com/x"}

        monkeypatch.setattr(billing, "_post_to_stripe", fake_poster)
        raw, _ = _issue_key(label="checkout-free")
        r = client.post(
            "/billing/checkout", json={"plan": "elite"}, headers=_auth(raw),
        )
        assert r.status_code == 200
        assert r.json()["plan"] == "elite"
        assert calls[0]["metadata[plan]"] == "elite"
