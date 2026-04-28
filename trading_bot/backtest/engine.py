"""
Backtesting engine for momentum strategy validation.

Uses yfinance for historical data and pandas-based signal generation.
Simulates the VWAP pullback strategy with proper position sizing,
stop losses, and scale-out exits.

Note: vectorbt is an optional dependency. The engine includes a
pure-pandas fallback for basic backtesting without vectorbt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import structlog
import yfinance as yf

from trading_bot.config.settings import AppConfig
from trading_bot.execution.paper_broker import SlippageModel
from trading_bot.models.domain import OrderSide
from trading_bot.utils.indicators import enrich_dataframe

log = structlog.get_logger(__name__)


@dataclass
class BacktestTrade:
    """Record of a single backtest trade."""

    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    stop_price: float
    shares: int
    pnl: float
    pnl_pct: float
    rr_ratio: float
    exit_reason: str


@dataclass
class BacktestResult:
    """Aggregated backtest results."""

    symbol: str
    total_return_pct: float
    total_pnl: float
    num_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe_ratio: float
    trades: list[BacktestTrade] = field(default_factory=list)


class BacktestEngine:
    """
    Backtesting engine using pandas-based simulation.

    Simulates the VWAP pullback strategy on daily data.
    For intraday simulation, use 1-minute data (limited history via yfinance).
    """

    def __init__(self, config: AppConfig):
        self._config = config
        self._risk_pct = config.risk.risk_per_trade_pct / 100.0
        self._stop_atr_mult = config.risk.stop_loss_atr_multiplier
        self._scale_targets = config.exit.scale_out_rr_targets
        self._scale_ratios = config.exit.scale_out_ratios
        self._starting_capital = config.starting_capital
        self._slippage = SlippageModel(base_bps=5.0, volume_impact_bps=2.0)

    def run(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
    ) -> dict[str, BacktestResult]:
        """
        Run backtest across multiple symbols.

        Args:
            symbols: List of ticker symbols.
            start_date: Start date (YYYY-MM-DD).
            end_date: End date (YYYY-MM-DD).

        Returns:
            Dict mapping symbol to BacktestResult.
        """
        results = {}

        for symbol in symbols:
            log.info("backtest.running", symbol=symbol, start=start_date, end=end_date)

            try:
                result = self._backtest_symbol(symbol, start_date, end_date)
                results[symbol] = result
                log.info(
                    "backtest.complete",
                    symbol=symbol,
                    trades=result.num_trades,
                    return_pct=round(result.total_return_pct, 2),
                    win_rate=round(result.win_rate, 2),
                )
            except Exception as e:
                log.error("backtest.error", symbol=symbol, error=str(e))
                results[symbol] = BacktestResult(
                    symbol=symbol,
                    total_return_pct=0.0,
                    total_pnl=0.0,
                    num_trades=0,
                    win_rate=0.0,
                    avg_win=0.0,
                    avg_loss=0.0,
                    profit_factor=0.0,
                    max_drawdown_pct=0.0,
                    sharpe_ratio=0.0,
                )

        return results

    def _backtest_symbol(
        self, symbol: str, start_date: str, end_date: str
    ) -> BacktestResult:
        """Backtest a single symbol."""
        # Download data
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start_date, end=end_date)

        if df.empty or len(df) < 30:
            log.warning("backtest.insufficient_data", symbol=symbol, rows=len(df))
            return BacktestResult(
                symbol=symbol,
                total_return_pct=0.0,
                total_pnl=0.0,
                num_trades=0,
                win_rate=0.0,
                avg_win=0.0,
                avg_loss=0.0,
                profit_factor=0.0,
                max_drawdown_pct=0.0,
                sharpe_ratio=0.0,
            )

        df.columns = [c.lower() for c in df.columns]

        # Enrich with indicators
        df = enrich_dataframe(df, ema_length=9, atr_length=14, include_vwap=False)

        # Generate signals and simulate
        trades = self._simulate(symbol, df)

        # Compute metrics
        return self._compute_metrics(symbol, trades)

    def _simulate(self, symbol: str, df: pd.DataFrame) -> list[BacktestTrade]:
        """
        Walk-forward simulation on enriched daily bars.

        Entry: pullback to EMA9 with bullish bar and above-avg volume.
        Exit: stop loss (ATR-based) or R:R target reached.
        """
        trades = []
        equity = self._starting_capital
        in_trade = False
        entry_price = 0.0
        stop_price = 0.0
        shares = 0
        entry_date = ""
        avg_volume = float(df["volume"].mean())

        ema_col = "ema_9"
        atr_col = "atr_14"

        for i in range(20, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]

            price = row["close"]
            atr = row.get(atr_col, 0.0)
            ema = row.get(ema_col, 0.0)

            if pd.isna(atr) or pd.isna(ema) or atr <= 0:
                continue

            if in_trade:
                # Check stop loss
                if row["low"] <= stop_price:
                    exit_price = self._slippage.compute_slippage(
                        OrderSide.SELL, stop_price, shares, int(avg_volume)
                    )
                    pnl = shares * (exit_price - entry_price)
                    trades.append(
                        BacktestTrade(
                            symbol=symbol,
                            entry_date=entry_date,
                            exit_date=str(df.index[i].date()),
                            entry_price=entry_price,
                            exit_price=exit_price,
                            stop_price=stop_price,
                            shares=shares,
                            pnl=round(pnl, 2),
                            pnl_pct=round(pnl / (entry_price * shares) * 100, 2),
                            rr_ratio=round(
                                (exit_price - entry_price)
                                / (entry_price - stop_price),
                                2,
                            )
                            if entry_price != stop_price
                            else 0.0,
                            exit_reason="stop_loss",
                        )
                    )
                    equity += pnl
                    in_trade = False
                    continue

                # Check 2:1 R:R target (simplified: exit full at 2R)
                risk = entry_price - stop_price
                target_2r = entry_price + (risk * 2.0)
                if row["high"] >= target_2r:
                    exit_price = self._slippage.compute_slippage(
                        OrderSide.SELL, target_2r, shares, int(avg_volume)
                    )
                    pnl = shares * (exit_price - entry_price)
                    trades.append(
                        BacktestTrade(
                            symbol=symbol,
                            entry_date=entry_date,
                            exit_date=str(df.index[i].date()),
                            entry_price=entry_price,
                            exit_price=exit_price,
                            stop_price=stop_price,
                            shares=shares,
                            pnl=round(pnl, 2),
                            pnl_pct=round(pnl / (entry_price * shares) * 100, 2),
                            rr_ratio=2.0,
                            exit_reason="target_2R",
                        )
                    )
                    equity += pnl
                    in_trade = False
                    continue

            else:
                # Entry logic: EMA pullback with bullish bar
                # Price pulled back near EMA (within 2% of EMA)
                ema_distance_pct = abs(price - ema) / ema * 100 if ema > 0 else 100
                if ema_distance_pct > 2.0:
                    continue

                # Bullish bar: close > open
                if row["close"] <= row["open"]:
                    continue

                # Price above EMA (reclaimed)
                if price < ema:
                    continue

                # Volume above average (simple: > prev bar volume * 1.5)
                if row["volume"] < prev["volume"] * 1.2:
                    continue

                # Calculate position with slippage on entry
                entry_price = self._slippage.compute_slippage(
                    OrderSide.BUY, price, shares if shares > 0 else 1, int(avg_volume)
                )
                stop_price = round(entry_price - (atr * self._stop_atr_mult), 4)
                stop_distance = entry_price - stop_price

                if stop_distance <= 0:
                    continue

                risk_dollars = equity * self._risk_pct
                shares = math.floor(risk_dollars / stop_distance)

                if shares <= 0:
                    continue

                # Check we can afford it
                if shares * entry_price > equity * 4:
                    shares = math.floor(equity * 4 / entry_price)

                if shares <= 0:
                    continue

                in_trade = True
                entry_date = str(df.index[i].date())

        # Close any open trade at the end
        if in_trade:
            exit_price = df.iloc[-1]["close"]
            pnl = shares * (exit_price - entry_price)
            risk = entry_price - stop_price if entry_price != stop_price else 1.0
            trades.append(
                BacktestTrade(
                    symbol=symbol,
                    entry_date=entry_date,
                    exit_date=str(df.index[-1].date()),
                    entry_price=entry_price,
                    exit_price=exit_price,
                    stop_price=stop_price,
                    shares=shares,
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl / (entry_price * shares) * 100, 2)
                    if shares > 0
                    else 0.0,
                    rr_ratio=round((exit_price - entry_price) / risk, 2),
                    exit_reason="end_of_period",
                )
            )

        return trades

    def _compute_metrics(
        self, symbol: str, trades: list[BacktestTrade]
    ) -> BacktestResult:
        """Compute performance metrics from trade list."""
        if not trades:
            return BacktestResult(
                symbol=symbol,
                total_return_pct=0.0,
                total_pnl=0.0,
                num_trades=0,
                win_rate=0.0,
                avg_win=0.0,
                avg_loss=0.0,
                profit_factor=0.0,
                max_drawdown_pct=0.0,
                sharpe_ratio=0.0,
            )

        pnls = [t.pnl for t in trades]
        total_pnl = sum(pnls)
        total_return_pct = (total_pnl / self._starting_capital) * 100

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        win_rate = len(wins) / len(pnls) * 100 if pnls else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Max drawdown
        equity_curve = [self._starting_capital]
        for pnl in pnls:
            equity_curve.append(equity_curve[-1] + pnl)
        equity_arr = np.array(equity_curve)
        peak = np.maximum.accumulate(equity_arr)
        drawdowns = (equity_arr - peak) / peak * 100
        max_drawdown_pct = abs(drawdowns.min())

        # Sharpe ratio (simplified: daily returns basis)
        if len(pnls) > 1:
            returns = np.array(pnls) / self._starting_capital
            sharpe = (
                np.mean(returns) / np.std(returns) * np.sqrt(252)
                if np.std(returns) > 0
                else 0.0
            )
        else:
            sharpe = 0.0

        return BacktestResult(
            symbol=symbol,
            total_return_pct=round(total_return_pct, 2),
            total_pnl=round(total_pnl, 2),
            num_trades=len(trades),
            win_rate=round(win_rate, 1),
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            max_drawdown_pct=round(max_drawdown_pct, 2),
            sharpe_ratio=round(sharpe, 2),
            trades=trades,
        )

    def report(self, results: dict[str, BacktestResult]) -> str:
        """Format backtest results as a human-readable report."""
        lines = [
            "",
            "=" * 70,
            "  BACKTEST RESULTS",
            "=" * 70,
            f"  Starting Capital: ${self._starting_capital:,.2f}",
            f"  Risk Per Trade:   {self._config.risk.risk_per_trade_pct}%",
            f"  Stop Loss ATR:    {self._stop_atr_mult}x",
            "",
        ]

        total_pnl = 0.0
        total_trades = 0

        for symbol, result in results.items():
            total_pnl += result.total_pnl
            total_trades += result.num_trades

            lines.extend(
                [
                    f"  --- {symbol} ---",
                    f"  Total Return:    {result.total_return_pct:+.2f}%",
                    f"  Total P&L:       ${result.total_pnl:+,.2f}",
                    f"  Trades:          {result.num_trades}",
                    f"  Win Rate:        {result.win_rate:.1f}%",
                    f"  Avg Win:         ${result.avg_win:+,.2f}",
                    f"  Avg Loss:        ${result.avg_loss:+,.2f}",
                    f"  Profit Factor:   {result.profit_factor:.2f}",
                    f"  Max Drawdown:    {result.max_drawdown_pct:.2f}%",
                    f"  Sharpe Ratio:    {result.sharpe_ratio:.2f}",
                    "",
                ]
            )

        overall_return = (total_pnl / self._starting_capital) * 100
        lines.extend(
            [
                "=" * 70,
                "  OVERALL",
                f"  Total P&L:       ${total_pnl:+,.2f}",
                f"  Total Return:    {overall_return:+.2f}%",
                f"  Total Trades:    {total_trades}",
                "=" * 70,
                "",
                "  DISCLAIMER: Past backtest performance does NOT guarantee",
                "  future results. Slippage, fees, and market impact are",
                "  not fully modeled. Use paper trading to validate.",
                "",
            ]
        )

        return "\n".join(lines)
