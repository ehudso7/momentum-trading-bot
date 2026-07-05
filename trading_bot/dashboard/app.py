"""
FastAPI dashboard application.

Serves the HTML dashboard, JSON API endpoints, and health probe endpoints.
Designed to run in a background thread alongside the trading bot.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from trading_bot.dashboard.state import DashboardState
from trading_bot.api.mobile_routes import router as mobile_router

log = structlog.get_logger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
_START_TIME = datetime.now(timezone.utc)

_dashboard_state: DashboardState | None = None


def create_app(state: DashboardState) -> FastAPI:
    """Create the FastAPI app wired to the given state container."""
    global _dashboard_state
    _dashboard_state = state

    app = FastAPI(
        title="Momentum Trading Bot",
        description="Live dashboard for the momentum day-trading bot",
        docs_url="/api/docs",
        redoc_url=None,
    )

    # --- CORS middleware ---
    allowed_origins = os.getenv(
        "DASHBOARD_CORS_ORIGINS", "http://localhost:3000,http://localhost:8080"
    ).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        max_age=600,
    )

    # Include mobile API routes
    app.include_router(mobile_router)

    # --- Global error handler ---
    @app.exception_handler(Exception)
    async def global_error_handler(request: Request, exc: Exception) -> JSONResponse:
        log.error("dashboard.unhandled_error", path=str(request.url.path), error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    # ------------------------------------------------------------------
    # Health probe endpoints (for Docker/Railway/K8s)
    # ------------------------------------------------------------------

    @app.get("/healthz")
    @app.get("/health")
    async def liveness() -> dict[str, Any]:
        """Liveness probe: is the process alive?

        Keep simple — no external dependency checks.
        Used by Docker HEALTHCHECK and orchestrator liveness probes.
        /health is an alias so PaaS healthchecks shared with the SaaS
        API service (railway.toml healthcheckPath) work unchanged.
        """
        return {
            "status": "alive",
            "uptime_seconds": round(
                (datetime.now(timezone.utc) - _START_TIME).total_seconds(), 1
            ),
        }

    @app.get("/readyz")
    async def readiness() -> JSONResponse:
        """Readiness probe: is the bot ready to operate?

        Checks that the bot has started and is pushing state updates.
        Returns 503 if not ready.
        """
        snap = state.get_snapshot()
        checks = {
            "bot_running": snap.bot_running,
            "has_updated": snap.last_updated is not None,
            "circuit_breaker_ok": snap.circuit_breaker.get("state") != "halted",
        }
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "checks": checks,
                "last_updated": snap.last_updated,
                "market_status": snap.market_status,
            },
        )

    # ------------------------------------------------------------------
    # HTML routes
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        """Main dashboard page."""
        try:
            snap = state.get_snapshot()
            return templates.TemplateResponse(
                request,
                "index.html",
                context={"snap": snap},
            )
        except Exception as e:
            log.error("dashboard.render_error", error=str(e), exc_info=True)
            return HTMLResponse(
                content=f"<pre>Dashboard error: {e}</pre>",
                status_code=500,
            )

    # ------------------------------------------------------------------
    # JSON API routes
    # ------------------------------------------------------------------

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        """Full bot status snapshot."""
        snap = state.get_snapshot()
        return {
            "equity": snap.equity,
            "starting_equity": snap.starting_equity,
            "daily_pnl": snap.daily_pnl,
            "daily_return_pct": (
                round(snap.daily_pnl / snap.starting_equity * 100, 2)
                if snap.starting_equity > 0
                else 0.0
            ),
            "buying_power": snap.buying_power,
            "regime": snap.regime,
            "run_mode": snap.run_mode,
            "circuit_breaker": snap.circuit_breaker,
            "health": snap.health,
            "market_status": snap.market_status,
            "market_status_detail": snap.market_status_detail,
            "open_positions_count": len(snap.open_positions),
            "total_trades_today": len(snap.journal_entries),
            "last_updated": snap.last_updated,
            "bot_running": snap.bot_running,
            "last_error": snap.last_error,
            "rejected_signals_count": snap.rejected_signals_count,
            "rejected_by_stage": snap.rejected_by_stage,
        }

    @app.get("/api/positions")
    async def api_positions() -> list[dict[str, Any]]:
        """Current open positions."""
        snap = state.get_snapshot()
        return snap.open_positions

    @app.get("/api/trades")
    async def api_trades() -> list[dict[str, Any]]:
        """Today's completed trades."""
        snap = state.get_snapshot()
        return snap.journal_entries

    @app.get("/api/equity-history")
    async def api_equity_history() -> list[dict[str, Any]]:
        """Equity curve data points for charting."""
        snap = state.get_snapshot()
        return snap.equity_history

    @app.get("/api/health")
    async def api_health() -> dict[str, Any]:
        """System health metrics."""
        snap = state.get_snapshot()
        return snap.health

    @app.get("/api/circuit-breaker")
    async def api_circuit_breaker() -> dict[str, Any]:
        """Circuit breaker status."""
        snap = state.get_snapshot()
        return snap.circuit_breaker

    @app.get("/api/rejections")
    async def api_rejections() -> dict[str, Any]:
        """Rejected signals summary and recent details."""
        snap = state.get_snapshot()
        return {
            "total": snap.rejected_signals_count,
            "by_stage": snap.rejected_by_stage,
            "recent": snap.rejected_signals,
        }

    @app.get("/api/rejections/summary")
    async def api_rejections_summary() -> dict[str, Any]:
        """Aggregate rejection reasons across all signals today."""
        snap = state.get_snapshot()

        # Group by stage, then count unique reasons within each stage
        stage_reasons: dict[str, dict[str, int]] = {}
        for r in snap.rejected_signals:
            stage = r.get("stage", "unknown")
            reason = r.get("reason", "unknown")
            if stage not in stage_reasons:
                stage_reasons[stage] = {}
            stage_reasons[stage][reason] = stage_reasons[stage].get(reason, 0) + 1

        # Also extract the top rejected symbols
        symbol_counts: dict[str, int] = {}
        for r in snap.rejected_signals:
            sym = r.get("symbol", "?")
            symbol_counts[sym] = symbol_counts.get(sym, 0) + 1

        top_symbols = sorted(symbol_counts.items(), key=lambda x: -x[1])[:15]

        return {
            "total": snap.rejected_signals_count,
            "by_stage": snap.rejected_by_stage,
            "stage_reasons": stage_reasons,
            "top_rejected_symbols": [
                {"symbol": s, "count": c} for s, c in top_symbols
            ],
        }

    return app


