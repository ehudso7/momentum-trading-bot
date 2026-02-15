"""
Portfolio manager: position tracking, scale-outs, trailing stops, and trade journal.

Orchestrates the lifecycle of each position from open to close,
including partial exits at R:R targets and trailing stop management.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from trading_bot.config.settings import AppConfig
from trading_bot.data.market_data import MarketDataProvider
from trading_bot.execution.broker_base import BrokerBase
from trading_bot.models.domain import (
    JournalEntry,
    OrderSide,
    PositionInfo,
    PositionStatus,
    RiskCheckResult,
    TradeSignal,
)
from trading_bot.risk.circuit_breaker import CircuitBreaker
from trading_bot.strategies.base import Strategy
from trading_bot.utils.helpers import format_currency, now_et

log = structlog.get_logger(__name__)

JOURNAL_HEADERS = [
    "date",
    "symbol",
    "side",
    "signal_type",
    "entry_price",
    "exit_price",
    "shares",
    "pnl",
    "rr_ratio",
    "hold_time_minutes",
    "entry_time",
    "exit_time",
    "exit_reason",
    "notes",
]


class PortfolioManager:
    """Tracks open positions, manages scale-outs, trailing stops, and P&L journal."""

    def __init__(
        self,
        broker: BrokerBase,
        config: AppConfig,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ):
        self._broker = broker
        self._config = config
        self._circuit = circuit_breaker
        self._positions: dict[str, PositionInfo] = {}
        self._daily_pnl: float = 0.0
        self._journal_path = Path(config.journal_csv_path)
        self._ensure_journal_file()

    def open_position(
        self, signal: TradeSignal, risk_result: RiskCheckResult
    ) -> PositionInfo:
        """
        Open a new position based on a validated trade signal.

        Submits market order via broker and creates PositionInfo tracking.
        """
        # Submit entry order
        order_id = self._broker.submit_market_order(
            symbol=signal.symbol,
            qty=risk_result.shares,
            side=OrderSide.BUY,
        )

        # Create position tracking
        position = PositionInfo(
            symbol=signal.symbol,
            side=OrderSide.BUY,
            entry_price=signal.entry_price,
            current_price=signal.entry_price,
            shares=risk_result.shares,
            shares_remaining=risk_result.shares,
            stop_price=signal.stop_price,
            target_prices=signal.target_prices,
            status=PositionStatus.OPEN,
            pnl_unrealized=0.0,
            pnl_realized=0.0,
            scale_outs_completed=0,
            entry_time=now_et(),
            signal_type=signal.signal_type,
            broker_order_ids=[order_id],
        )

        self._positions[signal.symbol] = position

        log.info(
            "portfolio.opened",
            symbol=signal.symbol,
            shares=risk_result.shares,
            entry=signal.entry_price,
            stop=signal.stop_price,
            risk=format_currency(risk_result.risk_dollars),
        )

        return position

    def update_positions(
        self, strategy: Strategy, market_data: MarketDataProvider
    ) -> list[JournalEntry]:
        """
        Update all open positions. Called every tick.

        For each position:
        1. Fetch latest price
        2. Check for exits (stop, time, PSAR)
        3. Check for scale-outs at R:R targets
        4. Update trailing stop
        5. Update unrealized P&L

        Returns list of journal entries for any closed trades.
        """
        journal_entries = []
        symbols_to_remove = []

        for symbol, position in self._positions.items():
            if position.status == PositionStatus.CLOSED:
                symbols_to_remove.append(symbol)
                continue

            # 1. Update current price
            price = market_data.get_current_price(symbol)
            if price is None:
                log.warning("portfolio.price_unavailable", symbol=symbol)
                continue

            position.current_price = price
            position.pnl_unrealized = (
                position.shares_remaining * (price - position.entry_price)
            )

            # 2. Get bar data for strategy decisions
            bars = market_data.get_intraday_bars(symbol, lookback_bars=50)

            # 3. Check full exit conditions
            should_exit, exit_reason = strategy.should_exit(position, bars)
            if should_exit:
                entry = self._close_position(position, price, exit_reason)
                if entry:
                    journal_entries.append(entry)
                symbols_to_remove.append(symbol)
                continue

            # 4. Check scale-outs
            scale_result = strategy.compute_scale_out(position, price)
            if scale_result:
                shares_to_sell, scale_reason = scale_result
                self._execute_scale_out(position, shares_to_sell, price, scale_reason)

            # 5. Update trailing stop
            new_stop = strategy.get_trailing_stop(position, bars)
            if new_stop and (
                not position.trailing_stop_price
                or new_stop > position.trailing_stop_price
            ):
                position.trailing_stop_active = True
                position.trailing_stop_price = new_stop
                log.debug(
                    "portfolio.trailing_stop_updated",
                    symbol=symbol,
                    new_stop=round(new_stop, 4),
                )

        # Clean up closed positions
        for symbol in symbols_to_remove:
            if symbol in self._positions:
                del self._positions[symbol]

        return journal_entries

    def close_position(self, symbol: str, reason: str) -> Optional[JournalEntry]:
        """Close an entire position by symbol."""
        if symbol not in self._positions:
            return None

        position = self._positions[symbol]
        entry = self._close_position(position, position.current_price, reason)
        del self._positions[symbol]
        return entry

    def close_all(self, reason: str = "end_of_day") -> list[JournalEntry]:
        """Close all open positions. Called at hard time exit or shutdown."""
        entries = []
        symbols = list(self._positions.keys())

        for symbol in symbols:
            entry = self.close_position(symbol, reason)
            if entry:
                entries.append(entry)

        if entries:
            log.info(
                "portfolio.all_closed",
                reason=reason,
                count=len(entries),
                total_pnl=format_currency(sum(e.pnl for e in entries)),
            )

        return entries

    def get_open_positions(self) -> list[PositionInfo]:
        return list(self._positions.values())

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def reset_daily(self) -> None:
        """Reset daily P&L tracking."""
        self._daily_pnl = 0.0

    # --- Private methods ---

    def _close_position(
        self, position: PositionInfo, exit_price: float, reason: str
    ) -> Optional[JournalEntry]:
        """Execute full close and create journal entry."""
        if position.shares_remaining <= 0:
            return None

        # Submit sell order for remaining shares
        order_id = self._broker.submit_market_order(
            symbol=position.symbol,
            qty=position.shares_remaining,
            side=OrderSide.SELL,
        )
        position.broker_order_ids.append(order_id)

        # Calculate final P&L
        close_pnl = position.shares_remaining * (exit_price - position.entry_price)
        total_pnl = position.pnl_realized + close_pnl
        self._daily_pnl += total_pnl

        # Record in circuit breaker
        if self._circuit:
            self._circuit.record_trade_result(total_pnl)

        # Calculate R multiple
        risk_per_share = abs(position.entry_price - position.stop_price)
        rr_ratio = (
            (exit_price - position.entry_price) / risk_per_share
            if risk_per_share > 0
            else 0.0
        )

        # Hold time
        hold_minutes = (now_et() - position.entry_time).total_seconds() / 60

        position.status = PositionStatus.CLOSED
        position.shares_remaining = 0

        entry = JournalEntry(
            date=now_et().strftime("%Y-%m-%d"),
            symbol=position.symbol,
            side=position.side.value,
            signal_type=position.signal_type.value,
            entry_price=position.entry_price,
            exit_price=exit_price,
            shares=position.shares,
            pnl=round(total_pnl, 2),
            rr_ratio=round(rr_ratio, 2),
            hold_time_minutes=round(hold_minutes, 1),
            entry_time=position.entry_time.strftime("%H:%M:%S"),
            exit_time=now_et().strftime("%H:%M:%S"),
            exit_reason=reason,
        )

        self._log_to_journal(entry)

        log.info(
            "portfolio.closed",
            symbol=position.symbol,
            pnl=format_currency(total_pnl),
            rr=round(rr_ratio, 2),
            reason=reason,
            hold_min=round(hold_minutes, 1),
        )

        return entry

    def _execute_scale_out(
        self, position: PositionInfo, shares: int, price: float, reason: str
    ) -> None:
        """Execute a partial scale-out."""
        order_id = self._broker.submit_market_order(
            symbol=position.symbol,
            qty=shares,
            side=OrderSide.SELL,
        )
        position.broker_order_ids.append(order_id)

        # Track realized P&L from scale-out
        scale_pnl = shares * (price - position.entry_price)
        position.pnl_realized += scale_pnl
        position.shares_remaining -= shares
        position.scale_outs_completed += 1

        if position.shares_remaining > 0:
            position.status = PositionStatus.PARTIALLY_CLOSED
        else:
            position.status = PositionStatus.CLOSED

        log.info(
            "portfolio.scale_out",
            symbol=position.symbol,
            shares_sold=shares,
            remaining=position.shares_remaining,
            pnl=format_currency(scale_pnl),
            reason=reason,
        )

    def _log_to_journal(self, entry: JournalEntry) -> None:
        """Append a trade to the CSV journal file."""
        try:
            with open(self._journal_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=JOURNAL_HEADERS)
                writer.writerow(entry.to_dict())
        except Exception as e:
            log.error("portfolio.journal_error", error=str(e))

    def _ensure_journal_file(self) -> None:
        """Create journal CSV with headers if it doesn't exist."""
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._journal_path.exists():
            try:
                with open(self._journal_path, "w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=JOURNAL_HEADERS)
                    writer.writeheader()
            except Exception as e:
                log.error("portfolio.journal_create_error", error=str(e))
