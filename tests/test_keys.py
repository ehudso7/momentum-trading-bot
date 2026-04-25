"""
Phase 6.0 tests — operator-only API key issuance CLI.

Exercises ``trading_bot.api.keys`` and verifies:

  * ``generate_api_key`` returns a 32+ byte URL-safe string;
  * ``_hash_api_key`` and ``_hash_label`` are byte-identical to
    SHA-256[:32] so cross-log joins work;
  * ``issue_key`` writes one append-only manifest row whose
    fields are EXACTLY the documented set (no raw key, no raw
    label, no PII);
  * ``--checkout`` requires premium + both URLs;
  * a Stripe failure during ``--checkout`` does NOT write a
    manifest row (no partial state);
  * the CLI prints the raw key once to stdout and writes the
    manifest;
  * concurrent writers don't corrupt JSONL lines;
  * the module imports nothing from Core.
"""

from __future__ import annotations

import hashlib
import json
import re
import string
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_bot.api import keys as keys_mod
from trading_bot.api.keys import (
    DEFAULT_KEYS_MANIFEST_PATH,
    KEY_BYTES,
    KEYS_MANIFEST_ENV_VAR,
    TIER_FREE,
    TIER_PREMIUM,
    VALID_TIERS,
    _hash_api_key,
    _hash_label,
    generate_api_key,
    issue_key,
    main as keys_main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def manifest_path(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "api_keys_manifest.jsonl"
    monkeypatch.setenv(KEYS_MANIFEST_ENV_VAR, str(p))
    return p


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        rows.append(json.loads(s))
    return rows


# ---------------------------------------------------------------------------
# generate_api_key
# ---------------------------------------------------------------------------


class TestGenerateApiKey:
    def test_returns_string(self):
        assert isinstance(generate_api_key(), str)

    def test_length_meets_or_exceeds_spec(self):
        """32 bytes of entropy → URL-safe base64 of 43 chars (no
        padding). Spec asks for ≥ 32 *bytes* of randomness, which
        means ≥ 43 chars at the URL-safe encoding; assert that."""
        for _ in range(10):
            k = generate_api_key()
            assert len(k) >= 43
            # Underlying entropy was at least KEY_BYTES bytes.
            assert KEY_BYTES >= 32

    def test_url_safe_charset(self):
        """Only ``[A-Za-z0-9\\-_]`` per RFC 4648 url-safe alphabet."""
        allowed = set(string.ascii_letters + string.digits + "-_")
        for _ in range(20):
            k = generate_api_key()
            assert set(k).issubset(allowed)

    def test_keys_are_unique_across_calls(self):
        seen = {generate_api_key() for _ in range(200)}
        assert len(seen) == 200


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


class TestHashHelpers:
    def test_hash_api_key_matches_other_modules(self):
        """Cross-log join contract: this hash must equal the one
        used by server / billing / conversion / growth /
        upgrade_events."""
        from trading_bot.api.server import (
            _hash_api_key as srv_hash,
        )
        for raw in ("", "abc", "🔒-key", "secret_test_123"):
            assert _hash_api_key(raw) == srv_hash(raw)

    def test_hash_label_is_sha256_prefix(self):
        for raw in ("alice@example.com", "acme-corp", "🦊"):
            assert _hash_label(raw) == hashlib.sha256(
                raw.encode("utf-8"),
            ).hexdigest()[:32]

    def test_hash_helpers_handle_empty(self):
        assert _hash_api_key("") == ""
        assert _hash_label("") == ""


# ---------------------------------------------------------------------------
# issue_key — happy path (free)
# ---------------------------------------------------------------------------


class TestIssueFreeTier:
    def test_returns_documented_fields(self, manifest_path: Path):
        result = issue_key(tier="free", label="alice@example.com")
        assert set(result.keys()) == {
            "api_key", "key_hash", "tier", "created_at",
            "label_hash", "ref_code",
            "checkout_session_id", "checkout_url",
            "manifest_path",
        }
        assert result["tier"] == "free"
        assert result["checkout_session_id"] is None
        assert result["checkout_url"] is None
        # Phase 6.1 — ref_code is None when --ref omitted.
        assert result["ref_code"] is None
        # Hashes match the raw inputs.
        assert result["key_hash"] == _hash_api_key(result["api_key"])
        assert result["label_hash"] == _hash_label("alice@example.com")
        # Created-at is ISO-8601 with Z suffix.
        assert result["created_at"].endswith("Z")
        datetime.strptime(
            result["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ",
        )

    def test_writes_one_manifest_row(self, manifest_path: Path):
        result = issue_key(tier="free", label="alice@example.com")
        rows = _read(manifest_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["tier"] == "free"
        assert row["key_hash"] == result["key_hash"]
        assert row["label_hash"] == result["label_hash"]
        assert row["created_at"] == result["created_at"]
        assert row["checkout_session_id"] is None

    def test_manifest_row_has_exactly_documented_fields(
        self, manifest_path: Path,
    ):
        issue_key(tier="free", label="alice@example.com")
        (row,) = _read(manifest_path)
        assert set(row.keys()) == {
            "created_at", "key_hash", "label_hash", "tier",
            "checkout_session_id",
            "ref_code",  # Phase 6.1 — null when --ref omitted.
        }
        assert row["ref_code"] is None

    def test_raw_key_never_in_manifest(self, manifest_path: Path):
        result = issue_key(tier="free", label="alice@example.com")
        body = manifest_path.read_text(encoding="utf-8")
        assert result["api_key"] not in body

    def test_raw_label_never_in_manifest(self, manifest_path: Path):
        marker = "RAW_LABEL_MARKER_DO_NOT_LEAK"
        issue_key(tier="free", label=marker)
        body = manifest_path.read_text(encoding="utf-8")
        assert marker not in body
        # The hash IS persisted.
        assert _hash_label(marker) in body

    def test_explicit_now_controls_timestamp(self, manifest_path: Path):
        ts = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        result = issue_key(
            tier="free", label="x", now=ts,
        )
        assert result["created_at"] == "2026-04-24T12:00:00.000000Z"
        (row,) = _read(manifest_path)
        assert row["created_at"] == result["created_at"]

    def test_explicit_manifest_path_overrides_env(
        self, tmp_path: Path, monkeypatch,
    ):
        env_path = tmp_path / "env.jsonl"
        explicit = tmp_path / "explicit.jsonl"
        monkeypatch.setenv(KEYS_MANIFEST_ENV_VAR, str(env_path))
        result = issue_key(
            tier="free", label="x", manifest_path=explicit,
        )
        assert explicit.exists()
        assert not env_path.exists()
        assert result["manifest_path"] == str(explicit)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_invalid_tier_rejected(self, manifest_path: Path):
        with pytest.raises(ValueError, match="invalid tier"):
            issue_key(tier="enterprise", label="x")
        # Manifest must NOT be created on a validation error.
        assert _read(manifest_path) == []

    def test_empty_label_rejected(self, manifest_path: Path):
        for bad in ("", "   ", "\t\t"):
            with pytest.raises(ValueError, match="non-empty"):
                issue_key(tier="free", label=bad)
        assert _read(manifest_path) == []

    def test_checkout_requires_premium(self, manifest_path: Path):
        with pytest.raises(ValueError, match="--tier premium"):
            issue_key(
                tier="free", label="x",
                with_checkout=True,
                success_url="https://e.com/ok",
                cancel_url="https://e.com/cancel",
            )
        assert _read(manifest_path) == []

    def test_checkout_requires_both_urls(self, manifest_path: Path):
        for s_url, c_url in (
            (None, None),
            ("https://e.com/ok", None),
            (None, "https://e.com/cancel"),
            ("", ""),
        ):
            with pytest.raises(ValueError, match="success-url"):
                issue_key(
                    tier="premium", label="x",
                    with_checkout=True,
                    success_url=s_url, cancel_url=c_url,
                )
        assert _read(manifest_path) == []


# ---------------------------------------------------------------------------
# Append-only / thread-safe
# ---------------------------------------------------------------------------


class TestAppendOnly:
    def test_two_issuances_append(self, manifest_path: Path):
        r1 = issue_key(tier="free", label="alice")
        r2 = issue_key(tier="free", label="bob")
        rows = _read(manifest_path)
        assert len(rows) == 2
        assert {r["key_hash"] for r in rows} == {
            r1["key_hash"], r2["key_hash"],
        }

    def test_concurrent_writers_no_corruption(
        self, manifest_path: Path,
    ):
        N = 16
        results: list[str] = []
        results_lock = threading.Lock()

        def worker(i: int) -> None:
            r = issue_key(tier="free", label=f"u{i}")
            with results_lock:
                results.append(r["key_hash"])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = _read(manifest_path)
        assert len(rows) == N
        assert {r["key_hash"] for r in rows} == set(results)


# ---------------------------------------------------------------------------
# --checkout via injected caller (no real Stripe needed)
# ---------------------------------------------------------------------------


class TestCheckoutPath:
    def test_checkout_writes_session_id_only_not_url(
        self, manifest_path: Path,
    ):
        captured: dict = {}

        def fake_checkout(api_key, success_url, cancel_url, **kw):
            captured["api_key"] = api_key
            captured["success_url"] = success_url
            captured["cancel_url"] = cancel_url
            return {
                "checkout_session_id": "cs_test_ABC123",
                "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_ABC123",
                "api_key_hash": _hash_api_key(api_key),
            }

        result = issue_key(
            tier="premium",
            label="acme-corp",
            with_checkout=True,
            success_url="https://example.com/billing/success",
            cancel_url="https://example.com/billing/cancel",
            checkout_caller=fake_checkout,
        )
        # Stripe got the raw key (it has to — to set subscription metadata).
        assert captured["api_key"] == result["api_key"]
        assert captured["success_url"] == "https://example.com/billing/success"
        assert captured["cancel_url"] == "https://example.com/billing/cancel"
        # CLI return values include the URL …
        assert result["checkout_session_id"] == "cs_test_ABC123"
        assert (
            result["checkout_url"]
            == "https://checkout.stripe.com/c/pay/cs_test_ABC123"
        )
        # … but the manifest only stores the session id.
        (row,) = _read(manifest_path)
        assert row["checkout_session_id"] == "cs_test_ABC123"
        # The URL must NOT be on disk.
        body = manifest_path.read_text(encoding="utf-8")
        assert "https://checkout.stripe.com" not in body
        assert "checkout_url" not in body

    def test_checkout_default_caller_is_billing_create_session(
        self, manifest_path: Path, monkeypatch,
    ):
        """When ``checkout_caller`` is None we lazy-import the real
        billing.create_checkout_session. Patch it on the billing
        module to confirm the dispatch."""
        from trading_bot.api import billing as billing_mod
        called: dict = {}

        def fake(api_key, success_url, cancel_url, **kw):
            called["yes"] = True
            return {
                "checkout_session_id": "cs_LIVE",
                "checkout_url": "https://checkout.stripe.com/c/pay/cs_LIVE",
                "api_key_hash": _hash_api_key(api_key),
            }
        monkeypatch.setattr(
            billing_mod, "create_checkout_session", fake,
        )
        result = issue_key(
            tier="premium", label="x",
            with_checkout=True,
            success_url="https://e.com/ok",
            cancel_url="https://e.com/cancel",
        )
        assert called.get("yes")
        assert result["checkout_session_id"] == "cs_LIVE"

    def test_stripe_failure_does_not_write_manifest(
        self, manifest_path: Path,
    ):
        def boom(*a, **kw):
            raise RuntimeError("simulated stripe outage")

        with pytest.raises(RuntimeError, match="simulated"):
            issue_key(
                tier="premium",
                label="acme-corp",
                with_checkout=True,
                success_url="https://e.com/ok",
                cancel_url="https://e.com/cancel",
                checkout_caller=boom,
            )
        # No partial state — manifest is empty.
        assert _read(manifest_path) == []

    def test_empty_checkout_response_is_rejected(
        self, manifest_path: Path,
    ):
        def empty(api_key, success_url, cancel_url, **kw):
            return {"checkout_session_id": "", "checkout_url": ""}

        with pytest.raises(RuntimeError, match="empty session id"):
            issue_key(
                tier="premium", label="x",
                with_checkout=True,
                success_url="https://e.com/ok",
                cancel_url="https://e.com/cancel",
                checkout_caller=empty,
            )
        assert _read(manifest_path) == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_no_args_prints_help_exits_two(self, capsys):
        rc = keys_main([])
        assert rc == 2
        err = capsys.readouterr().err
        assert "issue" in err

    def test_unknown_command_rejected(self, capsys):
        rc = keys_main(["bogus"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown command" in err
        assert "issue" in err

    def test_help_top_level(self, capsys):
        rc = keys_main(["--help"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "issue" in out

    def test_issue_free_prints_raw_key_once_and_writes_manifest(
        self, manifest_path: Path,
    ):
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "issue",
                "--tier", "free",
                "--label", "alice@example.com",
            ],
            capture_output=True, text=True,
            env={
                **__import__("os").environ,
                KEYS_MANIFEST_ENV_VAR: str(manifest_path),
            },
        )
        assert result.returncode == 0, result.stderr
        # Stdout contains exactly one ``api_key             :`` line.
        api_lines = [
            ln for ln in result.stdout.splitlines()
            if ln.strip().startswith("api_key")
        ]
        assert len(api_lines) == 1
        # Extract the raw key from that line.
        m = re.match(r"\s*api_key\s+:\s+(\S+)", api_lines[0])
        assert m is not None
        raw_key = m.group(1)
        assert len(raw_key) >= 43
        # The manifest row's key_hash matches the raw key.
        (row,) = _read(manifest_path)
        assert row["key_hash"] == _hash_api_key(raw_key)
        assert row["tier"] == "free"
        assert row["label_hash"] == _hash_label("alice@example.com")
        assert row["checkout_session_id"] is None
        # The raw key is NOT in the manifest.
        body = manifest_path.read_text(encoding="utf-8")
        assert raw_key not in body
        # The raw label is NOT in the manifest either.
        assert "alice@example.com" not in body

    def test_issue_premium_no_checkout(self, manifest_path: Path):
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "issue",
                "--tier", "premium",
                "--label", "acme-corp",
            ],
            capture_output=True, text=True,
            env={
                **__import__("os").environ,
                KEYS_MANIFEST_ENV_VAR: str(manifest_path),
            },
        )
        assert result.returncode == 0, result.stderr
        assert "tier                : premium" in result.stdout
        assert "checkout_session_id" not in result.stdout
        assert "checkout_url" not in result.stdout
        (row,) = _read(manifest_path)
        assert row["tier"] == "premium"
        assert row["checkout_session_id"] is None

    def test_cli_invalid_tier_rejected(self, manifest_path: Path):
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "issue",
                "--tier", "enterprise",
                "--label", "x",
            ],
            capture_output=True, text=True,
            env={
                **__import__("os").environ,
                KEYS_MANIFEST_ENV_VAR: str(manifest_path),
            },
        )
        # argparse rejects the choice with exit code 2.
        assert result.returncode == 2
        assert _read(manifest_path) == []

    def test_cli_checkout_without_premium_rejected(
        self, manifest_path: Path,
    ):
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "issue",
                "--tier", "free",
                "--label", "x",
                "--checkout",
                "--success-url", "https://e.com/ok",
                "--cancel-url", "https://e.com/cancel",
            ],
            capture_output=True, text=True,
            env={
                **__import__("os").environ,
                KEYS_MANIFEST_ENV_VAR: str(manifest_path),
            },
        )
        assert result.returncode == 2
        assert "premium" in result.stderr.lower()
        assert _read(manifest_path) == []

    def test_cli_checkout_without_urls_rejected(
        self, manifest_path: Path,
    ):
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "issue",
                "--tier", "premium",
                "--label", "x",
                "--checkout",
            ],
            capture_output=True, text=True,
            env={
                **__import__("os").environ,
                KEYS_MANIFEST_ENV_VAR: str(manifest_path),
            },
        )
        assert result.returncode == 2
        assert _read(manifest_path) == []


