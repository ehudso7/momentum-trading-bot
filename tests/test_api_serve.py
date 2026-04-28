"""
Tests for ``trading_bot.api.serve`` — the first-class SaaS API
deploy entry point added after the Railway healthcheck post-mortem.

Coverage:
  * pure resolvers honour CLI > env > default precedence
  * fail-soft behaviour for every malformed env-var value
  * --dry-run prints the bind address and exits 0 without
    importing uvicorn (so a missing uvicorn dependency in CI
    doesn't break smoke tests)
  * end-to-end: starting the entry point binds a real port and
    serves /health 200
  * the entry point imports nothing from Core (SaaS boundary)
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from trading_bot.api import serve


# ---------------------------------------------------------------------------
# Pure resolvers
# ---------------------------------------------------------------------------


class TestResolvePort:
    def test_explicit_cli_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("PORT", "12345")
        assert serve._resolve_port(9000) == 9000

    def test_env_used_when_no_cli(self, monkeypatch):
        monkeypatch.setenv("PORT", "12345")
        assert serve._resolve_port(None) == 12345

    def test_default_when_neither(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        assert serve._resolve_port(None) == 8080

    @pytest.mark.parametrize(
        "bad_env",
        ["", "   ", "abc", "0", "-1", "65536", "70000", "1.5"],
    )
    def test_invalid_env_falls_back_to_default(self, monkeypatch, bad_env):
        monkeypatch.setenv("PORT", bad_env)
        assert serve._resolve_port(None) == 8080

    @pytest.mark.parametrize("bad_cli", [0, -5, 65536, 1_000_000])
    def test_invalid_cli_falls_back_to_default(self, monkeypatch, bad_cli):
        """Out-of-range CLI value is fail-soft, not a hard error.
        An operator typo in startCommand must NOT brick the deploy."""
        monkeypatch.delenv("PORT", raising=False)
        assert serve._resolve_port(bad_cli) == 8080

    def test_whitespace_around_env_stripped(self, monkeypatch):
        monkeypatch.setenv("PORT", "   9090  ")
        assert serve._resolve_port(None) == 9090


class TestResolveHost:
    def test_default_is_zero_zero_zero_zero(self, monkeypatch):
        """PaaS must bind 0.0.0.0 — a 127.0.0.1 default would
        block the platform's healthcheck from reaching us."""
        monkeypatch.delenv("HOST", raising=False)
        assert serve._resolve_host(None) == "0.0.0.0"

    def test_env_used_when_no_cli(self, monkeypatch):
        monkeypatch.setenv("HOST", "127.0.0.1")
        assert serve._resolve_host(None) == "127.0.0.1"

    def test_explicit_cli_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("HOST", "127.0.0.1")
        assert serve._resolve_host("0.0.0.0") == "0.0.0.0"

    def test_blank_env_falls_through(self, monkeypatch):
        monkeypatch.setenv("HOST", "   ")
        assert serve._resolve_host(None) == "0.0.0.0"


class TestResolveLogLevel:
    @pytest.mark.parametrize(
        "level",
        ["critical", "error", "warning", "info", "debug", "trace"],
    )
    def test_valid_levels_kept(self, level):
        assert serve._resolve_log_level(level) == level

    def test_case_insensitive(self):
        assert serve._resolve_log_level("INFO") == "info"

    def test_unknown_falls_back_to_info(self):
        assert serve._resolve_log_level("verbose") == "info"

    def test_none_falls_back_to_info(self):
        assert serve._resolve_log_level(None) == "info"


# ---------------------------------------------------------------------------
# argparse + dry-run
# ---------------------------------------------------------------------------


