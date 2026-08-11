"""
Portfolio manager: position tracking, scale-outs, trailing stops, trade journal,
position reconciliation, and partial fill handling.

Orchestrates the lifecycle of each position from open to close,
including partial exits at R:R targets and trailing stop management.
"""

from __future__ import annotations

import csv
import math
import time
from decimal import Decimal, InvalidOperation
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
    "canceled_oco",
    "cancelled_oco",
    "canceled_stale",
    "cancelled_stale",
    "expired",
    "rejected",
    "replaced",
    "done_for_day",
}
_CONFIRMED_INACTIVE_ORDER_STATUSES = {
    "canceled",
    "cancelled",
    "canceled_oco",
    "cancelled_oco",
    "canceled_stale",
    "cancelled_stale",
    "expired",
    "rejected",
    "replaced",
    "done_for_day",
}
_OBSERVABLE_ORDER_STATUSES = _TERMINAL_ORDER_STATUSES | {
    "accepted",
    "new",
    "pending_new",
    "partially_filled",
}
_ACTIVE_STOP_STATUSES = {"new", "held"}
_STOP_ORDER_TYPES = {"stop", "stop_limit", "trailing_stop"}


def _strict_integral_qty(
    value: object,
    *,
    allow_zero: bool = False,
    allow_signed: bool = False,
) -> Optional[int]:
    """Parse an exact share quantity without ever truncating broker data.

    Alpaca commonly serializes integral quantities as strings such as
    ``"647.0"``.  Those are valid, but booleans, non-finite values, and any
    fractional value are not.  Signed values are accepted only by callers
    reading broker *position* quantities; order quantities remain positive,
    while cumulative fill quantities may explicitly opt into zero.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        parsed = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        return None
    quantity = int(parsed)
    if allow_signed:
        return quantity
    if allow_zero:
        return quantity if quantity >= 0 else None
    return quantity if quantity > 0 else None


class PortfolioSafetyError(RuntimeError):
    """Raised when broker state cannot be made provably safe."""


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

        A fresh process has no trustworthy map from broker positions to their
        protective exits. It therefore cancels all broker orders and flattens
        all signed positions, then proves the account is empty. An untracked
        position or unexpected short found later triggers the same global
        fail-closed cleanup. Broker holdings are never silently adopted.
        """
        internal_symbols = set(self._positions.keys())

        if not internal_symbols:
            self._close_all_and_verify("startup_reconcile")
            log.info("portfolio.reconciled", internal=0, broker=0)
            return

        try:
            broker_positions = self._broker.get_positions()
        except Exception as e:
            self._raise_safety_error(
                f"reconciliation position lookup failed: {e}"
            )

        broker_by_symbol: dict[str, tuple[dict, int]] = {}
        divergence: list[dict[str, object]] = []
        for broker_position in broker_positions:
            symbol = str(broker_position.get("symbol", "") or "").upper()
            broker_qty = _strict_integral_qty(
                broker_position.get("qty"), allow_signed=True
            )
            if (
                not symbol
                or symbol in broker_by_symbol
                or symbol not in internal_symbols
                or broker_qty is None
                or broker_qty <= 0
            ):
                divergence.append(
                    {
                        "symbol": symbol,
                        "broker_qty": broker_position.get("qty"),
                        "reason": "untrusted broker position",
                    }
                )
                continue
            broker_by_symbol[symbol] = (broker_position, broker_qty)

        for symbol in internal_symbols:
            position = self._positions[symbol]
            internal_qty = _strict_integral_qty(position.shares_remaining)
            broker_match = broker_by_symbol.get(symbol)
            if broker_match is None:
                divergence.append(
                    {
                        "symbol": symbol,
                        "internal_qty": position.shares_remaining,
                        "broker_qty": None,
                        "reason": "tracked position missing at broker",
                    }
                )
                continue
            _broker_position, broker_qty = broker_match
            if internal_qty is None or broker_qty != internal_qty:
                divergence.append(
                    {
                        "symbol": symbol,
                        "internal_qty": position.shares_remaining,
                        "broker_qty": broker_qty,
                        "reason": "position quantity mismatch",
                    }
                )

        if divergence:
            log.critical(
                "portfolio.reconcile_state_divergence",
                divergence=divergence,
                detail="Cancelling all orders and flattening all positions",
            )
            self._close_all_and_verify("reconcile_state_divergence")
            self._mark_all_positions_closed()
            self._raise_safety_error(
                f"reconcile_state_divergence: {divergence}"
            )

        # Exact quantities alone are insufficient: every recovered internal
        # position must still have freshly proven, exact broker protection.
        for position in self._positions.values():
            self._require_live_stop(position, context="reconcile_position")

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
        requested_shares = _strict_integral_qty(risk_result.shares)
        if requested_shares is None:
            self._raise_safety_error(
                f"entry_quantity_invalid:{signal.symbol}: "
                f"shares={risk_result.shares!r}"
            )

        tp_price = signal.target_prices[0] if signal.target_prices else (
            signal.entry_price + 2 * abs(signal.entry_price - signal.stop_price)
        )

        try:
            bracket_ids = self._broker.submit_bracket_order(
                symbol=signal.symbol,
                qty=requested_shares,
                side=OrderSide.BUY,
                stop_price=signal.stop_price,
                take_profit_price=tp_price,
            )
        except Exception as exc:
            log.critical(
                "portfolio.bracket_order_failed",
                symbol=signal.symbol,
                error=str(exc),
            )
            self._cleanup_failed_entry(signal.symbol)
            self._raise_safety_error(
                f"entry_submission_ambiguous:{signal.symbol}: {exc}"
            )

        entry_order_id = (
            str(bracket_ids.get("entry_order_id", "") or "")
            if isinstance(bracket_ids, dict)
            else ""
        )
        if not entry_order_id:
            self._cleanup_failed_entry(signal.symbol)
            self._raise_safety_error(
                f"entry_submission_ambiguous:{signal.symbol}: "
                "missing entry order id"
            )
        order_status = self._wait_for_order(
            entry_order_id,
            timeout_seconds=_ORDER_FILL_TIMEOUT_SECONDS,
        )
        status = self._order_status(order_status)
        filled_qty = _strict_integral_qty(
            order_status.get("filled_qty"), allow_zero=True
        )
        entry_validation_error = self._fill_validation_error(
            order_status,
            order_id=entry_order_id,
            symbol=signal.symbol,
            side="buy",
            qty=requested_shares,
            allowed_statuses={"filled"},
        )

        if entry_validation_error or filled_qty != requested_shares:
            log.critical(
                "portfolio.entry_fill_unconfirmed",
                symbol=signal.symbol,
                order_id=entry_order_id,
                status=status,
                requested=requested_shares,
                filled=filled_qty,
                validation_error=entry_validation_error,
                detail="Cancelling the parent and flattening any partial fill",
            )
            self._cleanup_failed_entry(signal.symbol, entry_order_id)
            self._raise_safety_error(
                f"entry_fill_unconfirmed:{signal.symbol}: "
                f"status={status!r}, filled_qty={filled_qty!r}"
            )

        actual_entry_price = float(order_status["filled_avg_price"])
        stop_order_id, tp_order_id = self._wait_for_bracket_legs(
            entry_order_id,
            expected_qty=filled_qty,
            expected_symbol=signal.symbol,
            expected_stop_price=signal.stop_price,
            expected_tp_price=tp_price,
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
            self._raise_safety_error(
                f"entry_protection_unconfirmed:{signal.symbol}: "
                f"stop={stop_order_id!r}, target={tp_order_id!r}"
            )

        broker_qty = self._broker_position_qty(signal.symbol)
        if broker_qty != filled_qty:
            log.critical(
                "portfolio.entry_position_unconfirmed",
                symbol=signal.symbol,
                order_id=entry_order_id,
                filled_qty=filled_qty,
                broker_qty=broker_qty,
                detail="Cancelling all exits and flattening before local adoption",
            )
            self._cleanup_failed_entry(signal.symbol, entry_order_id)
            self._raise_safety_error(
                f"entry_position_unconfirmed:{signal.symbol}: "
                f"broker_qty={broker_qty!r}, filled_qty={filled_qty!r}"
            )

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

            # A stored order ID is not proof of protection.  Re-read the stop
            # on every tick and fail closed before price/strategy work if the
            # exact downside exit is not still active at the broker.
            self._require_live_stop(position, context="position_update")

            # 2. Update current price
            raw_price = market_data.get_current_price(symbol)
            if raw_price is None:
                log.warning("portfolio.price_unavailable", symbol=symbol)
                continue
            try:
                if isinstance(raw_price, bool):
                    raise ValueError("boolean price is invalid")
                price = float(raw_price)
            except (TypeError, ValueError, OverflowError) as exc:
                self._fail_closed_for_stop(
                    position,
                    "market_price_invalid",
                    f"price={raw_price!r}: {exc}",
                )
            if not math.isfinite(price) or price <= 0:
                self._fail_closed_for_stop(
                    position,
                    "market_price_invalid",
                    f"price={price!r}",
                )

            position.current_price = price

            # Track high-water mark for smarter trailing stops
            if position.high_water_mark is None or price > position.high_water_mark:
                position.high_water_mark = price

            # Feed price to paper broker so bracket stops/TPs trigger
            if hasattr(self._broker, "update_price"):
                self._broker.update_price(symbol, price)

                # The in-memory paper broker can fill an OCO leg synchronously
                # inside update_price().  Reconcile that broker transition
                # before any strategy decision or trailing-stop replacement;
                # otherwise a filled leg could be replaced with an orphan exit
                # while the broker is already flat.
                broker_closed = self._check_bracket_fills(position)
                if broker_closed:
                    journal_entries.append(broker_closed)
                    symbols_to_remove.append(symbol)
                    continue

                # A partial or otherwise non-closing synchronous transition may
                # also have changed the protective order state.  Prove downside
                # protection again before continuing the tick.
                self._require_live_stop(
                    position, context="post_price_update"
                )
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
                old_stop = position.trailing_stop_price

                # Replace the broker-side stop order at the new price
                if position.broker_stop_order_id:
                    replacement_qty = _strict_integral_qty(
                        position.shares_remaining
                    )
                    if replacement_qty is None:
                        self._fail_closed_for_stop(
                            position,
                            "trailing_stop_quantity_invalid",
                            f"qty={position.shares_remaining!r}",
                        )
                    try:
                        new_stop_id = self._broker.replace_stop_order(
                            order_id=position.broker_stop_order_id,
                            qty=replacement_qty,
                            new_stop_price=new_stop,
                        )
                        self._require_live_stop(
                            position,
                            order_id=new_stop_id,
                            expected_stop=new_stop,
                            context="trailing_stop_replacement",
                        )
                        # Assignment happens only after a fresh exact read of
                        # the replacement ID.  The previous local stop remains
                        # authoritative until this point.
                        position.broker_stop_order_id = new_stop_id
                        position.trailing_stop_active = True
                        position.trailing_stop_price = new_stop
                        log.info(
                            "portfolio.broker_stop_updated",
                            symbol=symbol,
                            old_stop=round(old_stop or 0, 4),
                            new_stop=round(new_stop, 4),
                        )
                    except Exception as e:
                        if isinstance(e, PortfolioSafetyError):
                            raise
                        self._fail_closed_for_stop(
                            position,
                            "trailing_stop_replacement",
                            f"replacement request failed: {e}",
                        )
                else:
                    self._fail_closed_for_stop(
                        position,
                        "trailing_stop_replacement",
                        "no tracked stop order",
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

    def finalize_verified_broker_flat(
        self, reason: str = "broker_wide_flatten"
    ) -> list[JournalEntry]:
        """Journal local positions after an account-wide flatten was proven.

        Runtime shutdown, circuit, and hard-time cleanup use the broker's
        account-wide cancel/flatten endpoint first so they cannot spend a
        container grace period polling individual exits.  This method is the
        corresponding bookkeeping-only path: it rechecks that the broker is
        flat, submits no orders, and closes every remaining internal position
        only from the broker-confirmed fills produced by that flatten.
        """
        try:
            remaining = self._broker.get_positions()
        except Exception as exc:
            self._raise_safety_error(
                f"{reason}: verified-flat bookkeeping lookup failed: {exc}"
            )
        if remaining:
            self._raise_safety_error(
                f"{reason}: bookkeeping requires broker-flat state, found "
                f"{[(p.get('symbol'), p.get('qty')) for p in remaining]}"
            )

        try:
            fills = self._broker.get_last_close_fills()
        except Exception as exc:
            self._raise_safety_error(
                f"{reason}: close-fill lookup failed: {exc}"
            )
        fills_by_symbol: dict[str, list[dict]] = {}
        seen_fill_ids: set[str] = set()
        for fill in fills:
            if not isinstance(fill, dict):
                self._raise_safety_error(
                    f"{reason}: invalid close fill payload: {fill!r}"
                )
            fill_id = str(fill.get("id", "") or "").strip()
            if not fill_id:
                self._raise_safety_error(
                    f"{reason}: close fill is missing an id"
                )
            if fill_id in seen_fill_ids:
                self._raise_safety_error(
                    f"{reason}: duplicate close fill id: {fill_id!r}"
                )
            seen_fill_ids.add(fill_id)
            symbol = str(fill.get("symbol", "") or "").upper()
            fills_by_symbol.setdefault(symbol, []).append(fill)

        # Validate the complete accounting set before mutating any position.
        validated: list[tuple[str, PositionInfo, float]] = []
        for symbol, position in self._positions.items():
            matching = fills_by_symbol.get(symbol.upper(), [])
            if len(matching) != 1:
                self._raise_safety_error(
                    f"{reason}: expected one exact close fill for {symbol}, "
                    f"found {len(matching)}"
                )
            fill = matching[0]
            expected_qty = _strict_integral_qty(position.shares_remaining)
            requested_qty = _strict_integral_qty(fill.get("qty"))
            fill_qty = _strict_integral_qty(fill.get("filled_qty"))
            try:
                fill_price = float(fill.get("filled_avg_price", 0.0) or 0.0)
            except (TypeError, ValueError) as exc:
                self._raise_safety_error(
                    f"{reason}: invalid close fill for {symbol}: {exc}"
                )
            status = self._order_status(fill)
            side = self._normalized_enum(fill.get("side", ""))
            fill_id = str(fill.get("id", "") or "").strip()
            if (
                expected_qty is None
                or not fill_id
                or status != "filled"
                or side != "sell"
                or requested_qty != expected_qty
                or fill_qty != expected_qty
                or not math.isfinite(fill_price)
                or fill_price <= 0
            ):
                self._raise_safety_error(
                    f"{reason}: unverified close fill for {symbol}: "
                    f"id={fill_id!r}, status={status!r}, side={side!r}, "
                    f"qty={requested_qty!r}, "
                    f"filled_qty={fill_qty!r}, "
                    f"expected_qty={position.shares_remaining!r}, "
                    f"filled_avg_price={fill_price!r}"
                )
            validated.append((symbol, position, fill_price))

        if len(validated) != len(fills):
            self._raise_safety_error(
                f"{reason}: unmatched broker close fills: "
                f"expected={len(validated)}, observed={len(fills)}"
            )

        entries: list[JournalEntry] = []
        for symbol, position, fill_price in validated:
            entry = self._finalize_close(
                position,
                fill_price,
                position.shares_remaining,
                reason,
            )
            position.broker_stop_order_id = None
            position.broker_tp_order_id = None
            self._positions.pop(symbol, None)
            self._journal_entries.append(entry)
            entries.append(entry)

        if entries:
            log.info(
                "portfolio.verified_flat_bookkeeping_complete",
                reason=reason,
                count=len(entries),
                total_pnl=format_currency(sum(entry.pnl for entry in entries)),
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

    def _raise_safety_error(self, reason: str) -> None:
        """Stop the operation when broker safety cannot be proven."""
        log.critical("portfolio.safety_error", reason=reason)
        raise PortfolioSafetyError(reason)

    def _close_all_and_verify(self, context: str) -> None:
        """Cancel every broker order, flatten every signed position, and verify."""
        try:
            close_requested = self._broker.close_all_positions()
        except Exception as exc:
            self._raise_safety_error(
                f"{context}: close_all_positions failed: {exc}"
            )
        if not close_requested:
            self._raise_safety_error(
                f"{context}: broker did not confirm close_all_positions"
            )
        deadline = time.monotonic() + _ORDER_FILL_TIMEOUT_SECONDS
        while True:
            try:
                remaining = self._broker.get_positions()
            except Exception as exc:
                self._raise_safety_error(
                    f"{context}: post-close position verification failed: {exc}"
                )
            if not remaining:
                log.warning("portfolio.close_all_verified", context=context)
                return
            if time.monotonic() >= deadline:
                self._raise_safety_error(
                    f"{context}: broker positions remain after close-all: "
                    f"{[(p.get('symbol'), p.get('qty')) for p in remaining]}"
                )
            time.sleep(_ORDER_POLL_INTERVAL_SECONDS)

    def _mark_all_positions_closed(
        self, extra_position: Optional[PositionInfo] = None
    ) -> None:
        """Mirror a verified broker-wide flatten in the in-memory state."""
        positions = list(self._positions.values())
        if extra_position is not None and all(
            tracked is not extra_position for tracked in positions
        ):
            positions.append(extra_position)
        for position in positions:
            position.status = PositionStatus.CLOSED
            position.shares_remaining = 0
            position.broker_stop_order_id = None
            position.broker_tp_order_id = None
        self._positions.clear()

    @staticmethod
    def _order_status(order: dict) -> str:
        """Normalize broker status strings at the portfolio boundary."""
        status = str(order.get("status", "") or "").strip().lower()
        if "." in status:
            status = status.rsplit(".", 1)[-1]
        return status

    @classmethod
    def _fill_validation_error(
        cls,
        order: dict,
        *,
        order_id: str,
        symbol: str,
        side: str,
        qty: int,
        allowed_statuses: set[str],
    ) -> str:
        """Return why an order snapshot cannot support exact fill accounting."""
        if not isinstance(order, dict):
            return "broker returned no order snapshot"
        errors: list[str] = []
        reported_id = str(order.get("id", "") or "")
        reported_symbol = str(order.get("symbol", "") or "").upper()
        reported_side = cls._normalized_enum(order.get("side", ""))
        status = cls._order_status(order)
        expected_qty = _strict_integral_qty(qty)
        reported_qty = _strict_integral_qty(order.get("qty"))
        filled_qty = _strict_integral_qty(
            order.get("filled_qty"), allow_zero=True
        )
        try:
            fill_price = float(order.get("filled_avg_price", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            return f"invalid numeric fill fields: {exc}"
        if expected_qty is None:
            errors.append(f"expected_qty={qty!r}")
        if reported_qty is None:
            errors.append(f"qty={order.get('qty')!r}")
        if filled_qty is None:
            errors.append(f"filled_qty={order.get('filled_qty')!r}")
        if reported_id != order_id:
            errors.append(f"id={reported_id!r}")
        if reported_symbol != symbol.upper():
            errors.append(f"symbol={reported_symbol!r}")
        if reported_side != side:
            errors.append(f"side={reported_side!r}")
        if reported_qty is not None and reported_qty != expected_qty:
            errors.append(f"qty={reported_qty!r}")
        if status not in allowed_statuses:
            errors.append(f"status={status!r}")
        if (
            filled_qty is not None
            and expected_qty is not None
            and filled_qty > expected_qty
        ):
            errors.append(f"filled_qty={filled_qty!r}")
        if filled_qty is not None and filled_qty > 0 and (
            not math.isfinite(fill_price) or fill_price <= 0
        ):
            errors.append(f"filled_avg_price={fill_price!r}")
        return ", ".join(errors)

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
    def _leg_ids(
        order_status: dict,
        expected_qty: Optional[int] = None,
        expected_symbol: Optional[str] = None,
        expected_stop_price: Optional[float] = None,
        expected_tp_price: Optional[float] = None,
    ) -> tuple[str, str]:
        """Return one exact, active stop/target pair from one parent snapshot."""
        if expected_qty is not None:
            expected_qty = _strict_integral_qty(expected_qty)
            if expected_qty is None:
                return "", ""
        stop_ids: list[str] = []
        tp_ids: list[str] = []
        for leg in order_status.get("legs", []) or []:
            status = str(leg.get("status", "") or "").strip().lower()
            if "." in status:
                status = status.rsplit(".", 1)[-1]

            side = str(leg.get("side", "") or "").strip().lower()
            if "." in side:
                side = side.rsplit(".", 1)[-1]
            if side != "sell":
                continue
            if expected_symbol is not None and (
                str(leg.get("symbol", "") or "").upper()
                != expected_symbol.upper()
            ):
                continue
            leg_qty = _strict_integral_qty(leg.get("qty"))
            if leg_qty is None:
                continue
            if expected_qty is not None and leg_qty != expected_qty:
                continue

            leg_id = str(leg.get("id", "") or "")
            if not leg_id:
                continue
            leg_type = str(leg.get("type", "") or "").lower()
            if "." in leg_type:
                leg_type = leg_type.rsplit(".", 1)[-1]
            try:
                stop_price = float(leg.get("stop_price", 0.0) or 0.0)
                limit_price = float(leg.get("limit_price", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(stop_price) or not math.isfinite(limit_price):
                continue
            if leg_type in _STOP_ORDER_TYPES:
                if status not in _ACTIVE_STOP_STATUSES:
                    continue
                if (
                    expected_stop_price is not None
                    and round(stop_price, 2)
                    != round(float(expected_stop_price), 2)
                ):
                    continue
                stop_ids.append(leg_id)
            elif leg_type == "limit":
                # Alpaca's normal activated bracket is a NEW take-profit with
                # a NEW or HELD stop.  A held target (including both legs held)
                # is not a proven executable OCO pair.
                if status != "new":
                    continue
                if (
                    expected_tp_price is not None
                    and round(limit_price, 2)
                    != round(float(expected_tp_price), 2)
                ):
                    continue
                tp_ids.append(leg_id)
        if len(stop_ids) != 1 or len(tp_ids) != 1:
            return "", ""
        if stop_ids[0] == tp_ids[0]:
            return "", ""
        return stop_ids[0], tp_ids[0]

    def _wait_for_bracket_legs(
        self,
        entry_order_id: str,
        expected_qty: Optional[int] = None,
        expected_symbol: Optional[str] = None,
        expected_stop_price: Optional[float] = None,
        expected_tp_price: Optional[float] = None,
        initial_stop_id: str = "",
        initial_tp_id: str = "",
    ) -> tuple[str, str]:
        """Retrieve a verified bracket pair from a fresh nested parent read.

        IDs returned by order submission are hints only. They cannot prove the
        child orders are active, so every attempt evaluates a complete pair
        from one freshly fetched nested parent snapshot.
        """
        del initial_stop_id, initial_tp_id
        if expected_qty is not None:
            expected_qty = _strict_integral_qty(expected_qty)
            if expected_qty is None:
                return "", ""
        deadline = time.monotonic() + _BRACKET_LEG_TIMEOUT_SECONDS
        while True:
            try:
                parent = self._broker.get_order_status(entry_order_id)
            except Exception:
                parent = {}
            stop_id, tp_id = ("", "")
            parent_is_exact = self._order_status(parent) == "filled"
            if expected_symbol is not None and expected_qty is not None:
                parent_is_exact = not self._fill_validation_error(
                    parent,
                    order_id=entry_order_id,
                    symbol=expected_symbol,
                    side="buy",
                    qty=expected_qty,
                    allowed_statuses={"filled"},
                ) and _strict_integral_qty(
                    parent.get("filled_qty"), allow_zero=True
                ) == expected_qty
            if parent_is_exact:
                stop_id, tp_id = self._leg_ids(
                    parent,
                    expected_qty,
                    expected_symbol,
                    expected_stop_price,
                    expected_tp_price,
                )
            if stop_id and tp_id:
                return stop_id, tp_id
            if time.monotonic() >= deadline:
                return "", ""
            time.sleep(_ORDER_POLL_INTERVAL_SECONDS)

    def _broker_position_qty(self, symbol: str) -> Optional[int]:
        """Return the broker's signed quantity, or None if it cannot be proven."""
        try:
            for position in self._broker.get_positions():
                if position.get("symbol") == symbol:
                    return _strict_integral_qty(
                        position.get("qty"), allow_signed=True
                    )
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
        """Best-effort cancel the parent, then globally flatten and verify."""
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

        self._close_all_and_verify(f"failed_entry_cleanup:{symbol}")
        self._mark_all_positions_closed()

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
                order = self._broker.get_order_status(order_id)
                order_status = self._order_status(order)
                if order_status not in ("filled", "partially_filled"):
                    continue
                previous_qty = position.broker_filled_qty.get(order_id, 0)
                expected_order_qty = previous_qty + position.shares_remaining
                validation_error = self._fill_validation_error(
                    order,
                    order_id=order_id,
                    symbol=position.symbol,
                    side="sell",
                    qty=expected_order_qty,
                    allowed_statuses={"filled", "partially_filled"},
                )
                order_type = self._normalized_enum(order.get("type", ""))
                expected_types = (
                    _STOP_ORDER_TYPES
                    if leg_type == "broker_stop"
                    else {"limit"}
                )
                if order_type not in expected_types:
                    validation_error = ", ".join(
                        part
                        for part in (
                            validation_error,
                            f"type={order_type!r}",
                        )
                        if part
                    )
                if validation_error:
                    self._fail_closed_for_stop(
                        position,
                        "bracket_fill_invalid",
                        validation_error,
                    )

                if order_status == "partially_filled":
                    self._fail_closed_for_stop(
                        position,
                        "bracket_partial_fill_untrackable",
                        (
                            "protective leg partially filled; exact remaining "
                            "OCO protection cannot be proven atomically"
                        ),
                    )

                cumulative_qty = _strict_integral_qty(
                    order.get("filled_qty"), allow_zero=True
                )
                if cumulative_qty is None:
                    self._fail_closed_for_stop(
                        position,
                        "bracket_fill_qty_invalid",
                        f"filled_qty={order.get('filled_qty')!r}",
                    )
                fill_price = float(order["filled_avg_price"])
                if cumulative_qty < previous_qty:
                    self._fail_closed_for_stop(
                        position,
                        "bracket_fill_cumulative_regressed",
                        (
                            f"reported={cumulative_qty}, "
                            f"previous={previous_qty}"
                        ),
                    )
                reported_new_qty = cumulative_qty - previous_qty
                if (
                    reported_new_qty <= 0
                    or reported_new_qty > position.shares_remaining
                    or (
                        order_status == "filled"
                        and cumulative_qty != expected_order_qty
                    )
                    or (
                        order_status == "partially_filled"
                        and cumulative_qty >= expected_order_qty
                    )
                ):
                    self._fail_closed_for_stop(
                        position,
                        "bracket_fill_qty_divergence",
                        (
                            f"status={order_status!r}, "
                            f"cumulative={cumulative_qty}, "
                            f"previous={previous_qty}, "
                            f"remaining={position.shares_remaining}"
                        ),
                    )

                previous_notional = position.broker_filled_notional.get(
                    order_id, 0.0
                )
                if not math.isfinite(previous_notional) or previous_notional < 0:
                    self._fail_closed_for_stop(
                        position,
                        "bracket_fill_notional_invalid",
                        f"previous_notional={previous_notional!r}",
                    )
                cumulative_notional = cumulative_qty * fill_price
                reported_new_notional = cumulative_notional - previous_notional
                incremental_fill_price = (
                    reported_new_notional / reported_new_qty
                )
                if (
                    reported_new_notional <= 0
                    or not math.isfinite(incremental_fill_price)
                    or incremental_fill_price <= 0
                ):
                    self._fail_closed_for_stop(
                        position,
                        "bracket_fill_notional_invalid",
                        (
                            f"new_notional={reported_new_notional!r}, "
                            f"incremental_price={incremental_fill_price!r}"
                        ),
                    )

                expected_remaining = (
                    position.shares_remaining - reported_new_qty
                )
                broker_remaining = self._broker_position_qty(position.symbol)
                if broker_remaining != expected_remaining:
                    self._fail_closed_for_stop(
                        position,
                        "bracket_fill_broker_qty_divergence",
                        (
                            f"broker_qty={broker_remaining!r}, "
                            f"expected_qty={expected_remaining}"
                        ),
                    )

                position.broker_filled_qty[order_id] = cumulative_qty
                position.broker_filled_notional[order_id] = cumulative_notional

                log.info(
                    "portfolio.bracket_filled",
                    symbol=position.symbol,
                    leg=leg_type,
                    fill_price=incremental_fill_price,
                    filled_qty=reported_new_qty,
                    cumulative_qty=cumulative_qty,
                    partial=(expected_remaining > 0),
                )

                if expected_remaining > 0:
                    position.pnl_realized += reported_new_qty * (
                        incremental_fill_price - position.entry_price
                    )
                    position.shares_remaining = expected_remaining
                    position.status = PositionStatus.PARTIALLY_CLOSED
                    log.warning(
                        "portfolio.bracket_partial_fill",
                        symbol=position.symbol,
                        filled=reported_new_qty,
                        remaining=position.shares_remaining,
                    )
                    return None
                # A filled protective leg does not prove its OCO sibling is
                # inactive.  Confirm both tracked exits are terminal before
                # removing local state; an armed sibling could otherwise sell
                # a now-flat account and create an unintended short.
                if not self._cancel_bracket_legs(
                    position, accounted_filled_order_id=order_id
                ):
                    self._fail_closed_for_stop(
                        position,
                        "bracket_sibling_cancel_unconfirmed",
                        "protective fill completed but an OCO leg remains live",
                    )
                return self._finalize_close(
                    position,
                    incremental_fill_price,
                    reported_new_qty,
                    leg_type,
                )
            except PortfolioSafetyError:
                raise
            except Exception as exc:
                log.warning(
                    "portfolio.bracket_check_error",
                    symbol=position.symbol,
                    order_id=order_id,
                    error=str(exc),
                )
        return None

    def _cancel_bracket_legs(
        self,
        position: PositionInfo,
        accounted_filled_order_id: Optional[str] = None,
    ) -> bool:
        """Cancel every tracked leg and require terminal confirmation."""
        all_canceled = True
        for attr in ("broker_stop_order_id", "broker_tp_order_id"):
            order_id = getattr(position, attr)
            if not order_id:
                continue
            try:
                self._broker.cancel_order(order_id)
            except Exception as exc:
                log.warning(
                    "portfolio.cancel_bracket_error",
                    order_id=order_id,
                    error=str(exc),
                )
            try:
                snapshot = self._broker.get_order_status(order_id)
                status = self._order_status(snapshot)
            except Exception:
                snapshot = {}
                status = "error"
            expected_types = (
                _STOP_ORDER_TYPES
                if attr == "broker_stop_order_id"
                else {"limit"}
            )
            reported_qty = (
                _strict_integral_qty(snapshot.get("qty"))
                if isinstance(snapshot, dict)
                else None
            )
            expected_qty = _strict_integral_qty(position.shares_remaining)
            exact_identity = (
                isinstance(snapshot, dict)
                and str(snapshot.get("id", "") or "") == order_id
                and str(snapshot.get("symbol", "") or "").upper()
                == position.symbol.upper()
                and self._normalized_enum(snapshot.get("side", "")) == "sell"
                and expected_qty is not None
                and reported_qty == expected_qty
                and self._normalized_enum(snapshot.get("type", ""))
                in expected_types
            )
            canceled = exact_identity and (
                status in _CONFIRMED_INACTIVE_ORDER_STATUSES
                or (
                    order_id == accounted_filled_order_id
                    and status == "filled"
                )
            )
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

    def _restore_stop(self, position: PositionInfo, qty: int) -> bool:
        """Submit and freshly verify protection, or flatten and hard-stop."""
        parsed_qty = _strict_integral_qty(qty, allow_zero=True)
        if parsed_qty == 0:
            return True
        if parsed_qty is None:
            self._close_all_and_verify(
                f"stop_restore_invalid_qty:{position.symbol}"
            )
            self._mark_all_positions_closed(position)
            self._raise_safety_error(
                f"stop_restore_invalid_qty:{position.symbol}: qty={qty!r}"
            )
        qty = parsed_qty
        effective_stop = position.trailing_stop_price or position.stop_price
        order_id = position.broker_stop_order_id
        try:
            if not order_id:
                order_id = self._broker.submit_stop_order(
                    symbol=position.symbol,
                    qty=qty,
                    stop_price=effective_stop,
                )
            order = self._broker.get_order_status(order_id)
            validation_error = self._stop_validation_error(
                order,
                order_id=order_id,
                symbol=position.symbol,
                qty=qty,
                expected_stop=effective_stop,
            )
            if not validation_error and self._order_status(order) != "new":
                validation_error = (
                    "standalone restored stop is not independently active: "
                    f"status={self._order_status(order)!r}"
                )
            if validation_error:
                raise PortfolioSafetyError(
                    f"replacement stop verification failed: {validation_error}"
                )
            position.broker_stop_order_id = order_id
            log.warning(
                "portfolio.emergency_stop_restored",
                symbol=position.symbol,
                qty=qty,
                stop_price=effective_stop,
            )
            return True
        except Exception as exc:
            log.critical(
                "portfolio.emergency_stop_restore_failed",
                symbol=position.symbol,
                qty=qty,
                error=str(exc),
            )
            position.broker_stop_order_id = None
            self._close_all_and_verify(
                f"stop_restore_failed:{position.symbol}"
            )
            self._mark_all_positions_closed(position)
            self._raise_safety_error(
                f"stop_restore_failed:{position.symbol}: {exc}"
            )

    @staticmethod
    def _normalized_enum(value: object) -> str:
        normalized = str(value or "").strip().lower()
        if "." in normalized:
            normalized = normalized.rsplit(".", 1)[-1]
        return normalized

    @classmethod
    def _stop_validation_error(
        cls,
        order: dict,
        *,
        order_id: str,
        symbol: str,
        qty: int,
        expected_stop: float,
    ) -> str:
        """Return why a fresh broker snapshot is not exact protection."""
        if not order_id:
            return "missing stop order id"
        if not isinstance(order, dict):
            return "broker returned no stop snapshot"

        reported_id = str(order.get("id", "") or "")
        status = cls._order_status(order)
        side = cls._normalized_enum(order.get("side", ""))
        order_type = cls._normalized_enum(order.get("type", ""))
        reported_symbol = str(order.get("symbol", "") or "").upper()
        expected_qty = _strict_integral_qty(qty)
        reported_qty = _strict_integral_qty(order.get("qty"))
        try:
            reported_stop = float(order.get("stop_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            reported_stop = 0.0

        errors = []
        if reported_id != order_id:
            errors.append(f"id={reported_id!r}")
        if status not in _ACTIVE_STOP_STATUSES:
            errors.append(f"status={status!r}")
        if side != "sell":
            errors.append(f"side={side!r}")
        if order_type not in _STOP_ORDER_TYPES:
            errors.append(f"type={order_type!r}")
        if reported_symbol != symbol.upper():
            errors.append(f"symbol={reported_symbol!r}")
        if expected_qty is None:
            errors.append(f"expected_qty={qty!r}")
        if reported_qty != expected_qty:
            errors.append(f"qty={reported_qty!r}")
        # Broker stop submissions are rounded to cents.  Compare the
        # effective executable price at that same precision.
        if round(reported_stop, 2) != round(float(expected_stop), 2):
            errors.append(f"stop_price={reported_stop!r}")
        return ", ".join(errors)

    def _fail_closed_for_stop(
        self, position: PositionInfo, context: str, detail: str
    ) -> None:
        """Globally flatten, mirror the verified state, then hard-stop."""
        reason = f"{context}:{position.symbol}: {detail}"
        log.critical(
            "portfolio.stop_protection_lost",
            symbol=position.symbol,
            context=context,
            detail=detail,
        )
        self._close_all_and_verify(reason)
        self._mark_all_positions_closed(position)
        self._raise_safety_error(reason)

    @classmethod
    def _tp_validation_error(
        cls,
        order: dict,
        *,
        order_id: str,
        symbol: str,
        qty: int,
        expected_limit: Optional[float],
    ) -> str:
        """Validate the active OCO target that makes a HELD stop protective."""
        if not order_id:
            return "held stop has no tracked take-profit order id"
        if not isinstance(order, dict):
            return "broker returned no take-profit snapshot"
        reported_id = str(order.get("id", "") or "")
        status = cls._order_status(order)
        side = cls._normalized_enum(order.get("side", ""))
        order_type = cls._normalized_enum(order.get("type", ""))
        reported_symbol = str(order.get("symbol", "") or "").upper()
        expected_qty = _strict_integral_qty(qty)
        reported_qty = _strict_integral_qty(order.get("qty"))
        try:
            reported_limit = float(order.get("limit_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            reported_limit = 0.0

        errors = []
        if reported_id != order_id:
            errors.append(f"tp_id={reported_id!r}")
        if status != "new":
            errors.append(f"tp_status={status!r}")
        if side != "sell":
            errors.append(f"tp_side={side!r}")
        if order_type != "limit":
            errors.append(f"tp_type={order_type!r}")
        if reported_symbol != symbol.upper():
            errors.append(f"tp_symbol={reported_symbol!r}")
        if expected_qty is None:
            errors.append(f"expected_tp_qty={qty!r}")
        if reported_qty != expected_qty:
            errors.append(f"tp_qty={reported_qty!r}")
        if not math.isfinite(reported_limit) or reported_limit <= 0:
            errors.append(f"tp_limit_price={reported_limit!r}")
        elif expected_limit is not None and round(reported_limit, 2) != round(
            float(expected_limit), 2
        ):
            errors.append(f"tp_limit_price={reported_limit!r}")
        return ", ".join(errors)

    def _require_live_stop(
        self,
        position: PositionInfo,
        *,
        order_id: Optional[str] = None,
        expected_stop: Optional[float] = None,
        context: str,
    ) -> dict:
        """Freshly prove the exact tracked stop, or flatten and halt."""
        checked_id = order_id or position.broker_stop_order_id
        if not checked_id:
            self._fail_closed_for_stop(
                position, context, "missing tracked stop order id"
            )
        try:
            order = self._broker.get_order_status(checked_id)
        except Exception as exc:
            self._fail_closed_for_stop(
                position, context, f"stop status lookup failed: {exc}"
            )
        effective_stop = (
            expected_stop
            if expected_stop is not None
            else (position.trailing_stop_price or position.stop_price)
        )
        validation_error = self._stop_validation_error(
            order,
            order_id=checked_id,
            symbol=position.symbol,
            qty=position.shares_remaining,
            expected_stop=effective_stop,
        )
        if validation_error:
            self._fail_closed_for_stop(position, context, validation_error)
        if self._order_status(order) == "held":
            tp_order_id = position.broker_tp_order_id
            if not tp_order_id:
                self._fail_closed_for_stop(
                    position,
                    context,
                    "held stop has no tracked active OCO take-profit",
                )
            try:
                tp_order = self._broker.get_order_status(tp_order_id)
            except Exception as exc:
                self._fail_closed_for_stop(
                    position,
                    context,
                    f"held-stop take-profit lookup failed: {exc}",
                )
            expected_limit = (
                position.target_prices[0] if position.target_prices else None
            )
            tp_error = self._tp_validation_error(
                tp_order,
                order_id=tp_order_id,
                symbol=position.symbol,
                qty=position.shares_remaining,
                expected_limit=expected_limit,
            )
            if tp_error:
                self._fail_closed_for_stop(position, context, tp_error)
        return order

    def _finalize_close(
        self,
        position: PositionInfo,
        actual_exit_price: float,
        actual_shares: int,
        reason: str,
    ) -> JournalEntry:
        prior_realized = position.pnl_realized
        original_shares = _strict_integral_qty(position.shares)
        remaining_shares = _strict_integral_qty(position.shares_remaining)
        closed_shares = _strict_integral_qty(actual_shares)
        if (
            original_shares is None
            or remaining_shares is None
            or closed_shares is None
            or closed_shares != remaining_shares
            or not math.isfinite(actual_exit_price)
            or actual_exit_price <= 0
            or not math.isfinite(position.entry_price)
            or position.entry_price <= 0
            or not math.isfinite(prior_realized)
        ):
            self._raise_safety_error(
                f"close_accounting_invalid:{position.symbol}: "
                f"shares={position.shares!r}, remaining="
                f"{position.shares_remaining!r}, closed={actual_shares!r}, "
                f"entry={position.entry_price!r}, "
                f"exit={actual_exit_price!r}, "
                f"prior_pnl={prior_realized!r}"
            )
        close_pnl = closed_shares * (
            actual_exit_price - position.entry_price
        )
        total_pnl = prior_realized + close_pnl
        if not math.isfinite(total_pnl):
            self._raise_safety_error(
                f"close_accounting_invalid:{position.symbol}: "
                f"pnl={total_pnl!r}"
            )
        # A journal row represents the entire original position.  When prior
        # tranches were closed, reporting the final tranche price beside the
        # original share count makes the row internally contradictory.  The
        # weighted realized exit is the unique price that reconciles the row's
        # entry, total shares, and total realized P&L.
        weighted_exit_price = (
            position.entry_price + total_pnl / original_shares
        )
        position.pnl_realized = total_pnl
        # Prior partial tranches were booked when they filled. Add only the
        # final tranche now so daily P&L is neither delayed nor doubled.
        self._daily_pnl += close_pnl
        if self._circuit:
            if prior_realized:
                self._circuit.record_trade_result(
                    total_pnl,
                    realized_already_recorded=prior_realized,
                    defer_check=True,
                )
            else:
                self._circuit.record_trade_result(
                    total_pnl, defer_check=True
                )

        risk_per_share = abs(position.entry_price - position.stop_price)
        rr_ratio = (
            (weighted_exit_price - position.entry_price) / risk_per_share
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
            exit_price=weighted_exit_price,
            shares=original_shares,
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
        if broker_qty == 0:
            # A protective leg or external action won the race.  Never send a
            # sell into an already-flat account.  If neither tracked leg proves
            # the close, globally cancel any orphan exits and hard-stop because
            # accounting and protection state are no longer trustworthy.
            bracket_entry = self._check_bracket_fills(position)
            if bracket_entry is not None:
                return bracket_entry
            self._fail_closed_for_stop(
                position,
                "close_qty_divergence",
                (
                    "broker is flat without a confirmed protective fill; "
                    f"internal_qty={position.shares_remaining}"
                ),
            )
        if broker_qty != position.shares_remaining:
            # Any signed or positive quantity mismatch makes the tracked exits
            # wrong-sized.  Never adopt it or attempt a discretionary close
            # against an ambiguous broker state.
            self._fail_closed_for_stop(
                position,
                "close_qty_divergence",
                (
                    f"broker_qty={broker_qty}, "
                    f"internal_qty={position.shares_remaining}"
                ),
            )

        if not self._cancel_bracket_legs(position):
            bracket_entry = self._check_bracket_fills(position)
            if bracket_entry is not None:
                return bracket_entry
            self._fail_closed_for_stop(
                position,
                "close_exit_cancel_unconfirmed",
                "one or more protective exits could not be cancelled",
            )

        # Re-read after cancellation because a leg may have filled while the
        # cancellation was in flight.
        broker_qty = self._broker_position_qty(position.symbol)
        if broker_qty is None:
            self._fail_closed_for_stop(
                position,
                "close_post_cancel_qty_unknown",
                "broker quantity lookup failed after exit cancellation",
            )
        if broker_qty == 0:
            bracket_entry = self._check_bracket_fills(position)
            if bracket_entry is not None:
                return bracket_entry
        if broker_qty != position.shares_remaining:
            self._fail_closed_for_stop(
                position,
                "close_post_cancel_qty_divergence",
                (
                    f"broker_qty={broker_qty}, "
                    f"internal_qty={position.shares_remaining}"
                ),
            )
        qty_to_sell = _strict_integral_qty(position.shares_remaining)
        if qty_to_sell is None:
            self._fail_closed_for_stop(
                position,
                "close_quantity_invalid",
                f"internal_qty={position.shares_remaining!r}",
            )

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
        validation_error = self._fill_validation_error(
            order_status,
            order_id=order_id,
            symbol=position.symbol,
            side="sell",
            qty=qty_to_sell,
            allowed_statuses=_OBSERVABLE_ORDER_STATUSES,
        )
        if validation_error:
            self._fail_closed_for_stop(
                position,
                "close_sell_snapshot_invalid",
                validation_error,
            )
        filled_qty = _strict_integral_qty(
            order_status.get("filled_qty"), allow_zero=True
        )
        if filled_qty is None:
            self._fail_closed_for_stop(
                position,
                "close_sell_snapshot_invalid",
                f"filled_qty={order_status.get('filled_qty')!r}",
            )
        actual_exit_price = float(
            order_status.get("filled_avg_price", 0.0) or 0.0
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
                validation_error = self._fill_validation_error(
                    order_status,
                    order_id=order_id,
                    symbol=position.symbol,
                    side="sell",
                    qty=qty_to_sell,
                    allowed_statuses=_OBSERVABLE_ORDER_STATUSES,
                )
                if validation_error:
                    self._fail_closed_for_stop(
                        position,
                        "close_sell_snapshot_invalid",
                        validation_error,
                    )
                cancellation_confirmed = cancellation_confirmed or (
                    status in _CONFIRMED_INACTIVE_ORDER_STATUSES
                )
                filled_qty = _strict_integral_qty(
                    order_status.get("filled_qty"), allow_zero=True
                )
                if filled_qty is None:
                    self._fail_closed_for_stop(
                        position,
                        "close_sell_snapshot_invalid",
                        f"filled_qty={order_status.get('filled_qty')!r}",
                    )
                actual_exit_price = float(
                    order_status.get("filled_avg_price", 0.0) or 0.0
                )

            # The cancellation can lose the race to a complete fill.  Treat it
            # as the successful close that it is, rather than as a partial.
            if status == "filled" and filled_qty == qty_to_sell:
                remaining = self._broker_position_qty(position.symbol)
                if remaining != 0:
                    self._fail_closed_for_stop(
                        position,
                        "close_full_fill_broker_qty_divergence",
                        f"broker_qty={remaining!r}, expected_qty=0",
                    )
                return self._finalize_close(
                    position,
                    actual_exit_price,
                    filled_qty,
                    reason,
                )

            if not cancellation_confirmed:
                self._fail_closed_for_stop(
                    position,
                    "close_sell_cancel_unconfirmed",
                    (
                        f"order_id={order_id}, status={status!r}; "
                        "possibly-live sell cannot coexist with a new stop"
                    ),
                )

            if filled_qty > 0:
                expected_remaining = position.shares_remaining - filled_qty
                remaining = self._broker_position_qty(position.symbol)
                if remaining != expected_remaining:
                    self._fail_closed_for_stop(
                        position,
                        "close_partial_fill_qty_divergence",
                        (
                            f"broker_qty={remaining!r}, "
                            f"expected_qty={expected_remaining}"
                        ),
                    )
                partial_pnl = filled_qty * (
                    actual_exit_price - position.entry_price
                )
                if not math.isfinite(partial_pnl):
                    self._fail_closed_for_stop(
                        position,
                        "close_partial_pnl_invalid",
                        f"partial_pnl={partial_pnl!r}",
                    )
                position.pnl_realized += partial_pnl
                self._daily_pnl += partial_pnl
                position.shares_remaining = expected_remaining
                position.status = PositionStatus.PARTIALLY_CLOSED
                position.pnl_unrealized = expected_remaining * (
                    position.current_price - position.entry_price
                )
                if self._circuit:
                    self._circuit.record_partial_realized_pnl(
                        partial_pnl, defer_check=True
                    )
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
            self._fail_closed_for_stop(
                position,
                "close_filled_qty_divergence",
                f"reported_filled={filled_qty}, requested={qty_to_sell}",
            )

        remaining = self._broker_position_qty(position.symbol)
        if remaining != 0:
            self._fail_closed_for_stop(
                position,
                "close_full_fill_broker_qty_divergence",
                f"broker_qty={remaining!r}, expected_qty=0",
            )

        return self._finalize_close(
            position,
            actual_exit_price,
            filled_qty,
            reason,
        )

    def _execute_scale_out(
        self, position: PositionInfo, shares: int, price: float, reason: str
    ) -> None:
        """Defer scale-outs while broker-side protection is tracked.

        Canceling a full-size bracket to submit a discretionary partial exit
        creates an unprotected interval and competing-order recovery problem.
        Until scale-out can be expressed atomically at the broker, leave the
        verified bracket untouched. An open position with no tracked exit is a
        hard safety fault rather than permission to submit a naked sell.
        """
        del shares, price
        if position.shares_remaining <= 0:
            return
        self._require_live_stop(position, context="scale_out")
        log.info(
            "portfolio.scale_out_deferred_protected",
            symbol=position.symbol,
            stop_order_id=position.broker_stop_order_id,
            tp_order_id=position.broker_tp_order_id,
            reason=reason,
        )
        return

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