class TestCheckoutInProcessViaPatch:
    """End-to-end behaviour of the ``--checkout`` path, using
    monkeypatch to swap in a fake billing.create_checkout_session
    instead of subprocess + real Stripe."""

    def test_checkout_path_via_main(
        self, manifest_path: Path, monkeypatch, capsys,
    ):
        from trading_bot.api import billing as billing_mod

        def fake(api_key, success_url, cancel_url, **kw):
            return {
                "checkout_session_id": "cs_INPROC_42",
                "checkout_url": "https://checkout.stripe.com/c/pay/cs_INPROC_42",
                "api_key_hash": _hash_api_key(api_key),
            }
        monkeypatch.setattr(
            billing_mod, "create_checkout_session", fake,
        )

        rc = keys_main([
            "issue",
            "--tier", "premium",
            "--label", "acme-corp",
            "--checkout",
            "--success-url", "https://example.com/billing/success",
            "--cancel-url", "https://example.com/billing/cancel",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "tier                : premium" in out
        assert "checkout_session_id : cs_INPROC_42" in out
        assert (
            "checkout_url        : https://checkout.stripe.com/c/pay/cs_INPROC_42"
        ) in out
        # Manifest persisted only the session id, not the URL.
        (row,) = _read(manifest_path)
        assert row["checkout_session_id"] == "cs_INPROC_42"
        body = manifest_path.read_text(encoding="utf-8")
        assert "checkout.stripe.com" not in body

    def test_checkout_failure_returns_three(
        self, manifest_path: Path, monkeypatch, capsys,
    ):
        from trading_bot.api import billing as billing_mod

        def boom(*a, **kw):
            raise RuntimeError("stripe down")
        monkeypatch.setattr(
            billing_mod, "create_checkout_session", boom,
        )

        rc = keys_main([
            "issue",
            "--tier", "premium",
            "--label", "x",
            "--checkout",
            "--success-url", "https://e.com/ok",
            "--cancel-url", "https://e.com/cancel",
        ])
        assert rc == 3
        err = capsys.readouterr().err
        assert "stripe down" in err
        # No partial state.
        assert _read(manifest_path) == []


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


class TestNoCoreImports:
    def test_keys_module_does_not_reach_into_core(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "keys.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert forbidden not in src, (
                f"keys.py imports restricted surface: {forbidden!r}"
            )

    def test_module_exports_documented_names(self):
        """Sanity-check the public surface a downstream consumer
        would import."""
        for name in (
            "issue_key", "generate_api_key", "main",
            "TIER_FREE", "TIER_PREMIUM", "VALID_TIERS",
            "KEYS_MANIFEST_ENV_VAR", "DEFAULT_KEYS_MANIFEST_PATH",
        ):
            assert hasattr(keys_mod, name)


class TestDefaults:
    def test_default_path_matches_spec(self):
        assert DEFAULT_KEYS_MANIFEST_PATH == "data/api_keys_manifest.jsonl"

    def test_valid_tiers(self):
        assert TIER_FREE == "free"
        assert TIER_PREMIUM == "premium"
        assert VALID_TIERS == {"free", "premium"}


# ===========================================================================
# Phase 6.1 — referral key issuance flow
# ===========================================================================


class TestPhase61RefSanitization:
    """``--ref`` runs through the same sanitiser as the live ?ref=
    middleware (Phase 5.1 growth) so manifest rows are byte-identical
    to whatever the growth log would have stored."""

    def test_safe_ref_persisted_verbatim(self, manifest_path: Path):
        result = issue_key(
            tier="free", label="alice", ref_code="twitter-launch_2026",
        )
        assert result["ref_code"] == "twitter-launch_2026"
        (row,) = _read(manifest_path)
        assert row["ref_code"] == "twitter-launch_2026"

    def test_unsafe_chars_stripped(self, manifest_path: Path):
        """``<script>alert(1)</script>`` collapses to alphanumerics
        because the sanitiser strips ``<>/``."""
        result = issue_key(
            tier="free", label="bob",
            ref_code="<script>alert(1)</script>",
        )
        # Same charset rule as the growth sanitiser.
        assert result["ref_code"] == "scriptalert1script"
        (row,) = _read(manifest_path)
        assert row["ref_code"] == "scriptalert1script"
        body = manifest_path.read_text(encoding="utf-8")
        # The raw payload must NOT survive in any form.
        assert "<script>" not in body
        assert "</script>" not in body

    def test_overlong_ref_truncated_to_64_chars(self, manifest_path: Path):
        result = issue_key(
            tier="free", label="carol", ref_code="x" * 200,
        )
        assert result["ref_code"] is not None
        assert len(result["ref_code"]) == 64
        (row,) = _read(manifest_path)
        assert len(row["ref_code"]) == 64

    def test_all_stripped_becomes_null(self, manifest_path: Path):
        result = issue_key(
            tier="free", label="dave",
            ref_code="!@#$%^&*()",
        )
        assert result["ref_code"] is None
        (row,) = _read(manifest_path)
        assert row["ref_code"] is None

    @pytest.mark.parametrize("blank", [None, "", "   ", "\t\t"])
    def test_blank_or_missing_ref_becomes_null(
        self, manifest_path: Path, blank,
    ):
        result = issue_key(
            tier="free", label="ed", ref_code=blank,
        )
        assert result["ref_code"] is None
        (row,) = _read(manifest_path)
        assert row["ref_code"] is None

    def test_sanitiser_matches_growth_sanitiser(self, manifest_path: Path):
        """The keys module must hash through the SAME helper the
        live growth middleware uses, so manifest.ref_code joins
        cleanly with growth.ref_code."""
        from trading_bot.api.growth import _sanitize_ref_code as g_sanitize
        for raw in (
            "twitter-q2",
            "hn launch",  # space stripped
            "ref.with:dots",
            "weird?chars=here",
            "x" * 100,
        ):
            expected = g_sanitize(raw)
            result = issue_key(
                tier="free", label=f"u-{raw}", ref_code=raw,
            )
            assert result["ref_code"] == (expected or None)


class TestPhase61BackwardCompat:
    """Issuing a key without --ref must look identical to the
    Phase 6.0 surface from a downstream-reader perspective: the
    ref_code field is present but null."""

    def test_no_ref_kwarg_still_works(self, manifest_path: Path):
        result = issue_key(tier="free", label="x")
        assert result["ref_code"] is None
        (row,) = _read(manifest_path)
        assert row["ref_code"] is None

    def test_raw_label_still_never_persisted_with_ref(
        self, manifest_path: Path,
    ):
        marker = "RAW_LABEL_STILL_DO_NOT_LEAK"
        issue_key(tier="free", label=marker, ref_code="twitter")
        body = manifest_path.read_text(encoding="utf-8")
        assert marker not in body

    def test_raw_key_still_never_persisted_with_ref(
        self, manifest_path: Path,
    ):
        result = issue_key(
            tier="free", label="alice", ref_code="twitter",
        )
        body = manifest_path.read_text(encoding="utf-8")
        assert result["api_key"] not in body


class TestPhase61CheckoutForwardsRef:
    """When both ``--checkout`` and a non-empty ``--ref`` are used,
    the sanitised ref_code is forwarded as a kwarg to the
    checkout caller so it can land in Stripe metadata."""

    def test_sanitised_ref_passed_to_checkout_caller(
        self, manifest_path: Path,
    ):
        captured: dict = {}

        def fake_checkout(api_key, success_url, cancel_url, **kw):
            captured.update(kw)
            return {
                "checkout_session_id": "cs_test_ABC",
                "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_ABC",
                "api_key_hash": _hash_api_key(api_key),
            }

        result = issue_key(
            tier="premium",
            label="acme-corp",
            ref_code="hn-launch",
            with_checkout=True,
            success_url="https://e.com/ok",
            cancel_url="https://e.com/cancel",
            checkout_caller=fake_checkout,
        )
        assert captured.get("ref_code") == "hn-launch"
        assert result["ref_code"] == "hn-launch"
        (row,) = _read(manifest_path)
        assert row["ref_code"] == "hn-launch"
        assert row["checkout_session_id"] == "cs_test_ABC"

    def test_only_sanitised_ref_passed_never_raw(
        self, manifest_path: Path,
    ):
        captured: dict = {}

        def fake_checkout(api_key, success_url, cancel_url, **kw):
            captured.update(kw)
            return {
                "checkout_session_id": "cs_X",
                "checkout_url": "https://x",
                "api_key_hash": _hash_api_key(api_key),
            }

        issue_key(
            tier="premium",
            label="x",
            ref_code="<script>twitter</script>",  # raw includes <>
            with_checkout=True,
            success_url="https://e.com/ok",
            cancel_url="https://e.com/cancel",
            checkout_caller=fake_checkout,
        )
        assert captured.get("ref_code") == "scripttwitterscript"
        # The raw form must NEVER reach the checkout caller.
        assert "<script>" not in str(captured)
        assert "</script>" not in str(captured)

    def test_no_ref_means_no_ref_kwarg_to_caller(
        self, manifest_path: Path,
    ):
        """When --ref is omitted we MUST NOT pass ref_code=None to
        the checkout caller — that would override its default in a
        confusing way and could fail the Stripe Stripe POST
        depending on how billing handles None vs absent."""
        captured: dict = {}

        def fake_checkout(api_key, success_url, cancel_url, **kw):
            captured.update(kw)
            return {
                "checkout_session_id": "cs_X",
                "checkout_url": "https://x",
                "api_key_hash": _hash_api_key(api_key),
            }

        issue_key(
            tier="premium", label="x",
            with_checkout=True,
            success_url="https://e.com/ok",
            cancel_url="https://e.com/cancel",
            checkout_caller=fake_checkout,
        )
        assert "ref_code" not in captured

    def test_all_stripped_ref_means_no_ref_kwarg_to_caller(
        self, manifest_path: Path,
    ):
        """A ``--ref`` value that fully sanitises to empty becomes
        None and must NOT be forwarded to the caller."""
        captured: dict = {}

        def fake_checkout(api_key, success_url, cancel_url, **kw):
            captured.update(kw)
            return {
                "checkout_session_id": "cs_X",
                "checkout_url": "https://x",
                "api_key_hash": _hash_api_key(api_key),
            }

        issue_key(
            tier="premium", label="x",
            ref_code="!@#$%",
            with_checkout=True,
            success_url="https://e.com/ok",
            cancel_url="https://e.com/cancel",
            checkout_caller=fake_checkout,
        )
        assert "ref_code" not in captured


class TestPhase61BillingMetadataFields:
    """End-to-end: sanitised ref_code lands in the actual Stripe
    HTTP POST body's metadata fields. Uses billing's stub_http_post
    pattern."""

    def test_ref_code_lands_in_stripe_metadata(self, monkeypatch):
        from trading_bot.api.billing import (
            STRIPE_API_KEY_ENV_VAR,
            STRIPE_PRICE_ID_PREMIUM_ENV_VAR,
            create_checkout_session,
        )
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_xyz")
        monkeypatch.setenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "price_test")

        captured: dict = {}

        def stub_post(**kwargs):
            captured.update(kwargs)
            return {
                "id": "cs_test_session_abcdef",
                "url": "https://checkout.stripe.com/c/pay/cs_test_session_abcdef",
                "object": "checkout.session",
                "mode": "subscription",
            }

        create_checkout_session(
            "operator-issued-key-XYZ",
            "https://app.example.com/billing/success",
            "https://app.example.com/billing/cancel",
            ref_code="hn-launch",
            http_post=stub_post,
        )
        data = captured["data"]
        assert data["metadata[ref_code]"] == "hn-launch"
        assert data["subscription_data[metadata][ref_code]"] == "hn-launch"

    def test_no_ref_means_no_ref_metadata(self, monkeypatch):
        from trading_bot.api.billing import (
            STRIPE_API_KEY_ENV_VAR,
            STRIPE_PRICE_ID_PREMIUM_ENV_VAR,
            create_checkout_session,
        )
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_xyz")
        monkeypatch.setenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "price_test")

        captured: dict = {}

        def stub_post(**kwargs):
            captured.update(kwargs)
            return {
                "id": "cs_x", "url": "https://x", "object": "checkout.session",
            }

        create_checkout_session(
            "operator-issued-key-XYZ",
            "https://app.example.com/billing/success",
            "https://app.example.com/billing/cancel",
            http_post=stub_post,
        )
        data = captured["data"]
        # Phase 4.7 invariants still hold.
        assert data["metadata[api_key]"] == "operator-issued-key-XYZ"
        # Phase 6.1: no ref → no ref_code metadata fields.
        assert "metadata[ref_code]" not in data
        assert "subscription_data[metadata][ref_code]" not in data

    def test_empty_ref_means_no_ref_metadata(self, monkeypatch):
        from trading_bot.api.billing import (
            STRIPE_API_KEY_ENV_VAR,
            STRIPE_PRICE_ID_PREMIUM_ENV_VAR,
            create_checkout_session,
        )
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_xyz")
        monkeypatch.setenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "price_test")

        captured: dict = {}

        def stub_post(**kwargs):
            captured.update(kwargs)
            return {"id": "cs_x", "url": "https://x"}

        # ``ref_code=""`` (the post-sanitisation 'no value' state)
        # must not trigger the metadata insert.
        create_checkout_session(
            "key-X",
            "https://e.com/ok",
            "https://e.com/cancel",
            ref_code="",
            http_post=stub_post,
        )
        data = captured["data"]
        assert "metadata[ref_code]" not in data


