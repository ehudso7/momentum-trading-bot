"""
Portfolio manager: position tracking, scale-outs, trailing stops, trade journal,
position reconciliation, and partial fill handling.

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
        """
        Open a new position based on a validated trade signal.

        Uses bracket orders so stop-loss and take-profit live on the exchange,
        enforced even if the bot is down between ticks.  When the broker
        rejects or silently drops the bracket legs (e.g. Alpaca paper mode),
        falls back to a standalone stop order.
        """
        # Use first target for the bracket take-profit
        tp_price = signal.target_prices[0] if signal.target_prices else (
            signal.entry_price + 2 * abs(signal.entry_price - signal.stop_price)
        )

        # Submit bracket order: entry + stop + take-profit as one atomic unit
        bracket_ids = self._broker.submit_bracket_order(
            symbol=signal.symbol,
            qty=risk_result.shares,
            side=OrderSide.BUY,
            stop_price=signal.stop_price,
            take_profit_price=tp_price,
        )

        entry_order_id = bracket_ids["entry_order_id"]
        stop_order_id = bracket_ids.get("stop_order_id", "")
        tp_order_id = bracket_ids.get("tp_order_id", "")

        # Verify the entry order actually filled (not rejected/cancelled)
        actual_shares = risk_result.shares
        try:
            order_status = self._broker.get_order_status(entry_order_id)
            status_str = str(order_status.get("status", ""))
            if status_str in ("rejected", "cancelled", "expired", "error"):
                log.error(
                    "portfolio.entry_order_rejected",
                    symbol=signal.symbol,
                    status=status_str,
                    order_id=entry_order_id,
                )
                # Return a zero-share closed position so caller knows it failed
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
                    broker_order_ids=[entry_order_id],
                )
            if order_status.get("filled_qty", 0) > 0:
                actual_shares = order_status["filled_qty"]
                if actual_shares != risk_result.shares:
                    log.warning(
                        "portfolio.partial_fill",
                        symbol=signal.symbol,
                        requested=risk_result.shares,
                        filled=actual_shares,
                    )
        except Exception:
            pass  # Use requested shares if we can't check

        # CRITICAL: If the bracket didn't create a stop leg, submit a
        # standalone stop order so the position is ALWAYS protected.
        if not stop_order_id:
            log.warning(
                "portfolio.bracket_stop_missing",
                symbol=signal.symbol,
                detail="Bracket did not create stop leg — submitting standalone stop",
            )
            try:
                stop_order_id = self._broker.submit_stop_order(
                    symbol=signal.symbol,
                    qty=actual_shares,
                    stop_price=signal.stop_price,
                )
                log.info(
                    "portfolio.fallback_stop_placed",
                    symbol=signal.symbol,
                    stop_order_id=stop_order_id,
                    stop_price=signal.stop_price,
                )
            except Exception as e:
                log.critical(
                    "portfolio.fallback_stop_failed",
                    symbol=signal.symbol,
                    error=str(e),
                    detail="Position opened WITHOUT exchange-side stop protection!",
                )

        # Create position tracking
        position = PositionInfo(
            symbol=signal.symbol,
            side=OrderSide.BUY,
            entry_price=signal.entry_price,
            current_price=signal.entry_price,
            shares=actual_shares,
            shares_remaining=actual_shares,
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
            shares=actual_shares,
            entry=signal.entry_price,
            stop=signal.stop_price,
            target=tp_price,
            risk=format_currency(risk_result.risk_dollars),
            bracket=bool(stop_order_id),
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
            position.pnl_unrealized = (
                position.shares_remaining * (price - position.entry_price)
            )

            # 2b. EMERGENCY: Max loss per position hard cap.
            # If any single position loses more than 3x the intended risk,
            # close immediately. This catches gap-throughs and halt-reopens
            # where the stop was blown past.
            max_loss_multiplier = 3.0
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
                self._cancel_bracket_legs(position)
                entry = self._close_position(position, price, "emergency_max_loss")
                if entry:
                    journal_entries.append(entry)
                symbols_to_remove.append(symbol)
                continue

            # 3. Get bar data for strategy decisions
            bars = market_data.get_intraday_bars(symbol, lookback_bars=50)

            # 4. Check full exit conditions
            should_exit, exit_reason = strategy.should_exit(position, bars)
            if should_exit:
                # Cancel broker-side bracket legs before our own close
                self._cancel_bracket_legs(position)
                entry = self._close_position(position, price, exit_reason)
                if entry:
                    journal_entries.append(entry)
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
                        position.broker_stop_order_id = new_stop_id
                        log.info(
                            "portfolio.broker_stop_updated",
                            symbol=symbol,
                            old_stop=round(old_stop or 0, 4),
                            new_stop=round(new_stop, 4),
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
        entry = self._close_position(position, position.current_price, reason)
        del self._positions[symbol]
        if entry:
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

    def _check_bracket_fills(self, position: PositionInfo) -> Optional[JournalEntry]:
        """
        Check if the broker-side stop or take-profit leg already filled.

        Returns a JournalEntry if the position was closed by the broker,
        otherwise None.
        """
        for order_id, leg_type in [
            (position.broker_stop_order_id, "broker_stop"),
            (position.broker_tp_order_id, "broker_take_profit"),
        ]:
            if not order_id:
                continue
            try:
                status = self._broker.get_order_status(order_id)
                if status.get("status") in ("filled", "partially_filled"):
                    fill_price = status.get("filled_avg_price", position.current_price)
                    filled_qty = status.get("filled_qty", position.shares_remaining)

                    log.info(
                        "portfolio.bracket_filled",
                        symbol=position.symbol,
                        leg=leg_type,
                        fill_price=fill_price,
                        filled_qty=filled_qty,
                    )

                    # Update position state — broker already closed it
                    close_pnl = filled_qty * (fill_price - position.entry_price)
                    total_pnl = position.pnl_realized + close_pnl
                    self._daily_pnl += total_pnl

                    if self._circuit:
                        self._circuit.record_trade_result(total_pnl)

                    risk_per_share = abs(position.entry_price - position.stop_price)
                    rr_ratio = (
                        (fill_price - position.entry_price) / risk_per_share
                        if risk_per_share > 0
                        else 0.0
                    )
                    hold_minutes = (now_et() - position.entry_time).total_seconds() / 60

                    position.status = PositionStatus.CLOSED
                    position.shares_remaining = 0

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
            except Exception as e:
                log.debug(
                    "portfolio.bracket_check_error",
                    symbol=position.symbol,
                    order_id=order_id,
                    error=str(e),
                )
        return None

    def _cancel_bracket_legs(self, position: PositionInfo) -> None:
        """Cancel any outstanding broker-side bracket legs before closing."""
        for order_id in [position.broker_stop_order_id, position.broker_tp_order_id]:
            if order_id:
                try:
                    self._broker.cancel_order(order_id)
                except Exception as e:
                    log.debug(
                        "portfolio.cancel_bracket_error",
                        order_id=order_id,
                        error=str(e),
                    )

    def _close_position(
        self, position: PositionInfo, exit_price: float, reason: str
    ) -> Optional[JournalEntry]:
        """Execute full close and create journal entry."""
        if position.shares_remaining <= 0:
            return None

        # Cancel bracket legs — we're handling the exit ourselves
        self._cancel_bracket_legs(position)
        position.broker_stop_order_id = None
        position.broker_tp_order_id = None

        # Submit sell order with retry on failure — leaving shares open
        # with no stop is the worst possible outcome.
        order_id = None
        for attempt in range(3):
            try:
                order_id = self._broker.submit_market_order(
                    symbol=position.symbol,
                    qty=position.shares_remaining,
                    side=OrderSide.SELL,
                )
                break
            except Exception as e:
                log.error(
                    "portfolio.sell_order_failed",
                    symbol=position.symbol,
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt == 2:
                    # Last resort: try broker's close_position API
                    try:
                        self._broker.close_position(position.symbol)
                        log.warning(
                            "portfolio.fallback_close_position",
                            symbol=position.symbol,
                        )
                    except Exception as e2:
                        log.critical(
                            "portfolio.close_position_all_failed",
                            symbol=position.symbol,
                            error=str(e2),
                            detail="POSITION MAY STILL BE OPEN ON BROKER!",
                        )

        if order_id:
            position.broker_order_ids.append(order_id)

        # Check actual fill for partial fill handling
        actual_exit_price = exit_price
        actual_shares = position.shares_remaining
        if order_id:
            try:
                order_status = self._broker.get_order_status(order_id)
                status_str = str(order_status.get("status", ""))
                if status_str == "rejected":
                    log.critical(
                        "portfolio.sell_order_rejected",
                        symbol=position.symbol,
                        order_id=order_id,
                    )
                if order_status.get("filled_avg_price", 0) > 0:
                    actual_exit_price = order_status["filled_avg_price"]
                if order_status.get("filled_qty", 0) > 0:
                    actual_shares = order_status["filled_qty"]
            except Exception:
                pass

        # Calculate final P&L
        close_pnl = actual_shares * (actual_exit_price - position.entry_price)
        total_pnl = position.pnl_realized + close_pnl
        self._daily_pnl += total_pnl

        # Record in circuit breaker
        if self._circuit:
            self._circuit.record_trade_result(total_pnl)

        # Calculate R multiple
        risk_per_share = abs(position.entry_price - position.stop_price)
        rr_ratio = (
            (actual_exit_price - position.entry_price) / risk_per_share
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

        # Check actual fill
        actual_shares = shares
        actual_price = price
        try:
            order_status = self._broker.get_order_status(order_id)
            if order_status.get("filled_qty", 0) > 0:
                actual_shares = order_status["filled_qty"]
            if order_status.get("filled_avg_price", 0) > 0:
                actual_price = order_status["filled_avg_price"]
        except Exception:
            pass

        # Track realized P&L from scale-out
        scale_pnl = actual_shares * (actual_price - position.entry_price)
        position.pnl_realized += scale_pnl
        position.shares_remaining -= actual_shares
        position.scale_outs_completed += 1

        if position.shares_remaining > 0:
            position.status = PositionStatus.PARTIALLY_CLOSED

            # Replace broker-side stop for reduced share count
            if position.broker_stop_order_id:
                effective_stop = position.trailing_stop_price or position.stop_price
                try:
                    new_stop_id = self._broker.replace_stop_order(
                        order_id=position.broker_stop_order_id,
                        qty=position.shares_remaining,
                        new_stop_price=effective_stop,
                    )
                    position.broker_stop_order_id = new_stop_id
                except Exception as e:
                    log.error(
                        "portfolio.scale_out_stop_replace_error",
                        symbol=position.symbol,
                        error=str(e),
                    )

            # Cancel take-profit leg after first scale-out
            if position.broker_tp_order_id:
                try:
                    self._broker.cancel_order(position.broker_tp_order_id)
                    position.broker_tp_order_id = None
                except Exception:
                    pass
        else:
            position.status = PositionStatus.CLOSED
            self._cancel_bracket_legs(position)

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
