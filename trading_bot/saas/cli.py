"""
Operator CLI for the SaaS report engine.

Usage:
    python -m trading_bot.saas generate
    python -m trading_bot.saas generate --universe AAPL,MSFT,NVDA
    python -m trading_bot.saas generate --provider demo --output /tmp/sig
    python -m trading_bot.saas list

The command is operator-only — it never touches the live trading
core, never places trades, and never prints raw API keys or Stripe
secrets.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from trading_bot.saas.report_engine import (
    DEFAULT_REPORTS_DIR,
    generate_report,
    list_report_dates,
    persist_report,
    reports_dir as _default_reports_dir,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.saas",
        description="Operator commands for the SaaS signal-report engine.",
    )
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("generate", help="Generate and persist a signal report.")
    gen.add_argument(
        "--universe",
        default=None,
        help="Comma-separated symbol list (overrides TRADING_SAAS_UNIVERSE).",
    )
    gen.add_argument(
        "--provider",
        default=None,
        choices=("polygon", "alpaca", "yfinance", "demo"),
        help="Force a specific market data provider.",
    )
    gen.add_argument(
        "--output",
        default=None,
        help=f"Reports directory (default: {DEFAULT_REPORTS_DIR}).",
    )
    gen.add_argument(
        "--print",
        action="store_true",
        help="Also print the report JSON to stdout.",
    )

    ls = sub.add_parser("list", help="List persisted SaaS report dates.")
    ls.add_argument(
        "--output",
        default=None,
        help=f"Reports directory (default: {DEFAULT_REPORTS_DIR}).",
    )
    return parser


def _generate(args: argparse.Namespace) -> int:
    universe: Optional[list[str]] = None
    if args.universe:
        universe = [
            s.strip().upper()
            for s in str(args.universe).split(",")
            if s.strip()
        ]
        if not universe:
            print("error: --universe parsed to an empty list", file=sys.stderr)
            return 2

    target_dir = Path(args.output) if args.output else _default_reports_dir()
    report = generate_report(universe=universe, provider=args.provider)
    path = persist_report(report, target_dir=target_dir)
    print(f"wrote {path}")
    summary = report.get("summary") or {}
    print(
        "summary: "
        f"signals={summary.get('signal_count', 0)} "
        f"bull={summary.get('bullish_count', 0)} "
        f"bear={summary.get('bearish_count', 0)} "
        f"neutral={summary.get('neutral_count', 0)} "
        f"avg_conf={summary.get('average_confidence', 0.0)}"
    )
    if args.print:
        print(json.dumps(report, indent=2, default=str))
    return 0


def _list(args: argparse.Namespace) -> int:
    target_dir = Path(args.output) if args.output else _default_reports_dir()
    dates = list_report_dates(target_dir)
    if not dates:
        print(f"no reports in {target_dir}")
        return 0
    print(f"reports in {target_dir}:")
    for d in dates:
        print(f"  {d}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate":
        return _generate(args)
    if args.command == "list":
        return _list(args)
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
