#!/usr/bin/env python
"""
Runnable entry point for the faithful intraday replay backtester.

Replays the REAL trading pipeline (strategy + regime + advisor + risk +
paper broker + portfolio) over historical 5-minute bars from yfinance and
prints an honest report, writing closed trades to
``data/backtests/intraday_<timestamp>.csv`` in the SAME schema as
``data/journal.csv``.

Examples
--------
    # Default strategy-fit universe, 5m over ~60 days:
    python scripts/run_intraday_backtest.py

    # Custom universe and window:
    python scripts/run_intraday_backtest.py --symbols SOFI,PLUG,RIOT --interval 15m --period 60d

    # Adjust ONLY the first scale-out target so the strategy's own >=1.5 R:R
    # entry gate can pass (the shipped config's 0.75R first target blocks every
    # signal). This is clearly a diagnostic variant, not the shipped behaviour:
    python scripts/run_intraday_backtest.py --min-first-target-r 1.5
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Ensure the project root is importable when run as a bare script.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

from trading_bot.backtest.intraday_engine import (  # noqa: E402
    DEFAULT_UNIVERSE,
    IntradayReplayEngine,
    format_report,
)
from trading_bot.config.settings import AppConfig, ExitConfig, RunMode  # noqa: E402
from trading_bot.utils.logger import setup_logging  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Faithful intraday replay backtester (real strategy pipeline).",
    )
    p.add_argument(
        "--symbols",
        default=",".join(DEFAULT_UNIVERSE),
        help="Comma-separated ticker universe (default: strategy-fit movers).",
    )
    p.add_argument("--config", default="trading_bot/config/config.yaml",
                   help="Path to config YAML.")
    p.add_argument("--interval", default="5m",
                   help="yfinance intraday interval (5m default; 1m only ~7d).")
    p.add_argument("--period", default="60d",
                   help="yfinance lookback period (60d default for 5m/15m).")
    p.add_argument("--capital", type=float, default=None,
                   help="Override starting capital (default: config value).")
    p.add_argument("--output-dir", default="data/backtests",
                   help="Directory for the trades CSV output.")
    p.add_argument("--log-level", default="WARNING",
                   help="Log level (default WARNING to keep the report clean).")
    p.add_argument(
        "--min-first-target-r",
        type=float,
        default=None,
        help=(
            "Diagnostic: set the first scale-out R:R target to this value so "
            "the strategy's own >=1.5 entry gate can pass. The shipped config's "
            "0.75R first target blocks every signal. Not the shipped behaviour."
        ),
    )
    p.add_argument("--json", action="store_true",
                   help="Also print the raw result summary as JSON.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv(override=False)
    setup_logging(args.log_level, json_output=False)

    config = AppConfig.from_yaml(args.config)
    config.run_mode = RunMode.PAPER

    # Optional diagnostic: relax ONLY the first scale-out target so the
    # strategy can emit signals. This does NOT touch any risk limit.
    if args.min_first_target_r is not None:
        targets = list(config.exit.scale_out_rr_targets)
        if targets:
            targets[0] = args.min_first_target_r
            targets = sorted(targets)
        config.exit = ExitConfig(
            scale_out_ratios=config.exit.scale_out_ratios,
            scale_out_rr_targets=targets,
            trailing_stop_atr_multiplier=config.exit.trailing_stop_atr_multiplier,
            trailing_stop_breakeven_buffer_pct=config.exit.trailing_stop_breakeven_buffer_pct,
            hard_time_exit=config.exit.hard_time_exit,
            use_parabolic_sar=config.exit.use_parabolic_sar,
        )

    universe = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_csv = str(output_dir / f"intraday_{ts}.csv")

    engine = IntradayReplayEngine(
        config,
        universe=universe,
        period=args.period,
        interval=args.interval,
        starting_capital=args.capital,
        output_csv=output_csv,
    )

    result = engine.run()
    print(format_report(result))
    print(f"  Trades CSV written to: {output_csv}")

    if args.json:
        printable = {k: v for k, v in result.items() if k != "equity_curve"}
        print(json.dumps(printable, indent=2, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