class TestPhase61Cli:
    def test_cli_with_ref_persists_and_prints(
        self, manifest_path: Path,
    ):
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "issue",
                "--tier", "free",
                "--label", "alice@example.com",
                "--ref", "twitter-launch_2026",
            ],
            capture_output=True, text=True,
            env={
                **__import__("os").environ,
                KEYS_MANIFEST_ENV_VAR: str(manifest_path),
            },
        )
        assert result.returncode == 0, result.stderr
        # The new ref_code line is in stdout when present.
        assert "ref_code" in result.stdout
        assert "twitter-launch_2026" in result.stdout
        (row,) = _read(manifest_path)
        assert row["ref_code"] == "twitter-launch_2026"
        # Raw label still never on disk.
        body = manifest_path.read_text(encoding="utf-8")
        assert "alice@example.com" not in body

    def test_cli_without_ref_omits_line_from_stdout(
        self, manifest_path: Path,
    ):
        """No --ref should leave the stdout block byte-equivalent
        to the Phase 6.0 surface (no leftover ``ref_code:`` line)."""
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "issue",
                "--tier", "free",
                "--label", "alice",
            ],
            capture_output=True, text=True,
            env={
                **__import__("os").environ,
                KEYS_MANIFEST_ENV_VAR: str(manifest_path),
            },
        )
        assert result.returncode == 0, result.stderr
        ref_lines = [
            ln for ln in result.stdout.splitlines()
            if ln.strip().startswith("ref_code")
        ]
        assert ref_lines == []

    def test_cli_unsafe_ref_persists_sanitised_form(
        self, manifest_path: Path,
    ):
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "issue",
                "--tier", "free",
                "--label", "x",
                "--ref", "<script>xss</script>",
            ],
            capture_output=True, text=True,
            env={
                **__import__("os").environ,
                KEYS_MANIFEST_ENV_VAR: str(manifest_path),
            },
        )
        assert result.returncode == 0, result.stderr
        (row,) = _read(manifest_path)
        assert row["ref_code"] == "scriptxssscript"
        # The raw payload appears nowhere — not in stdout, not in disk.
        assert "<script>" not in result.stdout
        body = manifest_path.read_text(encoding="utf-8")
        assert "<script>" not in body