class TestArgparse:
    def test_dry_run_exits_zero_and_prints_bind(self, capsys):
        rc = serve.main(["--dry-run"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "binding" in err
        assert ":8080" in err

    def test_dry_run_with_port_env(self, monkeypatch, capsys):
        monkeypatch.setenv("PORT", "9876")
        rc = serve.main(["--dry-run"])
        assert rc == 0
        assert ":9876" in capsys.readouterr().err

    def test_dry_run_with_explicit_port(self, capsys):
        rc = serve.main(["--port", "5500", "--dry-run"])
        assert rc == 0
        assert ":5500" in capsys.readouterr().err

    def test_dry_run_with_explicit_host(self, capsys):
        rc = serve.main(["--host", "127.0.0.1", "--dry-run"])
        assert rc == 0
        assert "127.0.0.1:" in capsys.readouterr().err

    def test_dry_run_does_not_import_uvicorn(self, monkeypatch):
        """--dry-run must work even in environments where uvicorn
        is not installed. We force the import to raise to prove
        we never reach it on the dry-run path."""
        sentinel = object()

        class Block:
            def find_spec(self, name, path=None, target=None):
                if name == "uvicorn":
                    raise ImportError("blocked for test")
                return None

        block = Block()
        monkeypatch.setattr(sys, "meta_path", [block, *sys.meta_path])
        # If --dry-run touches uvicorn it'll raise here.
        rc = serve.main(["--dry-run"])
        assert rc == 0
        assert sentinel is sentinel  # placeholder

    def test_invalid_log_level_rejected_by_argparse(self, capsys):
        with pytest.raises(SystemExit) as exc:
            serve.main(["--log-level", "verbose", "--dry-run"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# End-to-end: actual uvicorn launch
# ---------------------------------------------------------------------------


class TestEndToEndBoot:
    """Spawn the entry point as a subprocess (uvicorn-clean), poll
    /health, assert 200. This is the closest reproducible analogue
    to the Railway boot path."""

    def _free_port(self) -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_health_returns_200_within_5s(self):
        port = self._free_port()
        env = {**os.environ, "PORT": str(port)}
        proc = subprocess.Popen(
            [sys.executable, "-m", "trading_bot.api.serve",
             "--host", "127.0.0.1"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 5.0
            last_err: object = None
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=0.5,
                    ) as resp:
                        body = resp.read()
                        assert resp.status == 200
                        # Documented public shape.
                        assert b'"status":"ok"' in body or b'"status": "ok"' in body
                        return  # success
                except Exception as exc:
                    last_err = exc
                    time.sleep(0.1)
            raise AssertionError(
                f"/health did not return 200 within 5s: {last_err!r}"
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------
# Boundary: serve.py must not import Core
# ---------------------------------------------------------------------------


class TestNoCoreImports:
    def test_module_does_not_import_core(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "serve.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
            "from trading_bot.dashboard",
        ):
            assert forbidden not in src, (
                f"serve.py imports restricted surface: {forbidden!r}"
            )

    def test_imports_only_api_server(self):
        """The only ``trading_bot.*`` module the entry point pulls
        in is ``trading_bot.api.server`` (lazy, inside main when
        not --reload). Verify no other surface is reached."""
        # Re-read as text: lazy imports inside ``main`` are fine,
        # we just want the module import to be inert.
        import importlib
        sys.modules.pop("trading_bot.api.serve", None)
        # Importing the module must not pull in server (which has
        # heavy middleware setup).
        importlib.import_module("trading_bot.api.serve")
        # ``trading_bot.api.server`` may already be loaded by other
        # tests; we don't assert it's NOT loaded — only that
        # serve.py's own source doesn't import it at top level.
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "serve.py"
        ).read_text()
        # Top-level lines (no leading whitespace) that import from
        # trading_bot.api.server are forbidden — only lazy imports
        # inside main() are allowed.
        for line in src.splitlines():
            stripped = line.lstrip()
            if not stripped.startswith("from trading_bot.api.server"):
                continue
            # If there's leading whitespace, it's an indented
            # (lazy) import — ok.
            if line != stripped:
                continue
            # Otherwise it's a top-level import — forbidden.
            raise AssertionError(
                f"top-level import of trading_bot.api.server: {line!r}"
            )
