"""
Portfolio manager: position tracking, scale-outs, trailing stops, trade journal,
position reconciliation, and partial fill handling.

Orchestrates the lifecycle of each position from open to close,
including partial exits at R:R targets and trailing stop management.
"""

from __future__ import annotations

import csv
import time
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

_ORDER_FILL_TIMEOUT_SECONDS = 15.0
_BRACKET_LEG_TIMEOUT_SECONDS = 5.0
_ORDER_POLL_INTERVAL_SECONDS = 0.25
_TERMINAL_ORDER_STATUSES = {
    "filled",
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "replaced",
    "done_for_day",
    "error",
}
_CONFIRMED_INACTIVE_ORDER_STATUSES = {
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "replaced",
    "done_for_day",
}


class PortfolioManager:
    """Tracks open positions, manages scale-outs, trailing stops, and P&L journal."""

    def __init__(
        self,
        broker: BrokerBase,
        config: AppConfig,
        circuit_breaker: Optional[CircuitBreaker] = None,
        market_data: Optional[MarketDataProvider] = None,
    ):
        self._broker = broker
        self._config = config
        self._circuit = circuit_breaker
        self._market_data = market_data
        self._positions: dict[str, PositionInfo] = {}
        self._daily_pnl: float = 0.0
        self._journal_path = Path(config.journal_csv_path)
        self._journal_entries: list[JournalEntry] = []
        self._ensure_journal_file()

    def reconcile_positions(self) -> None:
        """
        Reconcile internal position state with broker's actual positions.

        Called on restart to recover from crashes. If the broker has positions
        that we don't track internally, we add them as "recovered" positions.
        If we track positions the broker doesn't have, we clean them up.
        """
        try:
            broker_positions = self._broker.get_positions()
        except Exception as e:
            log.error("portfolio.reconcile_error", error=str(e))
            return

        broker_symbols = {p["symbol"] for p in broker_positions}
        internal_symbols = set(self._positions.keys())

        # Positions broker has that we don't track
        for bp in broker_positions:
            symbol = bp["symbol"]
            if symbol not in internal_symbols:
                log.warning(
                    "portfolio.reconcile_found_untracked",
                    symbol=symbol,
                    qty=bp["qty"],
                    entry=bp["avg_entry_price"],
                )
                # Create a recovery position with conservative stop
                entry_price = bp["avg_entry_price"]
                current_price = bp["current_price"]
                # Conservative stop: 3% below entry
                stop_price = entry_price * 0.97

                from trading_bot.models.domain import SignalType
                self._positions[symbol] = PositionInfo(
                    symbol=symbol,
                    side=OrderSide.BUY,
                    entry_price=entry_price,
                    current_price=current_price,
                    shares=bp["qty"],
                    shares_remaining=bp["qty"],
                    stop_price=stop_price,
                    target_prices=[
                        entry_price * 1.03,
                        entry_price * 1.06,
                    ],
                    status=PositionStatus.OPEN,
                    pnl_unrealized=bp.get("unrealized_pl", 0.0),
                    pnl_realized=0.0,
                    scale_outs_completed=0,
                    entry_time=now_et(),
                    signal_type=SignalType.VWAP_PULLBACK,
                    broker_order_ids=[],
                )

        # Positions we track but broker doesn't have (already closed externally)
        # DO NOT submit sell orders — the broker has no shares to sell.
        # Just record the journal entry for accounting.
        for symbol in internal_symbols - broker_symbols:
            log.warning(
                "portfolio.reconcile_stale_position",
                symbol=symbol,
            )
            pos = self._positions[symbol]
            # Record as closed without sending any orders to broker
            total_pnl = pos.pnl_realized + (
                pos.shares_remaining * (pos.current_price - pos.entry_price)
            )
            self._daily_pnl += total_pnl
            if self._circuit:
                self._circuit.record_trade_result(total_pnl)
            pos.status = PositionStatus.CLOSED
            pos.shares_remaining = 0
            entry = JournalEntry(
                date=now_et().strftime("%Y-%m-%d"),
                symbol=pos.symbol,
                side=pos.side.value,
                signal_type=pos.signal_type.value,
                entry_price=pos.entry_price,
                exit_price=pos.current_price,
                shares=pos.shares,
                pnl=round(total_pnl, 2),
                rr_ratio=0.0,
                hold_time_minutes=0.0,
                entry_time=pos.entry_time.strftime("%H:%M:%S"),
                exit_time=now_et().strftime("%H:%M:%S"),
                exit_reason="reconcile_missing",
            )
            self._log_to_journal(entry)
            self._journal_entries.append(entry)

        # Remove stale positions
        for symbol in internal_symbols - broker_symbols:
            self._positions.pop(symbol, None)

        # Update quantities for matching positions
        for bp in broker_positions:
            symbol = bp["symbol"]
            if symbol in self._positions:
                pos = self._positions[symbol]
                if pos.shares_remaining != bp["qty"]:
                    log.info(
                        "portfolio.reconcile_qty_mismatch",
                        symbol=symbol,
                        internal=pos.shares_remaining,
                        broker=bp["qty"],
                    )
                    # Broker is source of truth for partial fills
                    difference = pos.shares_remaining - bp["qty"]
                    if difference > 0:
                        # Some shares were sold (partial fill we missed)
                        pnl = difference * (bp["current_price"] - pos.entry_price)
                        pos.pnl_realized += pnl
                        pos.shares_remaining = bp["qty"]
                        if pos.shares_remaining == 0:
                            pos.status = PositionStatus.CLOSED

        log.info(
            "portfolio.reconciled",
            internal=len(self._positions),
            broker=len(broker_positions),
        )

    def open_position(
        self, signal: TradeSignal, risk_result: RiskCheckResult
    ) -> PositionInfo:
        """Open a position only after the broker confirms the complete bracket.

        A market submission response is not proof of a fill, and Alpaca does
        not attach bracket legs until the parent has fully filled.  This method
        waits for both facts and fails closed if either cannot be proven.
        """
        tp_price = signal.target_prices[0] if signal.target_prices else (
            signal.entry_price + 2 * abs(signal.entry_price - signal.stop_price)
        )

        try:
            bracket_ids = self._broker.submit_bracket_order(
                symbol=signal.symbol,
                qty=risk_result.shares,
                side=OrderSide.BUY,
                stop_price=signal.stop_price,
                take_profit_price=tp_price,
            )
        except Exception as exc:
            log.error(
                "portfolio.bracket_order_failed",
                symbol=signal.symbol,
                error=str(exc),
            )
            self._cleanup_failed_entry(signal.symbol)
            return self._failed_position(signal)

        entry_order_id = bracket_ids["entry_order_id"]
        order_status = self._wait_for_order(
            entry_order_id,
            timeout_seconds=_ORDER_FILL_TIMEOUT_SECONDS,
        )
        status = self._order_status(order_status)
        filled_qty = int(order_status.get("filled_qty", 0) or 0)

        if status != "filled" or filled_qty != risk_result.shares:
            log.critical(
                "portfolio.entry_fill_unconfirmed",
                symbol=signal.symbol,
                order_id=entry_order_id,
                status=status,
                requested=risk_result.shares,
                filled=filled_qty,
                detail="Cancelling the parent and flattening any partial fill",
            )
            self._cleanup_failed_entry(signal.symbol, entry_order_id)
            return self._failed_position(signal, [entry_order_id])

        actual_entry_price = float(
            order_status.get("filled_avg_price", 0.0) or signal.entry_price
        )
        stop_order_id, tp_order_id = self._wait_for_bracket_legs(
            entry_order_id,
            initial_stop_id=bracket_ids.get("stop_order_id", ""),
            initial_tp_id=bracket_ids.get("tp_order_id", ""),
        )
        if not stop_order_id or not tp_order_id:
            log.critical(
                "portfolio.bracket_legs_unconfirmed",
                symbol=signal.symbol,
                order_id=entry_order_id,
                stop_order_id=stop_order_id,
                tp_order_id=tp_order_id,
                detail="No standalone fallback is submitted beside an ambiguous bracket",
            )
            self._cleanup_failed_entry(signal.symbol, entry_order_id)
            return self._failed_position(signal, [entry_order_id])

        position = PositionInfo(
            symbol=signal.symbol,
            side=OrderSide.BUY,
            entry_price=actual_entry_price,
            current_price=actual_entry_price,
            shares=filled_qty,
            shares_remaining=filled_qty,
            stop_price=signal.stop_price,
            target_prices=signal.target_prices,
            status=PositionStatus.OPEN,
            pnl_unrealized=0.0,
            pnl_realized=0.0,
            scale_outs_completed=0,
            entry_time=now_et(),
            signal_type=signal.signal_type,
            broker_order_ids=[entry_order_id],
            broker_stop_order_id=stop_order_id,
            broker_tp_order_id=tp_order_id,
        )
        self._positions[signal.symbol] = position

        log.info(
            "portfolio.opened",
            symbol=signal.symbol,
            shares=filled_qty,
            entry=actual_entry_price,
            stop=signal.stop_price,
            target=tp_price,
            risk=format_currency(risk_result.risk_dollars),
            bracket=True,
        )
        return position

    def update_positions(
        self, strategy: Strategy, market_data: MarketDataProvider
    ) -> list[JournalEntry]:
        """
        Update all open positions. Called every tick.

        For each position:
        1. Check if broker-side stop/TP already filled (bracket OCO)
        2. Fetch latest price
        3. Check for exits (stop, time, PSAR)
        4. Check for scale-outs at R:R targets
        5. Update trailing stop (and replace broker stop order)
        6. Update unrealized P&L

        Returns list of journal entries for any closed trades.
        """
        journal_entries = []
        symbols_to_remove = []

        for symbol, position in self._positions.items():
            if position.status == PositionStatus.CLOSED:
                symbols_to_remove.append(symbol)
                continue

            # 1. Check if the broker-side stop or take-profit already fired
            broker_closed = self._check_bracket_fills(position)
            if broker_closed:
                journal_entries.append(broker_closed)
                symbols_to_remove.append(symbol)
                continue

            # 2. Update current price
            price = market_data.get_current_price(symbol)
            if price is None:
                log.warning("portfolio.price_unavailable", symbol=symbol)
                continue

            position.current_price = price

            # Track high-water mark for smarter trailing stops
            if position.high_water_mark is None or price > position.high_water_mark:
                position.high_water_mark = price

            # Feed price to paper broker so bracket stops/TPs trigger
            if hasattr(self._broker, "update_price"):
                self._broker.update_price(symbol, price)
            position.pnl_unrealized = (
                position.shares_remaining * (price - position.entry_price)
            )

            # 2b. EMERGENCY: Max loss per position hard cap.
            # If any single position loses more than 2x the intended risk,
            # close immediately. This catches gap-throughs and halt-reopens
            # where the stop was blown past.
            max_loss_multiplier = 2.0
            intended_risk = abs(position.entry_price - position.stop_price) * position.shares
            max_allowed_loss = intended_risk * max_loss_multiplier
            actual_loss = position.shares_remaining * (position.entry_price - price)
            if actual_loss > max_allowed_loss and actual_loss > 0:
                log.critical(
                    "portfolio.emergency_max_loss_exit",
                    symbol=symbol,
                    actual_loss=round(actual_loss, 2),
                    max_allowed=round(max_allowed_loss, 2),
                    intended_risk=round(intended_risk, 2),
                    price=price,
                    entry=position.entry_price,
                )
                entry = self._close_position(position, price, "emergency_max_loss")
                if entry:
                    journal_entries.append(entry)
                    symbols_to_remove.append(symbol)
                elif position.status == PositionStatus.CLOSED:
                    symbols_to_remove.append(symbol)
                continue

            # 3. Get bar data for strategy decisions
            bars = market_data.get_intraday_bars(symbol, lookback_bars=50)

            # 4. Check full exit conditions
            should_exit, exit_reason = strategy.should_exit(position, bars)
            if should_exit:
                entry = self._close_position(position, price, exit_reason)
                if entry:
                    journal_entries.append(entry)
                    symbols_to_remove.append(symbol)
                elif position.status == PositionStatus.CLOSED:
                    symbols_to_remove.append(symbol)
                continue

            # 5. Check scale-outs
            scale_result = strategy.compute_scale_out(position, price)
            if scale_result:
                shares_to_sell, scale_reason = scale_result
                self._execute_scale_out(position, shares_to_sell, price, scale_reason)

            # 6. Update trailing stop (and replace broker-side stop)
            new_stop = strategy.get_trailing_stop(position, bars)
            if new_stop and (
                not position.trailing_stop_price
                or new_stop > position.trailing_stop_price
            ):
                position.trailing_stop_active = True
                old_stop = position.trailing_stop_price
                position.trailing_stop_price = new_stop

                # Replace the broker-side stop order at the new price
                if position.broker_stop_order_id:
                    try:
                        new_stop_id = self._broker.replace_stop_order(
                            order_id=position.broker_stop_order_id,
                            qty=position.shares_remaining,
                            new_stop_price=new_stop,
                        )
                        if new_stop_id:
                            position.broker_stop_order_id = new_stop_id
                            log.info(
                                "portfolio.broker_stop_updated",
                                symbol=symbol,
                                old_stop=round(old_stop or 0, 4),
                                new_stop=round(new_stop, 4),
                            )
                        else:
                            # Empty is ambiguous: a replacement may have been
                            # accepted despite a lost response.  Never add a
                            # second sell order until reconciliation proves the
                            # first one terminal.
                            log.critical(
                                "portfolio.broker_stop_replace_unconfirmed",
                                symbol=symbol,
                                order_id=position.broker_stop_order_id,
                            )
                    except Exception as e:
                        log.error(
                            "portfolio.broker_stop_replace_error",
                            symbol=symbol,
                            error=str(e),
                        )
                else:
                    log.debug(
                        "portfolio.trailing_stop_updated",
                        symbol=symbol,
                        new_stop=round(new_stop, 4),
                    )

        # Clean up closed positions
        for symbol in symbols_to_remove:
            if symbol in self._positions:
                del self._positions[symbol]

        self._journal_entries.extend(journal_entries)
        return journal_entries

    def close_position(self, symbol: str, reason: str) -> Optional[JournalEntry]:
        """Close an entire position by symbol."""
        if symbol not in self._positions:
            return None

        position = self._positions[symbol]

        # Fetch fresh price instead of using potentially stale current_price
        exit_price = position.current_price
        if self._market_data:
            try:
                fresh_price = self._market_data.get_current_price(symbol)
                if fresh_price is not None:
                    exit_price = fresh_price
                    position.current_price = fresh_price
            except Exception:
                pass  # Use last known price

        entry = self._close_position(position, exit_price, reason)
        if entry is None:
            # Sell was rejected — position is still open, don't remove
            return None
        del self._positions[symbol]
        self._journal_entries.append(entry)
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

    def get_daily_journal_entries(self) -> list[JournalEntry]:
        """Get all journal entries from today's session."""
        return list(self._journal_entries)

    def reset_daily(self) -> None:
        """Reset daily P&L tracking."""
        self._daily_pnl = 0.0
        self._journal_entries.clear()

    # --- Private methods ---

    @staticmethod
    def _order_status(order: dict) -> str:
        """Normalize broker status strings at the portfolio boundary."""
        status = str(order.get("status", "") or "").strip().lower()
        if "." in status:
            status = status.rsplit(".", 1)[-1]
        return status

    def _wait_for_order(self, order_id: str, timeout_seconds: float) -> dict:
        """Poll until an order is terminal or the bounded timeout expires."""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        last_status: dict = {
            "id": order_id,
            "status": "unknown",
            "filled_qty": 0,
            "filled_avg_price": 0.0,
        }
        while True:
            try:
                last_status = self._broker.get_order_status(order_id)
            except Exception as exc:
                last_status = {
                    "id": order_id,
                    "status": "error",
                    "filled_qty": 0,
                    "filled_avg_price": 0.0,
                    "error": str(exc),
                }
            if self._order_status(last_status) in _TERMINAL_ORDER_STATUSES:
                return last_status
            if time.monotonic() >= deadline:
                return last_status
            time.sleep(_ORDER_POLL_INTERVAL_SECONDS)

    @staticmethod
    def _leg_ids(order_status: dict) -> tuple[str, str]:
        stop_id = ""
        tp_id = ""
        for leg in order_status.get("legs", []) or []:
            leg_type = str(leg.get("type", "") or "").lower()
            if "." in leg_type:
                leg_type = leg_type.rsplit(".", 1)[-1]
            if float(leg.get("stop_price", 0.0) or 0.0) > 0 or leg_type in {
                "stop",
                "stop_limit",
                "trailing_stop",
            }:
                stop_id = str(leg.get("id", "") or "")
            elif float(leg.get("limit_price", 0.0) or 0.0) > 0 or leg_type == "limit":
                tp_id = str(leg.get("id", "") or "")
        return stop_id, tp_id

    def _wait_for_bracket_legs(
        self,
        entry_order_id: str,
        initial_stop_id: str = "",
        initial_tp_id: str = "",
    ) -> tuple[str, str]:
        """Retrieve activated bracket legs from the filled parent order."""
        stop_id = initial_stop_id
        tp_id = initial_tp_id
        deadline = time.monotonic() + _BRACKET_LEG_TIMEOUT_SECONDS
        while not stop_id or not tp_id:
            try:
                parent = self._broker.get_order_status(entry_order_id)
            except Exception:
                parent = {}
            nested_stop_id, nested_tp_id = self._leg_ids(parent)
            stop_id = stop_id or nested_stop_id
            tp_id = tp_id or nested_tp_id
            if stop_id and tp_id:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(_ORDER_POLL_INTERVAL_SECONDS)
        return stop_id, tp_id

    def _broker_position_qty(self, symbol: str) -> Optional[int]:
        """Return the broker's signed quantity, or None if it cannot be proven."""
        try:
            for position in self._broker.get_positions():
                if position.get("symbol") == symbol:
                    return int(position.get("qty", 0))
            return 0
        except Exception as exc:
            log.error(
                "portfolio.position_qty_lookup_failed",
                symbol=symbol,
                error=str(exc),
            )
            return None

    def _cleanup_failed_entry(
        self, symbol: str, entry_order_id: Optional[str] = None
    ) -> None:
        """Cancel an uncertain parent and flatten any resulting broker position."""
        if entry_order_id:
            try:
                self._broker.cancel_order(entry_order_id)
            except Exception as exc:
                log.warning(
                    "portfolio.entry_cancel_error",
                    symbol=symbol,
                    order_id=entry_order_id,
                    error=str(exc),
                )

        qty = self._broker_position_qty(symbol)
        if qty is None:
            log.critical(
                "portfolio.failed_entry_position_unknown",
                symbol=symbol,
                detail="Broker position could not be verified; manual inspection required",
            )
            return
        if qty:
            try:
                closed = self._broker.close_position(symbol)
            except Exception as exc:
                closed = False
                log.critical(
                    "portfolio.failed_entry_close_error",
                    symbol=symbol,
                    qty=qty,
                    error=str(exc),
                )
            if not closed or self._broker_position_qty(symbol) not in (0, None):
                log.critical(
                    "portfolio.failed_entry_cleanup_unconfirmed",
                    symbol=symbol,
                    qty=qty,
                    detail="Manual broker inspection required before restart",
                )

    @staticmethod
    def _failed_position(
        signal: TradeSignal, broker_order_ids: Optional[list[str]] = None
    ) -> PositionInfo:
        return PositionInfo(
            symbol=signal.symbol,
            side=OrderSide.BUY,
            entry_price=signal.entry_price,
            current_price=signal.entry_price,
            shares=0,
            shares_remaining=0,
            stop_price=signal.stop_price,
            target_prices=signal.target_prices,
            status=PositionStatus.CLOSED,
            pnl_unrealized=0.0,
            pnl_realized=0.0,
            scale_outs_completed=0,
            entry_time=now_et(),
            signal_type=signal.signal_type,
            broker_order_ids=broker_order_ids or [],
        )

    def _check_bracket_fills(self, position: PositionInfo) -> Optional[JournalEntry]:
        """Reconcile cumulative broker leg fills without counting them twice."""
        for order_id, leg_type in [
            (position.broker_stop_order_id, "broker_stop"),
            (position.broker_tp_order_id, "broker_take_profit"),
        ]:
            if not order_id:
                continue
            try:
                status = self._broker.get_order_status(order_id)
                order_status = self._order_status(status)
                if order_status not in ("filled", "partially_filled"):
                    continue

                cumulative_qty = int(status.get("filled_qty", 0) or 0)
                fill_price = float(
                    status.get("filled_avg_price", 0.0) or position.current_price
                )
                previous_qty = position.broker_filled_qty.get(order_id, 0)
                previous_notional = position.broker_filled_notional.get(order_id, 0.0)
                newly_filled_qty = max(0, cumulative_qty - previous_qty)
                cumulative_notional = cumulative_qty * fill_price
                newly_filled_notional = max(
                    0.0, cumulative_notional - previous_notional
                )
                if newly_filled_qty <= 0:
                    continue

                newly_filled_qty = min(
                    newly_filled_qty, position.shares_remaining
                )
                position.broker_filled_qty[order_id] = cumulative_qty
                position.broker_filled_notional[order_id] = cumulative_notional

                close_pnl = (
                    newly_filled_notional
                    - newly_filled_qty * position.entry_price
                )
                position.pnl_realized += close_pnl
                position.shares_remaining -= newly_filled_qty

                log.info(
                    "portfolio.bracket_filled",
                    symbol=position.symbol,
                    leg=leg_type,
                    fill_price=fill_price,
                    filled_qty=newly_filled_qty,
                    cumulative_qty=cumulative_qty,
                    partial=(position.shares_remaining > 0),
                )

                if position.shares_remaining > 0:
                    position.status = PositionStatus.PARTIALLY_CLOSED
                    log.warning(
                        "portfolio.bracket_partial_fill",
                        symbol=position.symbol,
                        filled=newly_filled_qty,
                        remaining=position.shares_remaining,
                    )
                    return None

                position.shares_remaining = 0
                position.status = PositionStatus.CLOSED
                total_pnl = position.pnl_realized
                self._daily_pnl += total_pnl
                if self._circuit:
                    self._circuit.record_trade_result(total_pnl)

                risk_per_share = abs(position.entry_price - position.stop_price)
                rr_ratio = (
                    (fill_price - position.entry_price) / risk_per_share
                    if risk_per_share > 0
                    else 0.0
                )
                hold_minutes = (
                    now_et() - position.entry_time
                ).total_seconds() / 60
                entry = JournalEntry(
                    date=now_et().strftime("%Y-%m-%d"),
                    symbol=position.symbol,
                    side=position.side.value,
                    signal_type=position.signal_type.value,
                    entry_price=position.entry_price,
                    exit_price=fill_price,
                    shares=position.shares,
                    pnl=round(total_pnl, 2),
                    rr_ratio=round(rr_ratio, 2),
                    hold_time_minutes=round(hold_minutes, 1),
                    entry_time=position.entry_time.strftime("%H:%M:%S"),
                    exit_time=now_et().strftime("%H:%M:%S"),
                    exit_reason=leg_type,
                )
                self._log_to_journal(entry)
                return entry
            except Exception as exc:
                log.warning(
                    "portfolio.bracket_check_error",
                    symbol=position.symbol,
                    order_id=order_id,
                    error=str(exc),
                )
        return None

    def _cancel_bracket_legs(self, position: PositionInfo) -> bool:
        """Cancel every tracked leg and require terminal confirmation."""
        all_canceled = True
        for attr in ("broker_stop_order_id", "broker_tp_order_id"):
            order_id = getattr(position, attr)
            if not order_id:
                continue
            try:
                canceled = self._broker.cancel_order(order_id)
            except Exception as exc:
                canceled = False
                log.warning(
                    "portfolio.cancel_bracket_error",
                    order_id=order_id,
                    error=str(exc),
                )
            if not canceled:
                try:
                    status = self._order_status(
                        self._broker.get_order_status(order_id)
                    )
                except Exception:
                    status = "error"
                canceled = status in _CONFIRMED_INACTIVE_ORDER_STATUSES
            if canceled:
                setattr(position, attr, None)
            else:
                all_canceled = False
                log.critical(
                    "portfolio.cancel_bracket_unconfirmed",
                    symbol=position.symbol,
                    order_id=order_id,
                )
        return all_canceled

    def _restore_stop(self, position: PositionInfo, qty: int) -> None:
        """Restore protection only after every prior exit leg is confirmed gone."""
        if qty <= 0 or position.broker_stop_order_id:
            return
        effective_stop = position.trailing_stop_price or position.stop_price
        try:
            position.broker_stop_order_id = self._broker.submit_stop_order(
                symbol=position.symbol,
                qty=qty,
                stop_price=effective_stop,
            )
            log.warning(
                "portfolio.emergency_stop_restored",
                symbol=position.symbol,
                qty=qty,
                stop_price=effective_stop,
            )
        except Exception as exc:
            log.critical(
                "portfolio.emergency_stop_restore_failed",
                symbol=position.symbol,
                qty=qty,
                error=str(exc),
            )

    def _finalize_close(
        self,
        position: PositionInfo,
        actual_exit_price: float,
        actual_shares: int,
        reason: str,
    ) -> JournalEntry:
        close_pnl = actual_shares * (
            actual_exit_price - position.entry_price
        )
        position.pnl_realized += close_pnl
        total_pnl = position.pnl_realized
        self._daily_pnl += total_pnl
        if self._circuit:
            self._circuit.record_trade_result(total_pnl)

        risk_per_share = abs(position.entry_price - position.stop_price)
        rr_ratio = (
            (actual_exit_price - position.entry_price) / risk_per_share
            if risk_per_share > 0
            else 0.0
        )
        hold_minutes = (
            now_et() - position.entry_time
        ).total_seconds() / 60
        position.status = PositionStatus.CLOSED
        position.shares_remaining = 0

        entry = JournalEntry(
            date=now_et().strftime("%Y-%m-%d"),
            symbol=position.symbol,
            side=position.side.value,
            signal_type=position.signal_type.value,
            entry_price=position.entry_price,
            exit_price=actual_exit_price,
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

    def _close_position(
        self, position: PositionInfo, exit_price: float, reason: str
    ) -> Optional[JournalEntry]:
        """Close only a broker-confirmed long and wait for the terminal fill."""
        if position.shares_remaining <= 0:
            return None

        bracket_entry = self._check_bracket_fills(position)
        if bracket_entry is not None:
            return bracket_entry

        broker_qty = self._broker_position_qty(position.symbol)
        if broker_qty is None:
            log.critical(
                "portfolio.close_blocked_unknown_broker_qty",
                symbol=position.symbol,
            )
            return None
        if broker_qty < 0:
            log.critical(
                "portfolio.unexpected_short_detected",
                symbol=position.symbol,
                broker_qty=broker_qty,
                detail="Flattening via broker close endpoint; no sell will be sent",
            )
            if self._broker.close_position(position.symbol):
                return self._finalize_close(
                    position,
                    exit_price,
                    position.shares_remaining,
                    f"{reason}_short_reconciled",
                )
            return None
        if broker_qty == 0:
            # A protective leg or external action won the race.  Never send a
            # sell into an already-flat account.
            bracket_entry = self._check_bracket_fills(position)
            if bracket_entry is not None:
                return bracket_entry
            log.critical(
                "portfolio.close_skipped_broker_flat",
                symbol=position.symbol,
                internal_qty=position.shares_remaining,
            )
            return self._finalize_close(
                position,
                exit_price,
                position.shares_remaining,
                f"{reason}_broker_flat",
            )
        if broker_qty > position.shares_remaining:
            log.critical(
                "portfolio.close_blocked_qty_divergence",
                symbol=position.symbol,
                internal_qty=position.shares_remaining,
                broker_qty=broker_qty,
            )
            return None
        if broker_qty < position.shares_remaining:
            log.warning(
                "portfolio.close_qty_reconciled",
                symbol=position.symbol,
                internal_qty=position.shares_remaining,
                broker_qty=broker_qty,
            )
            position.shares_remaining = broker_qty

        if not self._cancel_bracket_legs(position):
            bracket_entry = self._check_bracket_fills(position)
            if bracket_entry is not None:
                return bracket_entry
            log.critical(
                "portfolio.close_blocked_active_exit_leg",
                symbol=position.symbol,
            )
            return None

        # Re-read after cancellation because a leg may have filled while the
        # cancellation was in flight.
        broker_qty = self._broker_position_qty(position.symbol)
        if broker_qty is None:
            self._restore_stop(position, position.shares_remaining)
            return None
        if broker_qty <= 0:
            if broker_qty < 0:
                self._broker.close_position(position.symbol)
            return self._finalize_close(
                position,
                exit_price,
                position.shares_remaining,
                f"{reason}_cancel_race",
            )
        qty_to_sell = min(position.shares_remaining, broker_qty)

        try:
            order_id = self._broker.submit_market_order(
                symbol=position.symbol,
                qty=qty_to_sell,
                side=OrderSide.SELL,
            )
        except Exception as exc:
            log.critical(
                "portfolio.sell_order_failed",
                symbol=position.symbol,
                qty=qty_to_sell,
                error=str(exc),
            )
            self._restore_stop(position, broker_qty)
            return None

        position.broker_order_ids.append(order_id)
        order_status = self._wait_for_order(
            order_id,
            timeout_seconds=_ORDER_FILL_TIMEOUT_SECONDS,
        )
        status = self._order_status(order_status)
        filled_qty = min(
            int(order_status.get("filled_qty", 0) or 0),
            qty_to_sell,
        )
        actual_exit_price = float(
            order_status.get("filled_avg_price", 0.0) or exit_price
        )

        if status != "filled":
            # A still-live partial/accepted exit must be canceled before a new
            # stop is introduced.  SELL_TO_CLOSE is an additional broker-side
            # guard, but cancellation confirmation remains mandatory.
            cancellation_confirmed = (
                status in _CONFIRMED_INACTIVE_ORDER_STATUSES
            )
            if not cancellation_confirmed:
                cancellation_confirmed = self._broker.cancel_order(order_id)
                order_status = self._broker.get_order_status(order_id)
                status = self._order_status(order_status)
                cancellation_confirmed = cancellation_confirmed or (
                    status in _CONFIRMED_INACTIVE_ORDER_STATUSES
                )
                filled_qty = min(
                    int(order_status.get("filled_qty", 0) or 0),
                    qty_to_sell,
                )
                actual_exit_price = float(
                    order_status.get("filled_avg_price", 0.0) or exit_price
                )

            # The cancellation can lose the race to a complete fill.  Treat it
            # as the successful close that it is, rather than as a partial.
            if status == "filled" and filled_qty == qty_to_sell:
                return self._finalize_close(
                    position,
                    actual_exit_price,
                    filled_qty,
                    reason,
                )

            if not cancellation_confirmed:
                log.critical(
                    "portfolio.sell_cancel_unconfirmed",
                    symbol=position.symbol,
                    order_id=order_id,
                    status=status,
                    detail="No replacement stop submitted beside a possibly-live sell",
                )
                return None

            if filled_qty > 0:
                position.pnl_realized += filled_qty * (
                    actual_exit_price - position.entry_price
                )
                remaining = self._broker_position_qty(position.symbol)
                position.shares_remaining = (
                    remaining
                    if remaining is not None and remaining >= 0
                    else max(0, position.shares_remaining - filled_qty)
                )
                position.status = PositionStatus.PARTIALLY_CLOSED
                self._restore_stop(position, position.shares_remaining)
                log.warning(
                    "portfolio.sell_partial_fill",
                    symbol=position.symbol,
                    filled=filled_qty,
                    remaining=position.shares_remaining,
                    status=status,
                )
                return None

            self._restore_stop(position, broker_qty)
            log.critical(
                "portfolio.sell_not_filled",
                symbol=position.symbol,
                order_id=order_id,
                status=status,
            )
            return None

        if filled_qty != qty_to_sell:
            position.pnl_realized += filled_qty * (
                actual_exit_price - position.entry_price
            )
            remaining = self._broker_position_qty(position.symbol)
            position.shares_remaining = (
                remaining
                if remaining is not None and remaining >= 0
                else max(0, position.shares_remaining - filled_qty)
            )
            position.status = PositionStatus.PARTIALLY_CLOSED
            self._restore_stop(position, position.shares_remaining)
            return None

        return self._finalize_close(
            position,
            actual_exit_price,
            filled_qty,
            reason,
        )

    def _execute_scale_out(
        self, position: PositionInfo, shares: int, price: float, reason: str
    ) -> None:
        """Execute and account for only the quantity the broker confirms filled."""
        broker_qty = self._broker_position_qty(position.symbol)
        if broker_qty is None or broker_qty <= 0:
            log.critical(
                "portfolio.scale_out_blocked_broker_qty",
                symbol=position.symbol,
                broker_qty=broker_qty,
            )
            return
        shares_to_sell = min(shares, position.shares_remaining, broker_qty)
        if shares_to_sell <= 0:
            return

        # Alpaca reserves position quantity for open sell orders.  More
        # importantly, submitting a scale-out while the full-size stop and
        # take-profit remain live creates competing exits.  Prove the bracket
        # inactive before placing the scale-out; protection is restored below
        # for the broker-confirmed remainder.
        if not self._cancel_bracket_legs(position):
            self._check_bracket_fills(position)
            log.critical(
                "portfolio.scale_out_blocked_active_exit_leg",
                symbol=position.symbol,
            )
            return

        broker_qty = self._broker_position_qty(position.symbol)
        if broker_qty is None:
            log.critical(
                "portfolio.scale_out_blocked_unknown_qty_after_cancel",
                symbol=position.symbol,
            )
            return
        if broker_qty <= 0:
            self._check_bracket_fills(position)
            return
        shares_to_sell = min(shares_to_sell, broker_qty)

        try:
            order_id = self._broker.submit_market_order(
                symbol=position.symbol,
                qty=shares_to_sell,
                side=OrderSide.SELL,
            )
        except Exception as exc:
            log.error(
                "portfolio.scale_out_order_failed",
                symbol=position.symbol,
                shares=shares_to_sell,
                error=str(exc),
            )
            return
        position.broker_order_ids.append(order_id)

        order_status = self._wait_for_order(
            order_id,
            timeout_seconds=_ORDER_FILL_TIMEOUT_SECONDS,
        )
        status = self._order_status(order_status)
        cancellation_confirmed = status in _CONFIRMED_INACTIVE_ORDER_STATUSES
        if status != "filled" and not cancellation_confirmed:
            cancellation_confirmed = self._broker.cancel_order(order_id)
            order_status = self._broker.get_order_status(order_id)
            status = self._order_status(order_status)
            cancellation_confirmed = cancellation_confirmed or (
                status in _CONFIRMED_INACTIVE_ORDER_STATUSES
            )

        actual_shares = min(
            int(order_status.get("filled_qty", 0) or 0),
            shares_to_sell,
        )
        if actual_shares <= 0:
            if not cancellation_confirmed and status != "filled":
                log.critical(
                    "portfolio.scale_out_cancel_unconfirmed",
                    symbol=position.symbol,
                    order_id=order_id,
                    status=status,
                    detail="No replacement stop submitted beside a possibly-live sell",
                )
                return
            self._restore_stop(position, broker_qty)
            log.error(
                "portfolio.scale_out_not_filled",
                symbol=position.symbol,
                order_id=order_id,
                status=status,
            )
            return
        actual_price = float(
            order_status.get("filled_avg_price", 0.0) or price
        )

        scale_pnl = actual_shares * (
            actual_price - position.entry_price
        )
        position.pnl_realized += scale_pnl
        broker_remaining = self._broker_position_qty(position.symbol)
        position.shares_remaining = (
            broker_remaining
            if broker_remaining is not None and broker_remaining >= 0
            else max(0, position.shares_remaining - actual_shares)
        )
        position.scale_outs_completed += 1

        if position.shares_remaining > 0:
            position.status = PositionStatus.PARTIALLY_CLOSED
            self._restore_stop(position, position.shares_remaining)
        else:
            position.status = PositionStatus.CLOSED

        log.info(
            "portfolio.scale_out",
            symbol=position.symbol,
            shares_sold=actual_shares,
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