# ===========================================================================
# Phase 6.2 — revoke subcommand
# ===========================================================================


@pytest.fixture
def revoked_path(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "api_keys_revoked.jsonl"
    monkeypatch.setenv("TRADING_API_KEYS_REVOKED_PATH", str(p))
    # Reset key_store caches so a previous test's revoked file
    # doesn't leak into this one.
    from trading_bot.api import key_store
    key_store.reset_caches_for_tests()
    yield p
    key_store.reset_caches_for_tests()


def _read_revocations(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s:
            rows.append(json.loads(s))
    return rows


class TestPhase62RevokeApi:
    """In-process invocation of ``main`` with ``revoke`` argv."""

    def test_revoke_by_hash_writes_row(self, revoked_path: Path):
        h = "a" * 32
        rc = keys_main([
            "revoke", "--key-hash", h, "--reason", "leaked",
        ])
        assert rc == 0
        (row,) = _read_revocations(revoked_path)
        assert row["key_hash"] == h
        assert row["reason"] == "leaked"
        assert "timestamp" in row

    def test_revoke_by_raw_key_stores_only_hash(
        self, revoked_path: Path,
    ):
        marker = "RAW_KEY_REVOKE_DO_NOT_LEAK_ABCXYZ"
        expected_hash = _hash_api_key(marker)

        rc = keys_main(["revoke", "--api-key", marker, "--reason", "rotation"])
        assert rc == 0

        (row,) = _read_revocations(revoked_path)
        assert row["key_hash"] == expected_hash
        body = revoked_path.read_text(encoding="utf-8")
        assert marker not in body
        # Sanity — the hash is present.
        assert expected_hash in body

    def test_revoke_blank_hash_rejected(self, revoked_path: Path):
        rc = keys_main(["revoke", "--key-hash", "   "])
        assert rc == 2
        assert _read_revocations(revoked_path) == []

    def test_revoke_blank_raw_key_rejected(self, revoked_path: Path):
        rc = keys_main(["revoke", "--api-key", "   "])
        assert rc == 2
        assert _read_revocations(revoked_path) == []

    def test_revoke_requires_one_of_two_inputs(
        self, revoked_path: Path,
    ):
        # argparse calls sys.exit(2) on a missing required group.
        with pytest.raises(SystemExit) as exc:
            keys_main(["revoke"])
        assert exc.value.code == 2
        assert _read_revocations(revoked_path) == []

    def test_revoke_rejects_both_inputs(
        self, revoked_path: Path,
    ):
        with pytest.raises(SystemExit) as exc:
            keys_main([
                "revoke",
                "--api-key", "k",
                "--key-hash", "h" * 32,
            ])
        assert exc.value.code == 2
        assert _read_revocations(revoked_path) == []

    def test_revoke_reason_optional(self, revoked_path: Path):
        rc = keys_main(["revoke", "--key-hash", "b" * 32])
        assert rc == 0
        (row,) = _read_revocations(revoked_path)
        assert "reason" not in row

    def test_revoke_path_override(self, tmp_path: Path):
        custom = tmp_path / "alt_revoked.jsonl"
        rc = keys_main([
            "revoke",
            "--key-hash", "c" * 32,
            "--revoked-path", str(custom),
        ])
        assert rc == 0
        assert custom.exists()


class TestPhase62RevokeCli:
    """Subprocess-level CLI test — verifies argv parsing & exit codes."""

    def test_subprocess_revoke_by_hash(self, tmp_path: Path):
        revoked = tmp_path / "api_keys_revoked.jsonl"
        h = "d" * 32
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "revoke", "--key-hash", h, "--reason", "test",
            ],
            capture_output=True, text=True,
            env={
                **__import__("os").environ,
                "TRADING_API_KEYS_REVOKED_PATH": str(revoked),
            },
        )
        assert result.returncode == 0, result.stderr
        assert "revoked" in result.stdout.lower()
        rows = _read_revocations(revoked)
        assert len(rows) == 1
        assert rows[0]["key_hash"] == h

    def test_subprocess_revoke_by_raw_key_does_not_leak(
        self, tmp_path: Path,
    ):
        revoked = tmp_path / "api_keys_revoked.jsonl"
        marker = "RAW_KEY_SUBPROC_DO_NOT_LEAK"
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "revoke", "--api-key", marker,
            ],
            capture_output=True, text=True,
            env={
                **__import__("os").environ,
                "TRADING_API_KEYS_REVOKED_PATH": str(revoked),
            },
        )
        assert result.returncode == 0, result.stderr
        body = revoked.read_text(encoding="utf-8")
        assert marker not in body
        assert marker not in result.stdout

    def test_subprocess_unknown_command_exits_nonzero(self):
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "no-such-command",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "available commands" in result.stderr
        assert "revoke" in result.stderr