def _resolve_dashboard_port(requested: int) -> int:
    """
    Defence-in-depth port resolver. ``trading_bot.main`` already
    pipes ``$PORT`` through the argparse default — but a direct
    caller of ``start_dashboard_server`` (tests, scripts) may pass
    the legacy 8080 default explicitly. If ``$PORT`` is set AND
    the caller didn't override the legacy default, prefer ``$PORT``.

    A malformed ``$PORT`` falls back to the requested value (rather
    than raising) so a typo in env config never bricks startup.
    """
    raw = os.environ.get("PORT")
    if raw and requested == 8080:
        try:
            port = int(str(raw).strip())
            if 1 <= port <= 65535:
                return port
        except (TypeError, ValueError):
            pass
    return requested


def start_dashboard_server(
    state: DashboardState,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> threading.Thread:
    """Start the dashboard server in a background daemon thread."""
    import uvicorn

    resolved_port = _resolve_dashboard_port(port)

    app = create_app(state)

    def _run() -> None:
        uvicorn.run(
            app,
            host=host,
            port=resolved_port,
            log_level="warning",
            access_log=False,
        )

    thread = threading.Thread(target=_run, name="dashboard", daemon=True)
    thread.start()
    log.info("dashboard.started", host=host, port=resolved_port)
    return thread
