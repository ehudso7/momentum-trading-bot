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
# Phase 6.2 — manifest inspection (``keys list``)
# ===========================================================================


from trading_bot.api.keys import (  # noqa: E402
    LIST_OUTPUT_FIELDS,
    _format_list_json,
    _format_list_text,
    list_keys,
)


def _seed_manifest(
    path: Path,
    rows: list[dict],
) -> None:
    """Write a manifest file directly so we control the exact rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _row(
    *,
    created_at: str,
    key_hash: str,
    label_hash: str = "0" * 32,
    tier: str = "free",
    ref_code=None,
    checkout_session_id=None,
    **extra: object,
) -> dict:
    """Build a manifest-style row, with any extra fields the test
    wants to plant for leak-guard purposes."""
    base = {
        "created_at": created_at,
        "key_hash": key_hash,
        "label_hash": label_hash,
        "tier": tier,
        "ref_code": ref_code,
        "checkout_session_id": checkout_session_id,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# list_keys — plain reads + filters + sort
# ---------------------------------------------------------------------------


class TestPhase62ListKeys:
    def test_missing_manifest_returns_empty(self, tmp_path: Path):
        # Manifest path explicitly set to a file that does NOT exist.
        out = list_keys(manifest_path=tmp_path / "absent.jsonl")
        assert out == []

    def test_empty_manifest_returns_empty(self, manifest_path: Path):
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("", encoding="utf-8")
        assert list_keys(manifest_path=manifest_path) == []

    def test_lists_all_rows(self, manifest_path: Path):
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-20T10:00:00.000000Z",
                 key_hash="aaaa", tier="free"),
            _row(created_at="2026-04-21T10:00:00.000000Z",
                 key_hash="bbbb", tier="premium"),
            _row(created_at="2026-04-22T10:00:00.000000Z",
                 key_hash="cccc", tier="free"),
        ])
        out = list_keys(manifest_path=manifest_path)
        assert len(out) == 3

    def test_sorted_newest_first(self, manifest_path: Path):
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-20T10:00:00.000000Z",
                 key_hash="oldest"),
            _row(created_at="2026-04-22T10:00:00.000000Z",
                 key_hash="newest"),
            _row(created_at="2026-04-21T10:00:00.000000Z",
                 key_hash="middle"),
        ])
        out = list_keys(manifest_path=manifest_path)
        assert [r["key_hash"] for r in out] == [
            "newest", "middle", "oldest",
        ]

    def test_filter_by_tier(self, manifest_path: Path):
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-20T00:00:00.000000Z",
                 key_hash="f1", tier="free"),
            _row(created_at="2026-04-21T00:00:00.000000Z",
                 key_hash="p1", tier="premium"),
            _row(created_at="2026-04-22T00:00:00.000000Z",
                 key_hash="f2", tier="free"),
        ])
        free = list_keys(
            manifest_path=manifest_path, tier_filter="free",
        )
        assert {r["key_hash"] for r in free} == {"f1", "f2"}
        prem = list_keys(
            manifest_path=manifest_path, tier_filter="premium",
        )
        assert {r["key_hash"] for r in prem} == {"p1"}

    def test_filter_by_tier_case_insensitive(
        self, manifest_path: Path,
    ):
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-20T00:00:00.000000Z",
                 key_hash="f1", tier="free"),
        ])
        # "FREE" / " Free " all normalise to "free".
        out = list_keys(
            manifest_path=manifest_path, tier_filter="FREE",
        )
        assert {r["key_hash"] for r in out} == {"f1"}

    def test_filter_by_ref(self, manifest_path: Path):
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-20T00:00:00.000000Z",
                 key_hash="t1", ref_code="twitter-launch_2026"),
            _row(created_at="2026-04-21T00:00:00.000000Z",
                 key_hash="h1", ref_code="hn-launch"),
            _row(created_at="2026-04-22T00:00:00.000000Z",
                 key_hash="n1", ref_code=None),
        ])
        out = list_keys(
            manifest_path=manifest_path,
            ref_filter="twitter-launch_2026",
        )
        assert {r["key_hash"] for r in out} == {"t1"}

    def test_filter_by_ref_runs_through_sanitiser(
        self, manifest_path: Path,
    ):
        """An operator typing ``--ref "twitter launch"`` (with a
        space) sanitises to ``twitterlaunch`` — no row matches in
        this case, but an operator pasting an XSS payload sanitises
        to its safe substring and matches the row that was
        sanitised at write time."""
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-20T00:00:00.000000Z",
                 key_hash="x1", ref_code="scriptxssscript"),
        ])
        out = list_keys(
            manifest_path=manifest_path,
            ref_filter="<script>xss</script>",
        )
        assert {r["key_hash"] for r in out} == {"x1"}

    def test_empty_ref_filter_after_sanitise_is_treated_as_no_filter(
        self, manifest_path: Path,
    ):
        """``--ref "!@#"`` sanitises to "" and we treat that as
        "no filter" — better than silently filtering to zero rows."""
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-20T00:00:00.000000Z",
                 key_hash="a", ref_code=None),
            _row(created_at="2026-04-21T00:00:00.000000Z",
                 key_hash="b", ref_code="hn-launch"),
        ])
        out = list_keys(manifest_path=manifest_path, ref_filter="!@#")
        assert {r["key_hash"] for r in out} == {"a", "b"}

    def test_filters_are_anded(self, manifest_path: Path):
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-20T00:00:00.000000Z",
                 key_hash="ft", tier="free", ref_code="twitter"),
            _row(created_at="2026-04-21T00:00:00.000000Z",
                 key_hash="pt", tier="premium", ref_code="twitter"),
            _row(created_at="2026-04-22T00:00:00.000000Z",
                 key_hash="fh", tier="free", ref_code="hn"),
        ])
        out = list_keys(
            manifest_path=manifest_path,
            tier_filter="free",
            ref_filter="twitter",
        )
        assert {r["key_hash"] for r in out} == {"ft"}

    def test_malformed_lines_are_skipped(self, manifest_path: Path):
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            "\n".join([
                "garbage",
                "",
                "[\"not a dict\"]",
                json.dumps(_row(
                    created_at="2026-04-20T00:00:00.000000Z",
                    key_hash="ok",
                )),
            ])
            + "\n",
            encoding="utf-8",
        )
        out = list_keys(manifest_path=manifest_path)
        assert {r["key_hash"] for r in out} == {"ok"}


# ---------------------------------------------------------------------------
# list_keys — output projection (defense in depth)
# ---------------------------------------------------------------------------


class TestPhase62OutputProjection:
    def test_only_documented_fields_emitted(self, manifest_path: Path):
        """Even when the row carries extra fields (a poisoned /
        hand-edited manifest), the output dict must contain ONLY
        the public field-set."""
        _seed_manifest(manifest_path, [
            _row(
                created_at="2026-04-20T00:00:00.000000Z",
                key_hash="ok",
                # Extra fields a Phase 6.0/6.1 row should NOT have.
                api_key="HOSTILE_RAW_KEY_DO_NOT_LEAK",
                label="HOSTILE_RAW_LABEL_DO_NOT_LEAK",
                checkout_url="https://stripe.example/leak",
                random_extra="HOSTILE_EXTRA_DO_NOT_LEAK",
            ),
        ])
        (out,) = list_keys(manifest_path=manifest_path)
        assert set(out.keys()) == set(LIST_OUTPUT_FIELDS)

    def test_text_output_never_contains_secret_markers(
        self, manifest_path: Path,
    ):
        _seed_manifest(manifest_path, [
            _row(
                created_at="2026-04-20T00:00:00.000000Z",
                key_hash="ok",
                api_key="HOSTILE_RAW_KEY_DO_NOT_LEAK",
                label="HOSTILE_RAW_LABEL_DO_NOT_LEAK",
                checkout_url="https://stripe.example/leak",
            ),
        ])
        rendered = _format_list_text(
            list_keys(manifest_path=manifest_path),
        )
        assert "HOSTILE_RAW_KEY_DO_NOT_LEAK" not in rendered
        assert "HOSTILE_RAW_LABEL_DO_NOT_LEAK" not in rendered
        assert "stripe.example/leak" not in rendered

    def test_json_output_never_contains_secret_markers(
        self, manifest_path: Path,
    ):
        _seed_manifest(manifest_path, [
            _row(
                created_at="2026-04-20T00:00:00.000000Z",
                key_hash="ok",
                api_key="HOSTILE_RAW_KEY_DO_NOT_LEAK",
                label="HOSTILE_RAW_LABEL_DO_NOT_LEAK",
                checkout_url="https://stripe.example/leak",
            ),
        ])
        rendered = _format_list_json(
            list_keys(manifest_path=manifest_path),
        )
        # Re-parse + spot-check.
        parsed = json.loads(rendered)
        for row in parsed:
            assert set(row.keys()) == set(LIST_OUTPUT_FIELDS)
        assert "HOSTILE_RAW_KEY_DO_NOT_LEAK" not in rendered
        assert "HOSTILE_RAW_LABEL_DO_NOT_LEAK" not in rendered
        assert "stripe.example/leak" not in rendered

    def test_label_hash_is_not_emitted(self, manifest_path: Path):
        """``label_hash`` IS in the manifest (Phase 6.0) but it's
        not part of the public list-view contract — operators can
        join elsewhere if they really need it. Keep the public
        surface tight."""
        _seed_manifest(manifest_path, [
            _row(
                created_at="2026-04-20T00:00:00.000000Z",
                key_hash="ok",
                label_hash="ABCDEF1234567890",
            ),
        ])
        (out,) = list_keys(manifest_path=manifest_path)
        assert "label_hash" not in out


# ---------------------------------------------------------------------------
# CLI — `keys list`
# ---------------------------------------------------------------------------


class TestPhase62Cli:
    def _env(self, manifest_path: Path) -> dict:
        return {
            **__import__("os").environ,
            KEYS_MANIFEST_ENV_VAR: str(manifest_path),
        }

    def test_cli_list_all_text(self, manifest_path: Path):
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-20T00:00:00.000000Z",
                 key_hash="aaaa1111", tier="free",
                 ref_code="twitter-launch_2026"),
            _row(created_at="2026-04-21T00:00:00.000000Z",
                 key_hash="bbbb2222", tier="premium"),
        ])
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "list",
            ],
            capture_output=True, text=True,
            env=self._env(manifest_path),
        )
        assert result.returncode == 0, result.stderr
        # Newest first.
        out = result.stdout
        assert out.find("bbbb2222") < out.find("aaaa1111")
        assert "twitter-launch_2026" in out
        assert "premium" in out

    def test_cli_filter_by_tier(self, manifest_path: Path):
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-20T00:00:00.000000Z",
                 key_hash="freekey", tier="free"),
            _row(created_at="2026-04-21T00:00:00.000000Z",
                 key_hash="premkey", tier="premium"),
        ])
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "list", "--tier", "premium",
            ],
            capture_output=True, text=True,
            env=self._env(manifest_path),
        )
        assert result.returncode == 0, result.stderr
        assert "premkey" in result.stdout
        assert "freekey" not in result.stdout

    def test_cli_filter_by_ref(self, manifest_path: Path):
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-20T00:00:00.000000Z",
                 key_hash="thash", ref_code="twitter-launch_2026"),
            _row(created_at="2026-04-21T00:00:00.000000Z",
                 key_hash="hhash", ref_code="hn-launch"),
        ])
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "list", "--ref", "hn-launch",
            ],
            capture_output=True, text=True,
            env=self._env(manifest_path),
        )
        assert result.returncode == 0, result.stderr
        assert "hhash" in result.stdout
        assert "thash" not in result.stdout

    def test_cli_combined_filters_anded(self, manifest_path: Path):
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-20T00:00:00.000000Z",
                 key_hash="ft", tier="free", ref_code="twitter"),
            _row(created_at="2026-04-21T00:00:00.000000Z",
                 key_hash="pt", tier="premium", ref_code="twitter"),
            _row(created_at="2026-04-22T00:00:00.000000Z",
                 key_hash="fh", tier="free", ref_code="hn"),
        ])
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "list", "--tier", "free", "--ref", "twitter",
            ],
            capture_output=True, text=True,
            env=self._env(manifest_path),
        )
        assert result.returncode == 0, result.stderr
        assert "ft" in result.stdout
        assert "pt" not in result.stdout
        assert "fh" not in result.stdout

    def test_cli_json_flag_emits_json_array(
        self, manifest_path: Path,
    ):
        _seed_manifest(manifest_path, [
            _row(created_at="2026-04-21T00:00:00.000000Z",
                 key_hash="abcd1234", tier="premium",
                 ref_code="hn-launch",
                 checkout_session_id="cs_test_abc"),
        ])
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "list", "--json",
            ],
            capture_output=True, text=True,
            env=self._env(manifest_path),
        )
        assert result.returncode == 0, result.stderr
        rows = json.loads(result.stdout)
        assert len(rows) == 1
        row = rows[0]
        assert set(row.keys()) == set(LIST_OUTPUT_FIELDS)
        assert row["key_hash"] == "abcd1234"
        assert row["tier"] == "premium"
        assert row["ref_code"] == "hn-launch"
        assert row["checkout_session_id"] == "cs_test_abc"

    def test_cli_missing_manifest_text_fallback(
        self, manifest_path: Path,
    ):
        # Don't write the file at all.
        assert not manifest_path.exists()
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "list",
            ],
            capture_output=True, text=True,
            env=self._env(manifest_path),
        )
        assert result.returncode == 0, result.stderr
        assert "(no records)" in result.stdout

    def test_cli_missing_manifest_json_emits_empty_array(
        self, manifest_path: Path,
    ):
        assert not manifest_path.exists()
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "list", "--json",
            ],
            capture_output=True, text=True,
            env=self._env(manifest_path),
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == []

    def test_cli_invalid_tier_rejected(self, manifest_path: Path):
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.api.keys",
                "list", "--tier", "enterprise",
            ],
            capture_output=True, text=True,
            env=self._env(manifest_path),
        )
        assert result.returncode == 2

    def test_cli_unknown_command_lists_both(self, capsys):
        from trading_bot.api.keys import main as keys_main
        rc = keys_main(["bogus"])
        assert rc == 2
        err = capsys.readouterr().err
        # Phase 6.2: the error message should mention BOTH commands.
        assert "issue" in err
        assert "list" in err

    def test_cli_list_never_outputs_secret_markers(
        self, manifest_path: Path,
    ):
        """End-to-end: a poisoned manifest with stray secret-looking
        fields must not leak through the CLI."""
        _seed_manifest(manifest_path, [
            _row(
                created_at="2026-04-21T00:00:00.000000Z",
                key_hash="ok",
                api_key="HOSTILE_RAW_KEY_DO_NOT_LEAK",
                label="HOSTILE_RAW_LABEL_DO_NOT_LEAK",
                checkout_url="https://stripe.example/HOSTILE_LEAK",
            ),
        ])
        # Both text and JSON CLI paths.
        for json_flag in ([], ["--json"]):
            result = subprocess.run(
                [
                    sys.executable, "-m", "trading_bot.api.keys",
                    "list", *json_flag,
                ],
                capture_output=True, text=True,
                env=self._env(manifest_path),
            )
            assert result.returncode == 0, result.stderr
            for marker in (
                "HOSTILE_RAW_KEY_DO_NOT_LEAK",
                "HOSTILE_RAW_LABEL_DO_NOT_LEAK",
                "HOSTILE_LEAK",
                "checkout_url",
            ):
                assert marker not in result.stdout, (
                    f"CLI {json_flag} leaked: {marker!r}"
                )