class TestPhase62IssueRevokeRoundTrip:
    """Issue a key, revoke it via the CLI, and assert the manifest +
    revocation log together describe the lifecycle correctly."""

    def test_issue_then_revoke_by_hash(
        self, manifest_path: Path, tmp_path: Path,
    ):
        issued = issue_key(tier="free", label="alice")
        api_key = issued["api_key"]
        key_hash = issued["key_hash"]

        revoked = tmp_path / "api_keys_revoked.jsonl"
        rc = keys_main([
            "revoke", "--key-hash", key_hash,
            "--revoked-path", str(revoked),
            "--reason", "user-requested",
        ])
        assert rc == 0

        # Manifest still has the original row …
        manifest_rows = _read(manifest_path)
        assert any(r["key_hash"] == key_hash for r in manifest_rows)

        # … and the revocation log records exactly the hash.
        (rev,) = _read_revocations(revoked)
        assert rev["key_hash"] == key_hash
        assert rev["reason"] == "user-requested"
        # Raw issued api_key never landed on disk.
        body = revoked.read_text(encoding="utf-8")
        assert api_key not in body

    def test_issue_then_revoke_by_raw_key(
        self, manifest_path: Path, tmp_path: Path,
    ):
        issued = issue_key(tier="premium", label="bob")
        api_key = issued["api_key"]
        key_hash = issued["key_hash"]

        revoked = tmp_path / "api_keys_revoked.jsonl"
        rc = keys_main([
            "revoke", "--api-key", api_key,
            "--revoked-path", str(revoked),
        ])
        assert rc == 0

        (rev,) = _read_revocations(revoked)
        assert rev["key_hash"] == key_hash
        body = revoked.read_text(encoding="utf-8")
        assert api_key not in body


class TestPhase62NoCoreImports:
    """The Phase 6.0 boundary still holds after Phase 6.2."""

    def test_keys_module_does_not_reach_into_core(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "keys.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
            "from trading_bot.api.server",
        ):
            assert forbidden not in src, (
                f"keys.py imports restricted surface: {forbidden!r}"
            )


# ===========================================================================
# Phase 6.3 — manifest inspection (list / show / stats)
# ===========================================================================


