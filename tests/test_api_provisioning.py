"""
Phase 11 tests — self-serve key provisioning, tier-aware checkout,
and the billing status probe.

Covers:
- POST /keys/provision guard: 503 when TRADING_PROVISION_SECRET is
  unset (fail-closed), 403 on a wrong/missing X-Provision-Secret
  header, and no API-key requirement.
- Provision body validation: user_ref required, 1-128 chars, safe
  charset; label capped at 128 chars.
- Provision happy path: a fresh free-tier manifest key that
  authenticates; the raw key never lands on disk.
- Idempotent rotation: a repeat call for the same user_ref revokes
  the previous key and issues a new one — one user never accumulates
  live keys. Premium entitlements carry over across rotation.
- POST /billing/checkout plan → price mapping (STRIPE_PRO_PRICE_ID /
  STRIPE_ELITE_PRICE_ID with STRIPE_PREMIUM_PRICE_ID fallback),
  plan + supabase_user_id metadata, invalid plan → 422, and the
  fail-closed 409 for keys outside the issuance manifest (P1 fix:
  an env-key caller must not be able to pay without fulfilment).
- GET /billing/status: tier/premium/plan_source for manifest, env,
  and allowlist keys; requires auth like every protected endpoint.
- Full journey: provision → checkout → webhook → status premium.

All tests write fixture files to a temporary directory and point
the API's env vars at them so no test touches the real reports/
or data/ directories.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trading_bot.api import key_store
from trading_bot.api.server import (
    API_KEY_ENV_VAR,
    PROVISION_SECRET_ENV_VAR,
    PROVISION_SECRET_HEADER,
    app,
)


PROVISION_SECRET = "phase11-provision-secret-XYZ"
USER_REF = "supabase-user-42"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def phase11_env(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    """Every test starts from a known env state with tmp-backed logs."""
    for name in (
        API_KEY_ENV_VAR,
        "TRADING_API_REPORTS_DIR",
        "TRADING_API_MANIFEST_PATH",
        "TRADING_API_ALLOWED_ORIGINS",
        "TRADING_API_RATE_LIMIT_PER_MINUTE",
        "TRADING_API_PREMIUM_KEYS",
        "STRIPE_API_KEY",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_ID_PREMIUM",
        "STRIPE_PREMIUM_PRICE_ID",
        "STRIPE_PRO_PRICE_ID",
        "STRIPE_ELITE_PRICE_ID",
        "STRIPE_CHECKOUT_SUCCESS_PATH",
        "STRIPE_CHECKOUT_CANCEL_PATH",
        "TRADING_PUBLIC_BASE_URL",
        "TRADING_FREE_MAX_REQUESTS_PER_DAY",
        "TRADING_FREE_MAX_REPORT_CALLS",
        "TRADING_USAGE_ENFORCEMENT_ENABLED",
        PROVISION_SECRET_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)

    keys_manifest = tmp_path / "api_keys_manifest.jsonl"
    keys_revoked = tmp_path / "api_keys_revoked.jsonl"
    stripe_cache = tmp_path / "stripe_premium_keys.json"
    usage_log = tmp_path / "usage.jsonl"
    audit_log = tmp_path / "audit.jsonl"
    upgrade_log = tmp_path / "upgrade_events.jsonl"
    webhook_events = tmp_path / "stripe_webhook_events.jsonl"

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

    from trading_bot.api import billing as _billing_mod
    _billing_mod.reset_cache_for_tests()
    key_store.reset_caches_for_tests()

    from trading_bot.api.server import _reset_rate_limit_bucket
    _reset_rate_limit_bucket()

    yield {
        "keys_manifest": keys_manifest,
        "keys_revoked": keys_revoked,
        "stripe_cache": stripe_cache,
        "usage_log": usage_log,
        "audit_log": audit_log,
    }

    _reset_rate_limit_bucket()
    _billing_mod.reset_cache_for_tests()
    key_store.reset_caches_for_tests()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _provision(
    client: TestClient,
    user_ref: str = USER_REF,
    *,
    secret: str | None = PROVISION_SECRET,
    label: str | None = None,
    body_override: dict | None = None,
):
    headers = {}
    if secret is not None:
        headers[PROVISION_SECRET_HEADER] = secret
    if body_override is not None:
        body = body_override
    else:
        body = {"user_ref": user_ref}
        if label is not None:
            body["label"] = label
    return client.post("/keys/provision", json=body, headers=headers)


class _FakeStripePoster:
    """Records every Stripe POST so tests can assert the exact payload."""

    def __init__(self, response=None):
        self.response = response or {
            "id": "cs_phase11_test",
            "url": "https://checkout.stripe.com/c/cs_phase11_test",
        }
        self.calls: list[dict] = []

    def __call__(self, *, url, data, auth, timeout):
        self.calls.append({"url": url, "data": dict(data), "auth": auth})
        return dict(self.response)


@pytest.fixture
def stripe_env(monkeypatch):
    """Stripe + public-base-url env so /billing/checkout can succeed."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_phase11")
    monkeypatch.setenv("STRIPE_PREMIUM_PRICE_ID", "price_premium_default")
    monkeypatch.setenv("TRADING_PUBLIC_BASE_URL", "https://api.example.com")
    fake = _FakeStripePoster()
    from trading_bot.api import billing as _billing_mod
    monkeypatch.setattr(_billing_mod, "_post_to_stripe", fake)
    return fake


