"""
FastAPI dashboard application.

Serves both the HTML dashboard and JSON API endpoints.
Designed to run in a background thread alongside the trading bot.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from trading_bot.dashboard.state import DashboardState

log = structlog.get_logger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"

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

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

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
                round(
                    (snap.equity - snap.starting_equity) / snap.starting_equity * 100,
                    2,
                )
                if snap.starting_equity > 0
                else 0.0
            ),
            "buying_power": snap.buying_power,
            "regime": snap.regime,
            "run_mode": snap.run_mode,
            "circuit_breaker": snap.circuit_breaker,
            "health": snap.health,
            "open_positions_count": len(snap.open_positions),
            "total_trades_today": len(snap.journal_entries),
            "last_updated": snap.last_updated,
            "bot_running": snap.bot_running,
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

    return app


def start_dashboard_server(
    state: DashboardState,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> threading.Thread:
    """Start the dashboard server in a background daemon thread."""
    import uvicorn

    app = create_app(state)

    def _run() -> None:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )

    thread = threading.Thread(target=_run, name="dashboard", daemon=True)
    thread.start()
    log.info("dashboard.started", host=host, port=port)
    return thread