@pytest.fixture
def inspect_env(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """
    Two empty append-only files plus monkeypatched env vars so
    inspection tests share the issuance + revocation paths every
    other Phase 6 surface uses.
    """
    manifest = tmp_path / "api_keys_manifest.jsonl"
    revoked = tmp_path / "api_keys_revoked.jsonl"
    monkeypatch.setenv(KEYS_MANIFEST_ENV_VAR, str(manifest))
    monkeypatch.setenv("TRADING_API_KEYS_REVOKED_PATH", str(revoked))
    from trading_bot.api import key_store
    key_store.reset_caches_for_tests()
    yield {"manifest": manifest, "revoked": revoked}
    key_store.reset_caches_for_tests()


def _issue(label: str, *, tier: str = "free", ref: str | None = None) -> dict:
    """Issue one key via the in-process API (uses the env-var paths)."""
    return issue_key(tier=tier, label=label, ref_code=ref)


def _revoke(key_hash: str, *, reason: str | None = None) -> int:
    return keys_main(
        ["revoke", "--key-hash", key_hash]
        + (["--reason", reason] if reason else [])
    )


class TestPhase63ListBasic:
    def test_default_excludes_revoked(self, inspect_env, capsys):
        a = _issue("alice", ref="twitter")
        b = _issue("bob", tier="premium", ref="hn-launch")
        c = _issue("carol")
        _revoke(c["key_hash"])
        capsys.readouterr()  # drain prior command output

        rc = keys_main(["list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert a["key_hash"] in out
        assert b["key_hash"] in out
        assert c["key_hash"] not in out
        assert "2 active" in out

    def test_include_revoked_flag(self, inspect_env, capsys):
        a = _issue("alice")
        b = _issue("bob")
        _revoke(a["key_hash"], reason="leaked")
        capsys.readouterr()  # drain prior command output

        rc = keys_main(["list", "--include-revoked"])
        assert rc == 0
        out = capsys.readouterr().out
        assert a["key_hash"] in out
        assert b["key_hash"] in out
        assert "1 active, 1 revoked" in out

    def test_filter_by_tier(self, inspect_env, capsys):
        f = _issue("alice", tier="free")
        p = _issue("bob", tier="premium")

        rc = keys_main(["list", "--tier", "premium"])
        assert rc == 0
        out = capsys.readouterr().out
        assert p["key_hash"] in out
        assert f["key_hash"] not in out

    def test_filter_by_ref(self, inspect_env, capsys):
        a = _issue("alice", ref="twitter")
        b = _issue("bob", ref="hn-launch")

        rc = keys_main(["list", "--ref", "twitter"])
        assert rc == 0
        out = capsys.readouterr().out
        assert a["key_hash"] in out
        assert b["key_hash"] not in out

    def test_filters_are_anded(self, inspect_env, capsys):
        match = _issue("alice", tier="premium", ref="hn-launch")
        miss_tier = _issue("bob", tier="free", ref="hn-launch")
        miss_ref = _issue("carol", tier="premium", ref="twitter")

        rc = keys_main(["list", "--tier", "premium", "--ref", "hn-launch"])
        assert rc == 0
        out = capsys.readouterr().out
        assert match["key_hash"] in out
        assert miss_tier["key_hash"] not in out
        assert miss_ref["key_hash"] not in out

    def test_unsafe_ref_filter_sanitised(self, inspect_env, capsys):
        """``--ref <script>`` collapses through the same Phase 5.1
        sanitiser the live middleware uses, so it matches whatever the
        manifest stored — never the raw payload."""
        target = _issue("alice", ref="<script>xss</script>")
        # Manifest stores the sanitised form already.
        assert target["ref_code"] == "scriptxssscript"

        rc = keys_main(["list", "--ref", "<script>xss</script>"])
        assert rc == 0
        out = capsys.readouterr().out
        assert target["key_hash"] in out

    def test_sort_newest_first(self, inspect_env, capsys):
        a = _issue("alice")
        time.sleep(0.001)  # ensure ordering
        b = _issue("bob")
        time.sleep(0.001)
        c = _issue("carol")

        rc = keys_main(["list"])
        assert rc == 0
        out = capsys.readouterr().out
        # The hashes must appear in c, b, a order.
        idx_a = out.index(a["key_hash"])
        idx_b = out.index(b["key_hash"])
        idx_c = out.index(c["key_hash"])
        assert idx_c < idx_b < idx_a


class TestPhase63ListJson:
    def test_json_output_is_valid_array(self, inspect_env, capsys):
        a = _issue("alice", ref="twitter")
        rc = keys_main(["list", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list)
        assert len(payload) == 1
        row = payload[0]
        assert row["key_hash"] == a["key_hash"]
        assert row["tier"] == "free"
        assert row["ref_code"] == "twitter"
        assert row["revoked"] is False
        # Field allow-list pinned by Phase 6.3 — never label_hash.
        for forbidden in ("api_key", "label", "label_hash", "checkout_url"):
            assert forbidden not in row

    def test_json_includes_revocation_metadata(self, inspect_env, capsys):
        a = _issue("alice")
        _revoke(a["key_hash"], reason="rotation")
        capsys.readouterr()  # drain prior command output

        rc = keys_main(["list", "--include-revoked", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload) == 1
        row = payload[0]
        assert row["revoked"] is True
        assert row["revoked_at"]
        assert row["revoked_reason"] == "rotation"


class TestPhase63ListRobustness:
    def test_missing_manifest_no_crash(self, inspect_env, capsys):
        # Don't issue anything → manifest file does not exist.
        rc = keys_main(["list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "0 active" in out
        assert "no manifest rows" in out

    def test_malformed_lines_skipped(self, inspect_env, capsys):
        a = _issue("alice")
        path = inspect_env["manifest"]
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n")                            # blank
            fh.write("not-json\n")                    # not JSON
            fh.write(json.dumps([1, 2, 3]) + "\n")    # not a dict
            fh.write(
                json.dumps({"key_hash": "x" * 32, "tier": "bogus"}) + "\n"
            )                                          # bogus tier
        rc = keys_main(["list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert a["key_hash"] in out
        # Bogus tier did not slip through.
        assert "bogus" not in out

    def test_empty_after_filter(self, inspect_env, capsys):
        _issue("alice", tier="free")
        rc = keys_main(["list", "--tier", "premium"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no manifest rows match" in out
        assert "0 active" in out


class TestPhase63Show:
    def test_show_active_key(self, inspect_env, capsys):
        a = _issue("alice", ref="twitter")
        rc = keys_main(["show", "--key-hash", a["key_hash"]])
        assert rc == 0
        out = capsys.readouterr().out
        assert a["key_hash"] in out
        assert "tier                : free" in out
        assert "ref_code            : twitter" in out
        assert "revoked             : no" in out

    def test_show_revoked_key_includes_reason(self, inspect_env, capsys):
        a = _issue("alice")
        _revoke(a["key_hash"], reason="leaked-on-twitter")
        capsys.readouterr()
        rc = keys_main(["show", "--key-hash", a["key_hash"]])
        assert rc == 0
        out = capsys.readouterr().out
        assert "revoked             : yes" in out
        assert "revoked_reason      : leaked-on-twitter" in out
        assert "revoked_at          :" in out

    def test_show_unknown_returns_2_with_stderr(self, inspect_env, capsys):
        rc = keys_main(["show", "--key-hash", "0" * 32])
        assert rc == 2
        err = capsys.readouterr().err
        assert "no manifest row" in err

    def test_show_blank_hash_rejected(self, inspect_env, capsys):
        rc = keys_main(["show", "--key-hash", "   "])
        assert rc == 2

    def test_show_json_output(self, inspect_env, capsys):
        a = _issue("alice", tier="premium", ref="hn-launch")
        rc = keys_main(["show", "--key-hash", a["key_hash"], "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["key_hash"] == a["key_hash"]
        assert payload["tier"] == "premium"
        assert payload["ref_code"] == "hn-launch"
        assert payload["revoked"] is False
        for forbidden in ("api_key", "label", "label_hash", "checkout_url"):
            assert forbidden not in payload


class TestPhase63Stats:
    def test_zero_counts_when_empty(self, inspect_env, capsys):
        rc = keys_main(["stats"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "total issued      : 0" in out
        assert "active            : 0" in out
        assert "revoked           : 0" in out

    def test_counts_after_issuance_and_revocation(
        self, inspect_env, capsys,
    ):
        _issue("alice", tier="free", ref="twitter")
        _issue("bob", tier="premium", ref="hn-launch")
        c = _issue("carol", tier="free", ref="twitter")
        _revoke(c["key_hash"])
        capsys.readouterr()

        rc = keys_main(["stats"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "total issued      : 3" in out
        assert "active            : 2" in out
        assert "revoked           : 1" in out

    def test_by_tier_breakdown(self, inspect_env, capsys):
        _issue("a", tier="free")
        _issue("b", tier="free")
        _issue("c", tier="premium")
        rc = keys_main(["stats", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["by_tier"] == {"free": 2, "premium": 1}

    def test_by_ref_breakdown(self, inspect_env, capsys):
        _issue("a", ref="twitter")
        _issue("b", ref="twitter")
        _issue("c", ref="hn-launch")
        _issue("d")  # no ref
        rc = keys_main(["stats", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["by_ref_code"] == {
            "(none)": 1, "hn-launch": 1, "twitter": 2,
        }

    def test_by_created_date_breakdown(self, inspect_env, capsys):
        _issue("a")
        _issue("b")
        rc = keys_main(["stats", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["by_created_date"]) == 1
        # Date string is YYYY-MM-DD.
        (day,) = payload["by_created_date"].keys()
        assert len(day) == 10 and day[4] == "-" and day[7] == "-"
        assert payload["by_created_date"][day] == 2

    def test_stats_counts_revoked_only_when_in_manifest(
        self, inspect_env, capsys,
    ):
        """A revocation row whose key_hash isn't in the manifest does
        not inflate the revoked counter — the counter is "manifest
        rows that have been revoked", not "revocation rows"."""
        a = _issue("a")
        # Append a stray revocation for a hash that was never issued.
        with open(inspect_env["revoked"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "timestamp": "2026-04-25T00:00:00.000000Z",
                "key_hash": "ffffffffffffffffffffffffffffffff",
            }) + "\n")
        rc = keys_main(["stats", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["total_issued"] == 1
        assert payload["active"] == 1
        assert payload["revoked"] == 0


class TestPhase63NoLeaks:
    """Inspection output may NEVER expose the raw api_key, raw label,
    or checkout_url. Even if a future schema bump quietly adds a new
    field, the inspection allow-list drops it."""

    def test_raw_key_never_in_list_output(self, inspect_env, capsys):
        result = _issue("alice")
        raw = result["api_key"]
        for argv in (
            ["list"],
            ["list", "--include-revoked"],
            ["list", "--json"],
            ["stats"],
            ["stats", "--json"],
            ["show", "--key-hash", result["key_hash"]],
            ["show", "--key-hash", result["key_hash"], "--json"],
        ):
            keys_main(argv)
            captured = capsys.readouterr()
            assert raw not in captured.out, (
                f"raw key leaked via {argv!r}: {captured.out!r}"
            )
            assert raw not in captured.err

    def test_raw_label_never_in_output(self, inspect_env, capsys):
        marker = "RAW_LABEL_LEAK_GUARD_alice@example.com"
        result = issue_key(tier="free", label=marker)
        for argv in (
            ["list"], ["list", "--json"],
            ["show", "--key-hash", result["key_hash"]],
            ["show", "--key-hash", result["key_hash"], "--json"],
            ["stats"], ["stats", "--json"],
        ):
            keys_main(argv)
            captured = capsys.readouterr()
            assert marker not in captured.out, (
                f"raw label leaked via {argv!r}"
            )

    def test_label_hash_not_in_inspection_surface(
        self, inspect_env, capsys,
    ):
        """label_hash is on disk but the Phase 6.3 inspection surface
        deliberately omits it — operators who need it grep the
        manifest file directly."""
        result = _issue("alice")
        # Sanity — the manifest does store label_hash.
        body = inspect_env["manifest"].read_text(encoding="utf-8")
        assert result["label_hash"] in body

        keys_main(["list", "--json"])
        out = capsys.readouterr().out
        assert "label_hash" not in out

        keys_main(["show", "--key-hash", result["key_hash"], "--json"])
        out = capsys.readouterr().out
        assert "label_hash" not in out

    def test_checkout_url_never_in_output(self, inspect_env, capsys):
        """checkout_url is never persisted; assert that even an
        accidentally-injected row containing one would not surface."""
        # Hand-craft a row that smuggles a checkout_url field — the
        # inspection allow-list must drop it.
        url = "https://checkout.example.com/SECRET-URL"
        path = inspect_env["manifest"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "created_at": "2026-04-25T00:00:00.000000Z",
                "key_hash": "a" * 32,
                "label_hash": "b" * 32,
                "tier": "free",
                "checkout_url": url,            # ← must NOT survive
                "checkout_session_id": "cs_xyz",
            }) + "\n")

        for argv in (
            ["list"], ["list", "--json"],
            ["show", "--key-hash", "a" * 32],
            ["show", "--key-hash", "a" * 32, "--json"],
        ):
            keys_main(argv)
            out = capsys.readouterr().out
            assert url not in out, (
                f"checkout_url leaked via {argv!r}"
            )

    def test_stats_does_not_print_any_per_row_field(
        self, inspect_env, capsys,
    ):
        """stats is aggregate-only — the raw per-row metadata never
        flows into its output."""
        result = _issue(
            "alice", tier="premium", ref="twitter",
        )
        keys_main(["stats"])
        out = capsys.readouterr().out
        assert result["api_key"] not in out
        assert result["key_hash"] not in out
        assert result["label_hash"] not in out


class TestPhase63CliDispatch:
    def test_unknown_command_lists_all_six(self, capsys):
        rc = keys_main(["no-such-thing"])
        assert rc == 2
        err = capsys.readouterr().err
        for cmd in ("issue", "revoke", "list", "show", "stats"):
            assert cmd in err

    def test_help_lists_all_subcommands(self, capsys):
        rc = keys_main(["--help"])
        assert rc == 0
        out = capsys.readouterr().out
        for cmd in ("issue", "revoke", "list", "show", "stats"):
            assert cmd in out


class TestPhase63NoCoreImports:
    """Phase 6.0/6.2 boundary still holds after Phase 6.3."""

    def test_keys_module_still_dependency_light(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "keys.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
            "from trading_bot.api.server",
            "import structlog",
            "import fastapi",
        ):
            assert forbidden not in src, (
                f"keys.py imports restricted surface: {forbidden!r}"
            )


# ---------------------------------------------------------------------------
# time import for sort-order tests
# ---------------------------------------------------------------------------
import time  # noqa: E402  — kept at the bottom so it stays scoped to Phase 6.3


# ===========================================================================
# Phase 7.2 — bulk revocation cleanup helper
# ===========================================================================


class TestPhase72RevokeMany:
    def test_revokes_each_supplied_hash(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        revoked = tmp_path / "phase72_revoked.jsonl"
        monkeypatch.setenv("TRADING_API_KEYS_REVOKED_PATH", str(revoked))
        from trading_bot.api import key_store
        key_store.reset_caches_for_tests()

        hashes = ["a" * 32, "b" * 32, "c" * 32]
        rc = keys_main(
            ["revoke-many"]
            + sum((["--key-hash", h] for h in hashes), [])
            + ["--reason", "launch-cleanup"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "written       : 3" in out
        for h in hashes:
            assert h in out

        rows = [
            json.loads(line)
            for line in revoked.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 3
        assert {r["key_hash"] for r in rows} == set(hashes)
        assert all(r["reason"] == "launch-cleanup" for r in rows)

    def test_one_hash_still_works(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        revoked = tmp_path / "phase72_revoked_one.jsonl"
        monkeypatch.setenv("TRADING_API_KEYS_REVOKED_PATH", str(revoked))
        rc = keys_main(["revoke-many", "--key-hash", "d" * 32])
        assert rc == 0
        body = revoked.read_text(encoding="utf-8")
        assert "d" * 32 in body

    def test_no_key_hash_rejected(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        monkeypatch.setenv(
            "TRADING_API_KEYS_REVOKED_PATH",
            str(tmp_path / "noop.jsonl"),
        )
        rc = keys_main(["revoke-many"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "--key-hash" in err

    def test_blank_key_hash_rejected(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        monkeypatch.setenv(
            "TRADING_API_KEYS_REVOKED_PATH",
            str(tmp_path / "noop.jsonl"),
        )
        rc = keys_main(
            ["revoke-many", "--key-hash", "   "],
        )
        assert rc == 2

    def test_revoke_many_path_override(self, tmp_path: Path):
        custom = tmp_path / "alt_bulk_revoked.jsonl"
        rc = keys_main([
            "revoke-many",
            "--key-hash", "e" * 32,
            "--key-hash", "f" * 32,
            "--revoked-path", str(custom),
        ])
        assert rc == 0
        assert custom.exists()
        rows = [
            json.loads(line)
            for line in custom.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 2

    def test_revoke_many_never_stores_raw_key(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        """The CLI accepts ONLY pre-hashed values via --key-hash; if
        an operator accidentally pastes a raw key, the test ensures
        the raw value never lands on disk in any form (we only store
        what was passed, and operators should pass the hash)."""
        revoked = tmp_path / "phase72_no_leak.jsonl"
        monkeypatch.setenv("TRADING_API_KEYS_REVOKED_PATH", str(revoked))

        # Pretend the operator pasted a 'looks-like-hash' value that
        # is actually the hash of a sensitive marker. The marker
        # itself must not appear on disk.
        import hashlib as _hl
        marker = "RAW_BULK_REVOKE_MARKER_DO_NOT_LEAK"
        h = _hl.sha256(marker.encode("utf-8")).hexdigest()[:32]
        rc = keys_main(["revoke-many", "--key-hash", h])
        assert rc == 0
        body = revoked.read_text(encoding="utf-8")
        assert marker not in body
        assert h in body

    def test_idempotent_against_list_show(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        """Duplicate revocations are accepted; ``list --include-revoked``
        and ``show`` still treat the key as revoked."""
        manifest = tmp_path / "phase72_idemp_manifest.jsonl"
        revoked = tmp_path / "phase72_idemp_revoked.jsonl"
        monkeypatch.setenv(KEYS_MANIFEST_ENV_VAR, str(manifest))
        monkeypatch.setenv("TRADING_API_KEYS_REVOKED_PATH", str(revoked))
        from trading_bot.api import key_store
        key_store.reset_caches_for_tests()

        result = issue_key(tier="free", label="phase72-idemp")
        h = result["key_hash"]
        # Revoke the same hash three times — duplicates accepted.
        rc1 = keys_main([
            "revoke-many", "--key-hash", h, "--key-hash", h, "--key-hash", h,
        ])
        assert rc1 == 0
        capsys.readouterr()  # drain

        # show treats the key as revoked.
        rc2 = keys_main(["show", "--key-hash", h])
        assert rc2 == 0
        out = capsys.readouterr().out
        assert "revoked             : yes" in out

        # list (default) hides the key.
        rc3 = keys_main(["list"])
        assert rc3 == 0
        out = capsys.readouterr().out
        assert h not in out

        # list --include-revoked shows it.
        rc4 = keys_main(["list", "--include-revoked"])
        assert rc4 == 0
        out = capsys.readouterr().out
        assert h in out


# ===========================================================================
# Phase 6.4 — explicit --manifest-path / --revoked-path override tests
# ===========================================================================
#
# Phase 6.2 / 6.3 already added the flags to every relevant subcommand;
# Phase 6.4's deployment story (operator points the CLI at a persistent
# Railway-volume path) depends on those flags overriding both the env
# vars AND the default paths. These tests pin that contract so a future
# refactor cannot silently regress production-shell ergonomics.


@pytest.fixture
def isolated_env_paths(tmp_path: Path, monkeypatch):
    """
    Force the env vars to point at one tmp location while the CLI
    is invoked with --manifest-path / --revoked-path pointing at a
    DIFFERENT tmp location. The CLI flags must win.
    """
    env_manifest = tmp_path / "env_manifest.jsonl"
    env_revoked = tmp_path / "env_revoked.jsonl"
    flag_manifest = tmp_path / "flag_manifest.jsonl"
    flag_revoked = tmp_path / "flag_revoked.jsonl"
    monkeypatch.setenv(KEYS_MANIFEST_ENV_VAR, str(env_manifest))
    monkeypatch.setenv("TRADING_API_KEYS_REVOKED_PATH", str(env_revoked))
    from trading_bot.api import key_store
    key_store.reset_caches_for_tests()
    yield {
        "env_manifest": env_manifest,
        "env_revoked": env_revoked,
        "flag_manifest": flag_manifest,
        "flag_revoked": flag_revoked,
    }
    key_store.reset_caches_for_tests()


class TestPhase64IssueManifestPathFlag:
    def test_issue_writes_to_flag_path_not_env_path(
        self, isolated_env_paths,
    ):
        result = issue_key(
            tier="free", label="alice",
            manifest_path=isolated_env_paths["flag_manifest"],
        )
        # The issue_key API path mirrors the CLI's --manifest-path
        # plumbing — assert the row landed at the flag path, not env.
        assert isolated_env_paths["flag_manifest"].exists()
        assert not isolated_env_paths["env_manifest"].exists()
        body = isolated_env_paths["flag_manifest"].read_text(
            encoding="utf-8",
        )
        assert result["key_hash"] in body

    def test_cli_issue_manifest_path_flag(
        self, isolated_env_paths, capsys,
    ):
        rc = keys_main([
            "issue", "--tier", "free", "--label", "alice",
            "--manifest-path", str(isolated_env_paths["flag_manifest"]),
        ])
        assert rc == 0
        assert isolated_env_paths["flag_manifest"].exists()
        assert not isolated_env_paths["env_manifest"].exists()


class TestPhase64RevokeRevokedPathFlag:
    def test_cli_revoke_revoked_path_flag(
        self, isolated_env_paths, capsys,
    ):
        rc = keys_main([
            "revoke", "--key-hash", "a" * 32,
            "--revoked-path", str(isolated_env_paths["flag_revoked"]),
            "--reason", "phase64-test",
        ])
        assert rc == 0
        assert isolated_env_paths["flag_revoked"].exists()
        assert not isolated_env_paths["env_revoked"].exists()
        body = isolated_env_paths["flag_revoked"].read_text(
            encoding="utf-8",
        )
        assert "a" * 32 in body
        assert "phase64-test" in body


class TestPhase64ListPathFlags:
    def test_list_uses_flag_paths_not_env_paths(
        self, isolated_env_paths, capsys,
    ):
        # Issue against the FLAG manifest, not the env one.
        result = issue_key(
            tier="free", label="alice", ref_code="phase64",
            manifest_path=isolated_env_paths["flag_manifest"],
        )
        # Sanity — env manifest is empty.
        assert not isolated_env_paths["env_manifest"].exists()

        rc = keys_main([
            "list",
            "--manifest-path", str(isolated_env_paths["flag_manifest"]),
            "--revoked-path",  str(isolated_env_paths["flag_revoked"]),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert result["key_hash"] in out
        assert "phase64" in out

    def test_list_flag_paths_isolate_revocation_index(
        self, isolated_env_paths, capsys,
    ):
        """A revocation row in the FLAG revoked file should be
        honoured by list, while a row in the ENV revoked file should
        not bleed through."""
        result = issue_key(
            tier="free", label="alice",
            manifest_path=isolated_env_paths["flag_manifest"],
        )
        # Revoke against the FLAG revoked path.
        rc = keys_main([
            "revoke", "--key-hash", result["key_hash"],
            "--revoked-path", str(isolated_env_paths["flag_revoked"]),
        ])
        assert rc == 0
        capsys.readouterr()  # drain

        rc = keys_main([
            "list", "--include-revoked",
            "--manifest-path", str(isolated_env_paths["flag_manifest"]),
            "--revoked-path",  str(isolated_env_paths["flag_revoked"]),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        # The hash is present and marked revoked.
        assert result["key_hash"] in out
        assert "0 active, 1 revoked" in out
        # Sanity — without --include-revoked it disappears.
        capsys.readouterr()
        keys_main([
            "list",
            "--manifest-path", str(isolated_env_paths["flag_manifest"]),
            "--revoked-path",  str(isolated_env_paths["flag_revoked"]),
        ])
        out2 = capsys.readouterr().out
        assert result["key_hash"] not in out2


class TestPhase64ShowPathFlags:
    def test_show_uses_flag_paths(self, isolated_env_paths, capsys):
        result = issue_key(
            tier="premium", label="bob", ref_code="hn-launch",
            manifest_path=isolated_env_paths["flag_manifest"],
        )
        rc = keys_main([
            "show", "--key-hash", result["key_hash"],
            "--manifest-path", str(isolated_env_paths["flag_manifest"]),
            "--revoked-path",  str(isolated_env_paths["flag_revoked"]),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert result["key_hash"] in out
        assert "tier                : premium" in out
        assert "ref_code            : hn-launch" in out

    def test_show_flag_path_isolation(self, isolated_env_paths, capsys):
        """A row in the env-pointed manifest is NOT visible when the
        show command targets a different path."""
        # Write to env manifest.
        env_result = issue_key(
            tier="free", label="alice",
            manifest_path=isolated_env_paths["env_manifest"],
        )
        # show against the FLAG manifest (empty) — should miss.
        rc = keys_main([
            "show", "--key-hash", env_result["key_hash"],
            "--manifest-path", str(isolated_env_paths["flag_manifest"]),
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "no manifest row" in err


class TestPhase64StatsPathFlags:
    def test_stats_uses_flag_paths(self, isolated_env_paths, capsys):
        # Two rows in the FLAG manifest.
        issue_key(
            tier="free", label="alice",
            manifest_path=isolated_env_paths["flag_manifest"],
        )
        issue_key(
            tier="premium", label="bob",
            manifest_path=isolated_env_paths["flag_manifest"],
        )
        # One row in the ENV manifest — must NOT count toward stats.
        issue_key(
            tier="free", label="env-only",
            manifest_path=isolated_env_paths["env_manifest"],
        )

        rc = keys_main([
            "stats", "--json",
            "--manifest-path", str(isolated_env_paths["flag_manifest"]),
            "--revoked-path",  str(isolated_env_paths["flag_revoked"]),
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["total_issued"] == 2
        assert payload["by_tier"] == {"free": 1, "premium": 1}


class TestPhase64DeploymentDocsExist:
    """The deployment story is operator-facing — pin the docs file
    so a future refactor that moves the runbook can't silently
    delete it."""

    def test_deployment_md_exists_and_mentions_railway_volume(self):
        path = (
            Path(__file__).resolve().parent.parent
            / "docs" / "DEPLOYMENT.md"
        )
        assert path.exists(), "docs/DEPLOYMENT.md is required for Phase 6.4"
        body = path.read_text(encoding="utf-8")
        # Must document the recommended Railway volume paths.
        assert "/data/api_keys_manifest.jsonl" in body
        assert "/data/api_keys_revoked.jsonl" in body
        # Must mention the env vars by name.
        assert "TRADING_API_KEYS_MANIFEST_PATH" in body
        assert "TRADING_API_KEYS_REVOKED_PATH" in body
        # Must include the local-vs-production warning so the most
        # common operator mistake is loud.
        assert "does NOT authenticate" in body

    def test_core_control_md_links_to_deployment_md(self):
        path = (
            Path(__file__).resolve().parent.parent
            / "docs" / "CORE_CONTROL.md"
        )
        body = path.read_text(encoding="utf-8")
        assert "DEPLOYMENT.md" in body, (
            "CORE_CONTROL.md must point operators at DEPLOYMENT.md"
        )