def _issue_manifest_key(tier: str = "free", label: str = "phase11") -> tuple[str, str]:
    from trading_bot.api.keys import issue_key
    result = issue_key(tier=tier, label=label)
    return result["api_key"], result["key_hash"]


# ===========================================================================
# POST /keys/provision — guard
# ===========================================================================


class TestProvisionGuard:
    def test_unset_secret_returns_503_naming_env_var(
        self, client: TestClient, monkeypatch,
    ):
        monkeypatch.delenv(PROVISION_SECRET_ENV_VAR, raising=False)
        r = _provision(client)
        assert r.status_code == 503
        assert PROVISION_SECRET_ENV_VAR in r.json()["detail"]

    def test_missing_header_returns_403(self, client: TestClient):
        r = _provision(client, secret=None)
        assert r.status_code == 403
        assert "provision secret" in r.json()["detail"].lower()

    def test_wrong_header_returns_403(self, client: TestClient):
        r = _provision(client, secret="not-the-secret")
        assert r.status_code == 403

    def test_guard_rejects_before_body_validation(self, client: TestClient):
        """A caller without the secret must get 403 even for a
        malformed body — no schema oracle for unauthenticated
        callers."""
        r = _provision(client, secret="wrong", body_override={"nope": 1})
        assert r.status_code == 403

    def test_does_not_require_api_key(self, client: TestClient):
        """No Authorization header anywhere — the endpoint's job is
        to mint the caller's first key."""
        r = _provision(client)
        assert r.status_code == 200, r.text

    def test_secret_never_echoed(self, client: TestClient):
        r = _provision(client)
        assert PROVISION_SECRET not in r.text


# ===========================================================================
# POST /keys/provision — body validation
# ===========================================================================


class TestProvisionValidation:
    def test_missing_user_ref_returns_422(self, client: TestClient):
        r = _provision(client, body_override={})
        assert r.status_code == 422

    def test_empty_user_ref_returns_422(self, client: TestClient):
        r = _provision(client, user_ref="")
        assert r.status_code == 422

    def test_whitespace_user_ref_returns_422(self, client: TestClient):
        r = _provision(client, user_ref="   ")
        assert r.status_code == 422

    def test_overlong_user_ref_returns_422(self, client: TestClient):
        r = _provision(client, user_ref="a" * 129)
        assert r.status_code == 422
        assert "user_ref" in r.json()["detail"]

    @pytest.mark.parametrize("bad", [
        "bad ref",
        "../../etc/passwd",
        "ref\nnewline",
        "ref;drop table",
        "réf-unicode",
        "ref<script>",
    ])
    def test_unsafe_charset_returns_422(self, client: TestClient, bad: str):
        r = _provision(client, user_ref=bad)
        assert r.status_code == 422

    @pytest.mark.parametrize("good", [
        "a",
        "user@example.com",
        "b7f9c2d0-1a2b-4c3d-8e9f-000011112222",
        "user_name.dot-dash",
        "a" * 128,
    ])
    def test_safe_charset_accepted(self, client: TestClient, good: str):
        r = _provision(client, user_ref=good)
        assert r.status_code == 200, r.text

    def test_overlong_label_returns_422(self, client: TestClient):
        r = _provision(client, label="x" * 129)
        assert r.status_code == 422
        assert "label" in r.json()["detail"]

    def test_optional_label_accepted(self, client: TestClient):
        r = _provision(client, label="Alice's laptop")
        assert r.status_code == 200, r.text


