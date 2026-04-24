"""
Phase 2.5 Core conversion: offline alpha analysis.

Joins the three Core-conversion CSVs produced by the live/paper bot:

- `data/alpha_scores.csv`  (Phase 2 — one row per decision)
- `data/decision_log.csv`  (Phase 1.5 — one row per candidate evaluated)
- `data/journal.csv`       (existing — one row per closed trade)

…and produces tier / reason / regime statistics so we can answer:
"Do high alpha tiers actually correlate with higher win rate, PnL,
and R-multiples?"

This is OFFLINE analysis only. Nothing here is imported or called by
the trading loop. All I/O is read-only with respect to the existing
CSVs — this module never writes to them.

Usage:
    python -m trading_bot.analysis.alpha_report
    python -m trading_bot.analysis.alpha_report --json
    python -m trading_bot.analysis.alpha_report --output report.txt
    python -m trading_bot.analysis.alpha_report --alpha path/alpha.csv \\
        --decision path/dec.csv --journal path/journal.csv --tolerance 10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Union

import pandas as pd

DEFAULT_ALPHA_CSV = "data/alpha_scores.csv"
DEFAULT_DECISION_CSV = "data/decision_log.csv"
DEFAULT_JOURNAL_CSV = "data/journal.csv"
DEFAULT_MATCH_TOLERANCE_MINUTES = 5

TIER_ORDER: list[str] = ["A", "B", "C", "D", "F"]


# ---------------------------------------------------------------------------
# Loaders — missing or malformed files always return an empty DataFrame.
# ---------------------------------------------------------------------------


def _empty(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame({c: [] for c in columns})


def load_alpha_scores(path: Union[str, Path]) -> pd.DataFrame:
    """Load Phase 2 alpha scores. Empty DataFrame if missing or unreadable."""
    cols = [
        "timestamp", "symbol", "score", "tier", "action", "confidence",
        "regime", "gap_pct", "relative_volume", "volatility", "reasons",
    ]
    p = Path(path)
    if not p.exists():
        return _empty(cols)
    try:
        df = pd.read_csv(p)
    except Exception:
        return _empty(cols)
    if df.empty:
        return _empty(cols)
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    for numeric in ("score", "confidence", "gap_pct", "relative_volume", "volatility"):
        df[numeric] = pd.to_numeric(df[numeric], errors="coerce")
    return df


def load_decision_log(path: Union[str, Path]) -> pd.DataFrame:
    """Load Phase 1.5 decision log. Empty DataFrame if missing or unreadable."""
    cols = [
        "timestamp", "symbol", "price", "gap_pct", "relative_volume",
        "volatility", "regime", "action", "confidence", "reason",
    ]
    p = Path(path)
    if not p.exists():
        return _empty(cols)
    try:
        df = pd.read_csv(p)
    except Exception:
        return _empty(cols)
    if df.empty:
        return _empty(cols)
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    for numeric in ("price", "gap_pct", "relative_volume", "volatility", "confidence"):
        df[numeric] = pd.to_numeric(df[numeric], errors="coerce")
    return df


def load_journal(path: Union[str, Path]) -> pd.DataFrame:
    """
    Load the trade journal.

    Journal stores `date` as YYYY-MM-DD and `entry_time`/`exit_time` as
    HH:MM:SS — reconstruct a full entry_dt by concatenating them so we
    can join with alpha/decision timestamps.
    """
    cols = [
        "date", "symbol", "side", "signal_type", "entry_price", "exit_price",
        "shares", "pnl", "rr_ratio", "hold_time_minutes", "entry_time",
        "exit_time", "exit_reason", "notes",
    ]
    p = Path(path)
    if not p.exists():
        return _empty(cols + ["entry_dt", "exit_dt"])
    try:
        df = pd.read_csv(p)
    except Exception:
        return _empty(cols + ["entry_dt", "exit_dt"])
    if df.empty:
        return _empty(cols + ["entry_dt", "exit_dt"])
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    for numeric in ("pnl", "rr_ratio", "entry_price", "exit_price", "shares",
                    "hold_time_minutes"):
        df[numeric] = pd.to_numeric(df[numeric], errors="coerce")
    df["entry_dt"] = pd.to_datetime(
        df["date"].astype(str) + " " + df["entry_time"].astype(str),
        errors="coerce",
    )
    df["exit_dt"] = pd.to_datetime(
        df["date"].astype(str) + " " + df["exit_time"].astype(str),
        errors="coerce",
    )
    return df


# ---------------------------------------------------------------------------
# Join alpha rows to realized outcomes.
# ---------------------------------------------------------------------------


def join_outcomes(
    alpha_df: pd.DataFrame,
    journal_df: pd.DataFrame,
    tolerance_minutes: int = DEFAULT_MATCH_TOLERANCE_MINUTES,
) -> pd.DataFrame:
    """
    Left-join each alpha row to the closest journal trade by
    (symbol, entry_time) within `tolerance_minutes`.

    Rows that never traded (skip decisions, no matching journal entry)
    keep NaN in the outcome columns — that's intentional and is used
    downstream to compute win_rate / avg_pnl only over rows that traded.
    """
    if alpha_df.empty:
        return alpha_df.copy()

    merged = alpha_df.copy()
    merged["pnl"] = pd.NA
    merged["rr_ratio"] = pd.NA
    merged["hold_time_minutes"] = pd.NA
    merged["exit_reason"] = pd.NA
    merged["matched"] = False

    if journal_df.empty:
        return merged

    # Ensure the tolerance comparison uses datetime64[ns] on both sides.
    alpha_sorted = merged.sort_values("timestamp").reset_index(drop=False)
    journal_sorted = (
        journal_df.dropna(subset=["entry_dt"])
        .sort_values("entry_dt")
        .reset_index(drop=True)
    )
    if journal_sorted.empty:
        return merged

    joined = pd.merge_asof(
        alpha_sorted,
        journal_sorted[[
            "symbol", "entry_dt", "pnl", "rr_ratio",
            "hold_time_minutes", "exit_reason",
        ]].rename(columns={
            "pnl": "_j_pnl",
            "rr_ratio": "_j_rr",
            "hold_time_minutes": "_j_hold",
            "exit_reason": "_j_exit_reason",
        }),
        left_on="timestamp",
        right_on="entry_dt",
        by="symbol",
        direction="nearest",
        tolerance=pd.Timedelta(minutes=tolerance_minutes),
    )

    # Restore original alpha_df row order via the captured index.
    joined = joined.sort_values("index").set_index("index")
    joined.index.name = None

    merged.loc[joined.index, "pnl"] = joined["_j_pnl"]
    merged.loc[joined.index, "rr_ratio"] = joined["_j_rr"]
    merged.loc[joined.index, "hold_time_minutes"] = joined["_j_hold"]
    merged.loc[joined.index, "exit_reason"] = joined["_j_exit_reason"]
    merged.loc[joined.index, "matched"] = joined["entry_dt"].notna()

    return merged


# ---------------------------------------------------------------------------
# Group statistics.
# ---------------------------------------------------------------------------


def _numeric_series(group: pd.DataFrame, col: str) -> pd.Series:
    """Return the column as a numeric Series, or an empty Series if absent."""
    if col not in group.columns:
        return pd.Series([], dtype=float)
    return pd.to_numeric(group[col], errors="coerce").dropna()


def _group_row(group: pd.DataFrame) -> dict[str, Any]:
    count = int(len(group))

    if "action" in group.columns:
        actions = group["action"].astype(str)
        buy_count = int((actions == "buy").sum())
        skip_count = int((actions == "skip").sum())
    else:
        buy_count = 0
        skip_count = 0

    score_series = _numeric_series(group, "score")
    avg_score = float(score_series.mean()) if not score_series.empty else None

    pnl = _numeric_series(group, "pnl")
    rr = _numeric_series(group, "rr_ratio")

    win_rate = None
    avg_pnl = None
    avg_r_multiple = None
    outcome_count = int(len(pnl))
    if outcome_count > 0:
        win_rate = float((pnl > 0).sum()) / outcome_count
        avg_pnl = float(pnl.mean())
    if not rr.empty:
        avg_r_multiple = float(rr.mean())

    return {
        "count": count,
        "buy_count": buy_count,
        "skip_count": skip_count,
        "avg_score": round(avg_score, 4) if avg_score is not None else None,
        "outcome_count": outcome_count,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "avg_pnl": round(avg_pnl, 4) if avg_pnl is not None else None,
        "avg_r_multiple": (
            round(avg_r_multiple, 4) if avg_r_multiple is not None else None
        ),
    }


def tier_stats(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Stats per alpha tier. All five tiers are included in canonical order."""
    out: list[dict[str, Any]] = []
    if df.empty or "tier" not in df.columns:
        for t in TIER_ORDER:
            row = {"tier": t}
            row.update(_group_row(df.head(0)))
            out.append(row)
        return out
    groups = {t: g for t, g in df.groupby("tier")}
    for t in TIER_ORDER:
        g = groups.get(t, df.head(0))
        row = {"tier": t}
        row.update(_group_row(g))
        out.append(row)
    return out


