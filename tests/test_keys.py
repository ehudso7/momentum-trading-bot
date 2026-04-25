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
            "label_hash", "checkout_session_id", "checkout_url",
            "manifest_path",
        }
        assert result["tier"] == "free"
        assert result["checkout_session_id"] is None
        assert result["checkout_url"] is None
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
        }

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