# ===========================================================================
# POST /keys/provision — happy path
# ===========================================================================


class TestProvisionHappyPath:
    def test_response_shape(self, client: TestClient):
        r = _provision(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"api_key", "tier", "rotated"}
        assert isinstance(body["api_key"], str)
        assert len(body["api_key"]) >= 32
        assert body["tier"] == "free"
        assert body["rotated"] is False

    def test_issued_key_authenticates_as_free_manifest_key(
        self, client: TestClient,
    ):
        raw = _provision(client).json()["api_key"]
        entry = key_store.verify_api_key(raw)
        assert entry is not None
        assert entry.tier == "free"

    def test_manifest_row_uses_canonical_label_hash(
        self, client: TestClient, phase11_env,
    ):
        from trading_bot.api.keys import _hash_label
        raw = _provision(client).json()["api_key"]
        rows = [
            json.loads(line)
            for line in phase11_env["keys_manifest"].read_text(
                "utf-8",
            ).splitlines()
            if line.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["key_hash"] == key_store.hash_api_key(raw)
        assert rows[0]["label_hash"] == _hash_label(f"user:{USER_REF}")

    def test_raw_key_never_persisted(
        self, client: TestClient, phase11_env,
    ):
        raw = _provision(client).json()["api_key"]
        for name in ("keys_manifest", "keys_revoked", "audit_log",
                     "usage_log", "stripe_cache"):
            path = phase11_env[name]
            if path.exists():
                assert raw not in path.read_text("utf-8"), (
                    f"raw provisioned key leaked into {path}"
                )

    def test_provisioned_key_works_on_protected_endpoint(
        self, client: TestClient,
    ):
        raw = _provision(client).json()["api_key"]
        r = client.get(
            "/billing/status",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        assert r.json()["tier"] == "free"


# ===========================================================================
# POST /keys/provision — idempotent rotation
# ===========================================================================


class TestProvisionRotation:
    def test_repeat_call_rotates_previous_key(self, client: TestClient):
        first = _provision(client).json()
        second = _provision(client).json()
        assert second["rotated"] is True
        assert second["api_key"] != first["api_key"]
        old_hash = key_store.hash_api_key(first["api_key"])
        assert key_store.is_revoked(old_hash) is True
        assert key_store.verify_api_key(first["api_key"]) is None
        assert key_store.verify_api_key(second["api_key"]) is not None

    def test_rotated_out_key_rejected_by_auth(self, client: TestClient):
        first_raw = _provision(client).json()["api_key"]
        _provision(client)
        r = client.get(
            "/billing/status",
            headers={"Authorization": f"Bearer {first_raw}"},
        )
        assert r.status_code == 403

    def test_three_calls_leave_exactly_one_live_key(
        self, client: TestClient,
    ):
        raws = [_provision(client).json()["api_key"] for _ in range(3)]
        live = [r for r in raws if key_store.verify_api_key(r) is not None]
        assert live == [raws[-1]]

    def test_different_user_refs_do_not_rotate_each_other(
        self, client: TestClient,
    ):
        a = _provision(client, user_ref="user-a").json()
        b = _provision(client, user_ref="user-b").json()
        assert b["rotated"] is False
        assert key_store.verify_api_key(a["api_key"]) is not None
        assert key_store.verify_api_key(b["api_key"]) is not None

    def test_rotation_writes_revocation_reason(
        self, client: TestClient, phase11_env,
    ):
        _provision(client)
        _provision(client)
        revoked_body = phase11_env["keys_revoked"].read_text("utf-8")
        assert "rotated via /keys/provision" in revoked_body


# ===========================================================================
# POST /keys/provision — premium carry-over
# ===========================================================================


class TestProvisionPremiumCarryOver:
    def test_premium_carries_over_to_rotated_key(self, client: TestClient):
        from trading_bot.api import billing
        first_raw = _provision(client).json()["api_key"]
        first_hash = key_store.hash_api_key(first_raw)
        billing.add_premium_hash(first_hash)

        second = _provision(client).json()
        second_hash = key_store.hash_api_key(second["api_key"])
        assert second["tier"] == "premium"
        assert second["rotated"] is True
        assert billing.is_premium_hash(second_hash) is True
        # Stale hash dropped from the cache; old key revoked anyway.
        assert billing.is_premium_hash(first_hash) is False
        assert key_store.is_revoked(first_hash) is True

    def test_premium_key_still_premium_via_auth_after_rotation(
        self, client: TestClient, monkeypatch,
    ):
        from trading_bot.api import billing
        first_raw = _provision(client).json()["api_key"]
        billing.add_premium_hash(key_store.hash_api_key(first_raw))
        new_raw = _provision(client).json()["api_key"]
        # The Stripe-cache premium path requires Stripe to be
        # configured (same rule as require_api_key).
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_carryover")
        r = client.get(
            "/billing/status",
            headers={"Authorization": f"Bearer {new_raw}"},
        )
        assert r.status_code == 200
        assert r.json() == {
            "tier": "premium",
            "premium": True,
            "plan_source": "manifest",
        }

    def test_free_rotation_never_grants_premium(self, client: TestClient):
        from trading_bot.api import billing
        _provision(client)
        second = _provision(client).json()
        assert second["tier"] == "free"
        assert billing.is_premium_hash(
            key_store.hash_api_key(second["api_key"]),
        ) is False


# ===========================================================================
# POST /billing/checkout — tier-aware plans
# ===========================================================================


class TestCheckoutPlanPriceMapping:
    def test_no_body_uses_premium_price_and_default_plan(
        self, client: TestClient, stripe_env,
    ):
        raw, key_hash = _issue_manifest_key()
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        data = stripe_env.calls[0]["data"]
        assert data["line_items[0][price]"] == "price_premium_default"
        assert data["metadata[plan]"] == "pro"
        assert data["subscription_data[metadata][plan]"] == "pro"
        assert r.json()["plan"] == "pro"

    def test_plan_pro_uses_pro_price_id(
        self, client: TestClient, stripe_env, monkeypatch,
    ):
        monkeypatch.setenv("STRIPE_PRO_PRICE_ID", "price_pro_123")
        raw, _ = _issue_manifest_key()
        r = client.post(
            "/billing/checkout",
            json={"plan": "pro"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        data = stripe_env.calls[0]["data"]
        assert data["line_items[0][price]"] == "price_pro_123"
        assert data["metadata[plan]"] == "pro"
        assert r.json()["plan"] == "pro"

    def test_plan_elite_uses_elite_price_id(
        self, client: TestClient, stripe_env, monkeypatch,
    ):
        monkeypatch.setenv("STRIPE_ELITE_PRICE_ID", "price_elite_456")
        raw, _ = _issue_manifest_key()
        r = client.post(
            "/billing/checkout",
            json={"plan": "elite"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        data = stripe_env.calls[0]["data"]
        assert data["line_items[0][price]"] == "price_elite_456"
        assert data["metadata[plan]"] == "elite"
        assert data["subscription_data[metadata][plan]"] == "elite"
        assert r.json()["plan"] == "elite"

    def test_plan_pro_falls_back_to_premium_price(
        self, client: TestClient, stripe_env, monkeypatch,
    ):
        monkeypatch.delenv("STRIPE_PRO_PRICE_ID", raising=False)
        raw, _ = _issue_manifest_key()
        r = client.post(
            "/billing/checkout",
            json={"plan": "pro"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        data = stripe_env.calls[0]["data"]
        assert data["line_items[0][price]"] == "price_premium_default"
        assert data["metadata[plan]"] == "pro"

    def test_plan_elite_falls_back_to_premium_price(
        self, client: TestClient, stripe_env, monkeypatch,
    ):
        monkeypatch.delenv("STRIPE_ELITE_PRICE_ID", raising=False)
        raw, _ = _issue_manifest_key()
        r = client.post(
            "/billing/checkout",
            json={"plan": "elite"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        data = stripe_env.calls[0]["data"]
        assert data["line_items[0][price]"] == "price_premium_default"
        assert data["metadata[plan]"] == "elite"

    def test_plan_is_case_and_whitespace_insensitive(
        self, client: TestClient, stripe_env, monkeypatch,
    ):
        monkeypatch.setenv("STRIPE_ELITE_PRICE_ID", "price_elite_456")
        raw, _ = _issue_manifest_key()
        r = client.post(
            "/billing/checkout",
            json={"plan": " Elite "},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        assert stripe_env.calls[0]["data"]["metadata[plan]"] == "elite"

    def test_invalid_plan_returns_422(
        self, client: TestClient, stripe_env,
    ):
        raw, _ = _issue_manifest_key()
        r = client.post(
            "/billing/checkout",
            json={"plan": "platinum"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "platinum" in detail
        assert "pro" in detail and "elite" in detail
        # No Stripe call was made for a rejected plan.
        assert stripe_env.calls == []

    def test_existing_metadata_unchanged(
        self, client: TestClient, stripe_env,
    ):
        raw, key_hash = _issue_manifest_key()
        r = client.post(
            "/billing/checkout",
            json={"plan": "pro"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        data = stripe_env.calls[0]["data"]
        assert data["client_reference_id"] == key_hash
        assert data["metadata[key_hash]"] == key_hash
        assert data["metadata[tier_from]"] == "free"
        assert data["metadata[tier_to]"] == "premium"
        assert data["subscription_data[metadata][key_hash]"] == key_hash


class TestCheckoutSupabaseUserId:
    def test_supabase_user_id_forwarded_to_metadata(
        self, client: TestClient, stripe_env,
    ):
        raw, _ = _issue_manifest_key()
        r = client.post(
            "/billing/checkout",
            json={"plan": "pro", "supabase_user_id": "sb-user-777"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        data = stripe_env.calls[0]["data"]
        assert data["metadata[supabase_user_id]"] == "sb-user-777"
        assert data[
            "subscription_data[metadata][supabase_user_id]"
        ] == "sb-user-777"

    def test_absent_supabase_user_id_omitted_from_metadata(
        self, client: TestClient, stripe_env,
    ):
        raw, _ = _issue_manifest_key()
        r = client.post(
            "/billing/checkout",
            json={"plan": "pro"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        data = stripe_env.calls[0]["data"]
        assert "metadata[supabase_user_id]" not in data

    def test_unsafe_supabase_user_id_returns_422(
        self, client: TestClient, stripe_env,
    ):
        raw, _ = _issue_manifest_key()
        r = client.post(
            "/billing/checkout",
            json={"supabase_user_id": "bad id\nwith newline"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 422
        assert "supabase_user_id" in r.json()["detail"]
        assert stripe_env.calls == []


class TestCheckoutManifestGuard:
    """P1 fix: a key outside the issuance manifest could previously
    buy a session whose webhook fulfilment would be silently ignored
    (key_not_in_manifest_or_revoked). Such callers are now rejected
    with 409 BEFORE Stripe is contacted."""

    def test_env_key_caller_rejected_with_409(
        self, client: TestClient, stripe_env, monkeypatch,
    ):
        monkeypatch.setenv(API_KEY_ENV_VAR, "env-configured-key")
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": "Bearer env-configured-key"},
        )
        assert r.status_code == 409
        detail = r.json()["detail"].lower()
        assert "manifest" in detail
        assert "provision" in detail
        assert "profile" in detail
        # Fail-closed BEFORE any money can change hands.
        assert stripe_env.calls == []

    def test_manifest_key_caller_still_succeeds(
        self, client: TestClient, stripe_env, monkeypatch,
    ):
        # Even with the env key ALSO configured, a manifest caller
        # passes the guard.
        monkeypatch.setenv(API_KEY_ENV_VAR, "env-configured-key")
        raw, _ = _issue_manifest_key()
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text

    def test_provisioned_key_can_checkout(
        self, client: TestClient, stripe_env,
    ):
        raw = _provision(client).json()["api_key"]
        r = client.post(
            "/billing/checkout",
            json={"plan": "elite"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text


# ===========================================================================
# GET /billing/status
# ===========================================================================


class TestBillingStatus:
    def test_requires_auth(self, client: TestClient):
        _issue_manifest_key()  # deployment is "configured" → 401 not 503
        r = client.get("/billing/status")
        assert r.status_code == 401

    def test_bogus_key_rejected(self, client: TestClient):
        _issue_manifest_key()
        r = client.get(
            "/billing/status",
            headers={"Authorization": "Bearer bogus"},
        )
        assert r.status_code == 403

    def test_free_manifest_key(self, client: TestClient):
        raw, _ = _issue_manifest_key(tier="free")
        r = client.get(
            "/billing/status",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        assert r.json() == {
            "tier": "free",
            "premium": False,
            "plan_source": "manifest",
        }

    def test_premium_manifest_key(self, client: TestClient):
        raw, _ = _issue_manifest_key(tier="premium")
        r = client.get(
            "/billing/status",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        assert r.json() == {
            "tier": "premium",
            "premium": True,
            "plan_source": "manifest",
        }

    def test_env_key_source(self, client: TestClient, monkeypatch):
        monkeypatch.setenv(API_KEY_ENV_VAR, "env-status-key")
        r = client.get(
            "/billing/status",
            headers={"Authorization": "Bearer env-status-key"},
        )
        assert r.status_code == 200
        assert r.json() == {
            "tier": "free",
            "premium": False,
            "plan_source": "env_key",
        }

    def test_premium_allowlist_source(self, client: TestClient, monkeypatch):
        monkeypatch.setenv("TRADING_API_PREMIUM_KEYS", "allowlisted-key")
        r = client.get(
            "/billing/status",
            headers={"Authorization": "Bearer allowlisted-key"},
        )
        assert r.status_code == 200
        assert r.json() == {
            "tier": "premium",
            "premium": True,
            "plan_source": "premium_allowlist",
        }

    def test_stripe_cache_premium_manifest_key(
        self, client: TestClient, monkeypatch,
    ):
        from trading_bot.api import billing
        raw, key_hash = _issue_manifest_key(tier="free")
        billing.add_premium_hash(key_hash)
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_status")
        r = client.get(
            "/billing/status",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        assert r.json() == {
            "tier": "premium",
            "premium": True,
            "plan_source": "manifest",
        }


# ===========================================================================
# Full journey — provision → checkout → webhook → status
# ===========================================================================


class TestProvisionCheckoutFulfilmentJourney:
    def test_end_to_end_free_to_premium(
        self, client: TestClient, stripe_env, monkeypatch,
    ):
        from trading_bot.api import billing

        # 1. Frontend provisions a key for its signed-in user.
        provisioned = _provision(client, user_ref="journey-user").json()
        raw = provisioned["api_key"]
        key_hash = key_store.hash_api_key(raw)
        assert provisioned["tier"] == "free"

        # 2. User starts checkout for the elite plan.
        monkeypatch.setenv("STRIPE_ELITE_PRICE_ID", "price_elite_journey")
        r = client.post(
            "/billing/checkout",
            json={"plan": "elite", "supabase_user_id": "journey-user"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        sent = stripe_env.calls[0]["data"]
        assert sent["line_items[0][price]"] == "price_elite_journey"
        assert sent["metadata[key_hash]"] == key_hash
        assert sent["metadata[plan]"] == "elite"
        assert sent["metadata[supabase_user_id]"] == "journey-user"

        # 3. Stripe fires checkout.session.completed with the SAME
        # metadata — the manifest gate passes and premium is granted.
        result = billing.handle_webhook_event({
            "id": "evt_journey_1",
            "type": "checkout.session.completed",
            "data": {"object": {
                "metadata": {
                    "key_hash": key_hash,
                    "plan": "elite",
                    "supabase_user_id": "journey-user",
                },
            }},
        })
        assert result["action"] == "added"

        # 4. The success page polls /billing/status and sees premium.
        r = client.get(
            "/billing/status",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        assert r.json()["premium"] is True
        assert r.json()["tier"] == "premium"