def reason_stats(df: pd.DataFrame) -> list[dict[str, Any]]:
    """
    Stats per decision reason. Uses the decision-log `reason` column
    when present on the alpha row; otherwise falls back to the first
    pipe-separated token in the alpha `reasons` column.
    """
    if df.empty:
        return []
    key_col = None
    if "reason" in df.columns and df["reason"].notna().any():
        key_col = "reason"
    elif "reasons" in df.columns and df["reasons"].notna().any():
        # Use the first token as a stable bucket (e.g. "gap_sweet_spot" or
        # "action_skip:strategy:no_valid_setup").
        df = df.copy()
        df["_reason_key"] = (
            df["reasons"].fillna("").astype(str).str.split("|").str[0]
        )
        key_col = "_reason_key"
    else:
        return []

    out: list[dict[str, Any]] = []
    for reason, g in df.groupby(key_col):
        if pd.isna(reason) or reason == "":
            continue
        row = {"reason": str(reason)}
        row.update(_group_row(g))
        out.append(row)
    out.sort(key=lambda r: r["count"], reverse=True)
    return out


def regime_stats(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Stats per market regime."""
    if df.empty or "regime" not in df.columns:
        return []
    out: list[dict[str, Any]] = []
    for regime, g in df.groupby("regime"):
        if pd.isna(regime) or regime == "":
            continue
        row = {"regime": str(regime)}
        row.update(_group_row(g))
        out.append(row)
    out.sort(key=lambda r: r["count"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Report builder.
# ---------------------------------------------------------------------------


def build_report(
    alpha_path: Union[str, Path] = DEFAULT_ALPHA_CSV,
    decision_path: Union[str, Path] = DEFAULT_DECISION_CSV,
    journal_path: Union[str, Path] = DEFAULT_JOURNAL_CSV,
    tolerance_minutes: int = DEFAULT_MATCH_TOLERANCE_MINUTES,
) -> dict[str, Any]:
    """Assemble the full analysis report as a plain dict."""
    alpha_df = load_alpha_scores(alpha_path)
    decision_df = load_decision_log(decision_path)
    journal_df = load_journal(journal_path)

    sources = {
        "alpha_scores": {
            "path": str(alpha_path),
            "exists": Path(alpha_path).exists(),
            "rows": int(len(alpha_df)),
        },
        "decision_log": {
            "path": str(decision_path),
            "exists": Path(decision_path).exists(),
            "rows": int(len(decision_df)),
        },
        "journal": {
            "path": str(journal_path),
            "exists": Path(journal_path).exists(),
            "rows": int(len(journal_df)),
        },
    }

    notes: list[str] = []
    if not sources["alpha_scores"]["exists"]:
        notes.append("alpha_scores.csv not found — report is empty")
    if not sources["decision_log"]["exists"]:
        notes.append("decision_log.csv not found — reason stats may be limited")
    if not sources["journal"]["exists"]:
        notes.append("journal.csv not found — outcome stats unavailable")

    joined = join_outcomes(alpha_df, journal_df, tolerance_minutes)

    matched_trades = int(joined["matched"].sum()) if "matched" in joined else 0

    return {
        "sources": sources,
        "tolerance_minutes": tolerance_minutes,
        "totals": {
            "alpha_rows": int(len(alpha_df)),
            "buy_rows": (
                int((alpha_df.get("action") == "buy").sum())
                if "action" in alpha_df.columns
                else 0
            ),
            "skip_rows": (
                int((alpha_df.get("action") == "skip").sum())
                if "action" in alpha_df.columns
                else 0
            ),
            "matched_trades": matched_trades,
            "journal_trades": int(len(journal_df)),
        },
        "tier_stats": tier_stats(joined),
        "reason_stats": reason_stats(joined),
        "regime_stats": regime_stats(joined),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Formatters.
# ---------------------------------------------------------------------------


def _fmt(value: Optional[float], spec: str = ".4f", na: str = "n/a") -> str:
    if value is None:
        return na
    try:
        if isinstance(value, float) and (value != value):  # NaN
            return na
    except Exception:
        return na
    try:
        return format(value, spec)
    except Exception:
        return str(value)


def format_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("ALPHA ANALYSIS REPORT (Phase 2.5)")
    lines.append("=" * 78)
    sources = report["sources"]
    lines.append("Sources:")
    for name, meta in sources.items():
        lines.append(
            f"  {name:<14} exists={meta['exists']!s:<5} "
            f"rows={meta['rows']:<6} path={meta['path']}"
        )
    lines.append(f"Join tolerance: {report['tolerance_minutes']} minutes")

    totals = report["totals"]
    lines.append("")
    lines.append("Totals:")
    lines.append(f"  alpha_rows     = {totals['alpha_rows']}")
    lines.append(f"  buy_rows       = {totals['buy_rows']}")
    lines.append(f"  skip_rows      = {totals['skip_rows']}")
    lines.append(f"  journal_trades = {totals['journal_trades']}")
    lines.append(f"  matched_trades = {totals['matched_trades']}")

    def _section(title: str, key: str, rows: list[dict[str, Any]]) -> None:
        lines.append("")
        lines.append(f"{title}:")
        header = (
            f"  {key:<28} {'count':>6} {'buy':>5} {'skip':>5} "
            f"{'avg_score':>10} {'outcomes':>9} {'win_rate':>9} "
            f"{'avg_pnl':>10} {'avg_R':>7}"
        )
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 2))
        if not rows:
            lines.append(f"  (no data)")
            return
        for row in rows:
            lines.append(
                f"  {str(row[key])[:28]:<28} "
                f"{row['count']:>6} "
                f"{row['buy_count']:>5} "
                f"{row['skip_count']:>5} "
                f"{_fmt(row['avg_score'], '.4f'):>10} "
                f"{row['outcome_count']:>9} "
                f"{_fmt(row['win_rate'], '.2%'):>9} "
                f"{_fmt(row['avg_pnl'], '.2f'):>10} "
                f"{_fmt(row['avg_r_multiple'], '.2f'):>7}"
            )

    _section("By tier", "tier", report["tier_stats"])
    _section("By reason (top first)", "reason", report["reason_stats"])
    _section("By regime", "regime", report["regime_stats"])

    if report.get("notes"):
        lines.append("")
        lines.append("Notes:")
        for n in report["notes"]:
            lines.append(f"  - {n}")

    lines.append("=" * 78)
    return "\n".join(lines)


def format_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=False, default=str)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.analysis.alpha_report",
        description=(
            "Offline analysis of Phase 1.5 / 2 Core-conversion datasets. "
            "Joins alpha_scores.csv, decision_log.csv, and journal.csv, "
            "then prints tier / reason / regime statistics."
        ),
    )
    parser.add_argument("--alpha", default=DEFAULT_ALPHA_CSV,
                        help=f"Path to alpha_scores.csv (default: {DEFAULT_ALPHA_CSV})")
    parser.add_argument("--decision", default=DEFAULT_DECISION_CSV,
                        help=f"Path to decision_log.csv (default: {DEFAULT_DECISION_CSV})")
    parser.add_argument("--journal", default=DEFAULT_JOURNAL_CSV,
                        help=f"Path to journal.csv (default: {DEFAULT_JOURNAL_CSV})")
    parser.add_argument("--tolerance", type=int,
                        default=DEFAULT_MATCH_TOLERANCE_MINUTES,
                        help="Minutes of slack when joining alpha rows to trades")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of plain text")
    parser.add_argument("--output", default=None,
                        help="Write report to this path instead of stdout")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    report = build_report(
        alpha_path=args.alpha,
        decision_path=args.decision,
        journal_path=args.journal,
        tolerance_minutes=args.tolerance,
    )

    payload = format_json(report) if args.json else format_text(report)

    if args.output:
        try:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(payload + "\n")
        except Exception as e:  # pragma: no cover
            print(f"error: failed to write {args.output}: {e}", file=sys.stderr)
            return 2
    else:
        print(payload)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
