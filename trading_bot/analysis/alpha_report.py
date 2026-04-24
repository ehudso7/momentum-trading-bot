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
import glob
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import pandas as pd

PathSpec = Union[str, Path, None]

DEFAULT_ALPHA_CSV = "data/alpha_scores.csv"
DEFAULT_DECISION_CSV = "data/decision_log.csv"
DEFAULT_JOURNAL_CSV = "data/journal.csv"
DEFAULT_MATCH_TOLERANCE_MINUTES = 5
DEFAULT_MIN_REQUIRED_OUTCOMES = 100

TIER_ORDER: list[str] = ["A", "B", "C", "D", "F"]
HIGH_TIERS: list[str] = ["A", "B"]
LOW_TIERS: list[str] = ["C", "D", "F"]

# Phase 2.9 — tier-based "what if we had filtered trades?" thresholds.
# Order matters — printed top-to-bottom in the text report.
SHADOW_FILTER_THRESHOLDS: list[tuple[str, list[str]]] = [
    ("A only", ["A"]),
    ("A+B", ["A", "B"]),
    ("A+B+C", ["A", "B", "C"]),
]

# Ten decile buckets [0.0, 0.1), [0.1, 0.2), …, [0.9, 1.0] — the last
# bucket is extended by an epsilon so a score of exactly 1.0 is captured.
DECILE_BINS: list[float] = [i / 10.0 for i in range(11)]
DECILE_BINS[-1] = 1.0 + 1e-9  # include 1.0 in the last bucket
DECILE_LABELS: list[str] = [
    f"{i / 10:.1f}-{(i + 1) / 10:.1f}" for i in range(10)
]


# ---------------------------------------------------------------------------
# Loaders — missing or malformed files always return an empty DataFrame.
# ---------------------------------------------------------------------------


def _empty(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame({c: [] for c in columns})


def _looks_like_glob(text: str) -> bool:
    """True if `text` contains any shell-glob meta character."""
    return any(ch in text for ch in "*?[")


def _resolve_paths(spec: PathSpec) -> list[Path]:
    """
    Resolve an input spec into a deduplicated list of existing CSV paths.

    Accepted forms (Phase 2.8):
      - None                                → []
      - Path                                → [path] if it exists, else []
      - "path/to/file.csv"                  → single explicit path
      - "data/alpha_scores_*.csv"           → glob pattern (sorted)
      - "a.csv,b.csv"                       → comma-separated list
      - "data/a_*.csv,data/b.csv"           → mixed globs and explicit paths

    Missing explicit paths and empty glob matches are silently skipped
    — `build_report` is always best-effort and never raises on I/O.
    Order: comma-separated segments are processed left-to-right; glob
    matches within a segment are sorted lexicographically so a run
    across rotated daily files is chronologically stable.
    """
    if spec is None:
        return []
    if isinstance(spec, Path):
        return [spec] if spec.is_file() else []
    s = str(spec).strip()
    if not s:
        return []

    resolved: list[Path] = []
    seen: set[Path] = set()
    for part in (segment.strip() for segment in s.split(",")):
        if not part:
            continue
        if _looks_like_glob(part):
            matches = sorted(glob.glob(part))
        else:
            matches = [part]
        for match in matches:
            p = Path(match)
            if p in seen:
                continue
            if not p.is_file():
                continue
            seen.add(p)
            resolved.append(p)
    return resolved


def _read_and_concat(
    paths: Iterable[Path],
    all_columns: list[str],
    timestamp_col: str = "timestamp",
) -> pd.DataFrame:
    """
    Read every CSV in `paths`, concatenate them, and drop any rows that
    look like a duplicated header (common when files were appended
    together by hand).

    Returns an empty DataFrame with `all_columns` when there is nothing
    to load.
    """
    frames: list[pd.DataFrame] = []
    for p in paths:
        try:
            frame = pd.read_csv(p)
        except Exception:
            continue
        if frame is None or frame.empty:
            continue
        frames.append(frame)
    if not frames:
        return _empty(all_columns)

    df = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
    if df.empty:
        return _empty(all_columns)

    # Drop rows whose first column literally equals the header name —
    # catches `cat a.csv b.csv > merged.csv` style concatenation.
    if timestamp_col in df.columns:
        header_mask = df[timestamp_col].astype(str).str.strip() == timestamp_col
        if header_mask.any():
            df = df[~header_mask].reset_index(drop=True)

    return df


def load_alpha_scores(path: PathSpec) -> pd.DataFrame:
    """
    Load Phase 2 alpha scores.

    Accepts a single CSV path, a glob pattern, or a comma-separated
    list of paths/globs. Empty DataFrame if nothing resolves or every
    matched file is unreadable.
    """
    cols = [
        "timestamp", "symbol", "score", "tier", "action", "confidence",
        "regime", "gap_pct", "relative_volume", "volatility", "reasons",
    ]
    paths = _resolve_paths(path)
    if not paths:
        return _empty(cols)
    df = _read_and_concat(paths, cols)
    if df.empty:
        return df
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    for numeric in ("score", "confidence", "gap_pct", "relative_volume", "volatility"):
        df[numeric] = pd.to_numeric(df[numeric], errors="coerce")
    return df


def load_decision_log(path: PathSpec) -> pd.DataFrame:
    """Load Phase 1.5 decision log. Accepts path / glob / comma-separated spec."""
    cols = [
        "timestamp", "symbol", "price", "gap_pct", "relative_volume",
        "volatility", "regime", "action", "confidence", "reason",
    ]
    paths = _resolve_paths(path)
    if not paths:
        return _empty(cols)
    df = _read_and_concat(paths, cols)
    if df.empty:
        return df
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    for numeric in ("price", "gap_pct", "relative_volume", "volatility", "confidence"):
        df[numeric] = pd.to_numeric(df[numeric], errors="coerce")
    return df


def load_journal(path: PathSpec) -> pd.DataFrame:
    """
    Load the trade journal. Accepts path / glob / comma-separated spec.

    Journal stores `date` as YYYY-MM-DD and `entry_time`/`exit_time` as
    HH:MM:SS — reconstruct a full entry_dt by concatenating them so we
    can join with alpha/decision timestamps.
    """
    cols = [
        "date", "symbol", "side", "signal_type", "entry_price", "exit_price",
        "shares", "pnl", "rr_ratio", "hold_time_minutes", "entry_time",
        "exit_time", "exit_reason", "notes",
    ]
    paths = _resolve_paths(path)
    if not paths:
        return _empty(cols + ["entry_dt", "exit_dt"])
    df = _read_and_concat(paths, cols, timestamp_col="date")
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
# Phase 2.6 — score-decile calibration.
# ---------------------------------------------------------------------------


def decile_stats(df: pd.DataFrame) -> list[dict[str, Any]]:
    """
    Bin alpha scores into 10 deciles of width 0.1 and compute per-bucket
    stats. Bucketing is left-closed / right-open except the last bucket,
    which includes a score of exactly 1.0.

    Always returns all 10 rows, in ascending order, so a consumer can
    render a clean calibration curve even when some bins are empty.
    """
    out: list[dict[str, Any]] = []
    if df.empty or "score" not in df.columns:
        for lbl in DECILE_LABELS:
            row = {"decile": lbl}
            row.update(_group_row(df.head(0)))
            out.append(row)
        return out

    scores = pd.to_numeric(df["score"], errors="coerce").clip(
        lower=0.0, upper=1.0
    )
    df2 = df.copy()
    df2["_decile"] = pd.cut(
        scores,
        bins=DECILE_BINS,
        labels=DECILE_LABELS,
        include_lowest=True,
        right=False,
    )

    for lbl in DECILE_LABELS:
        g = df2[df2["_decile"] == lbl]
        row = {"decile": lbl}
        row.update(_group_row(g))
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Phase 2.6 — promotion readiness.
# ---------------------------------------------------------------------------


def _ab_outperforms(
    ab_wr: Optional[float],
    cdf_wr: Optional[float],
    ab_r: Optional[float],
    cdf_r: Optional[float],
    ab_n: int,
    cdf_n: int,
) -> bool:
    """
    Has the A/B tier cohort demonstrably beaten the C/D/F tier cohort?

    - No outcomes on A/B → cannot outperform.
    - Only A/B has outcomes (C/D/F all skipped → no realized trades):
      fall back to an absolute positive-expectancy heuristic.
    - Both sides have outcomes: require A/B to beat C/D/F on both
      win rate AND avg R-multiple, to avoid declaring victory from a
      single flaky metric.
    """
    if ab_n == 0:
        return False
    if cdf_n == 0:
        if ab_wr is not None and ab_wr >= 0.5:
            return True
        if ab_r is not None and ab_r > 0.5:
            return True
        return False
    wr_ok = ab_wr is not None and cdf_wr is not None and ab_wr > cdf_wr
    r_ok = ab_r is not None and cdf_r is not None and ab_r > cdf_r
    return wr_ok and r_ok


def promotion_readiness(
    joined: pd.DataFrame,
    min_required_outcomes: int = DEFAULT_MIN_REQUIRED_OUTCOMES,
) -> dict[str, Any]:
    """
    Assess whether the alpha scores are predictive enough to graduate
    out of shadow mode. Returns a status string plus the underlying
    numbers the decision was based on.

    Status transitions:
      - "not_ready"                    : fewer than min_required_outcomes and
                                         no clear A/B edge visible.
      - "weak"                         : enough outcomes but A/B fails to
                                         outperform C/D/F.
      - "promising"                    : A/B outperforms but sample is small.
      - "ready_for_shadow_filter_test" : A/B outperforms AND we have at
                                         least min_required_outcomes total.
    """
    ab_pnl = pd.Series([], dtype=float)
    cdf_pnl = pd.Series([], dtype=float)
    ab_rr = pd.Series([], dtype=float)
    cdf_rr = pd.Series([], dtype=float)

    if not joined.empty and "tier" in joined.columns:
        tier_col = joined["tier"].astype(str)
        ab_df = joined[tier_col.isin(HIGH_TIERS)]
        cdf_df = joined[tier_col.isin(LOW_TIERS)]
        ab_pnl = _numeric_series(ab_df, "pnl")
        cdf_pnl = _numeric_series(cdf_df, "pnl")
        ab_rr = _numeric_series(ab_df, "rr_ratio")
        cdf_rr = _numeric_series(cdf_df, "rr_ratio")

    ab_n = int(len(ab_pnl))
    cdf_n = int(len(cdf_pnl))
    total_outcomes = ab_n + cdf_n

    ab_wr: Optional[float] = (
        float((ab_pnl > 0).sum()) / ab_n if ab_n > 0 else None
    )
    cdf_wr: Optional[float] = (
        float((cdf_pnl > 0).sum()) / cdf_n if cdf_n > 0 else None
    )
    ab_r: Optional[float] = float(ab_rr.mean()) if not ab_rr.empty else None
    cdf_r: Optional[float] = (
        float(cdf_rr.mean()) if not cdf_rr.empty else None
    )

    outperforms = _ab_outperforms(ab_wr, cdf_wr, ab_r, cdf_r, ab_n, cdf_n)
    enough = total_outcomes >= max(1, int(min_required_outcomes))

    if outperforms and enough:
        status = "ready_for_shadow_filter_test"
        explanation = (
            f"A/B tiers outperform C/D/F and sample size "
            f"{total_outcomes} >= {min_required_outcomes} outcomes."
        )
    elif outperforms:
        status = "promising"
        explanation = (
            f"A/B tiers outperform but sample is small "
            f"({total_outcomes}/{min_required_outcomes} outcomes)."
        )
    elif enough:
        status = "weak"
        explanation = (
            f"A/B tiers fail to outperform C/D/F over "
            f"{total_outcomes} outcomes."
        )
    else:
        status = "not_ready"
        explanation = (
            f"Insufficient outcomes ({total_outcomes}/"
            f"{min_required_outcomes}) to assess."
        )

    def _round(v: Optional[float], nd: int = 4) -> Optional[float]:
        return round(v, nd) if v is not None else None

    return {
        "status": status,
        "explanation": explanation,
        "min_required_outcomes": int(min_required_outcomes),
        "outcome_count": total_outcomes,
        "ab_outperforms": outperforms,
        "ab": {
            "outcome_count": ab_n,
            "win_rate": _round(ab_wr),
            "avg_r_multiple": _round(ab_r),
        },
        "cdf": {
            "outcome_count": cdf_n,
            "win_rate": _round(cdf_wr),
            "avg_r_multiple": _round(cdf_r),
        },
    }


# ---------------------------------------------------------------------------
# Phase 2.9 — shadow-mode tier-filter simulation.
# ---------------------------------------------------------------------------


def _side_stats(prefix: str, df: pd.DataFrame) -> dict[str, Any]:
    """Compute win_rate / avg_pnl / avg_r_multiple for one side of the split."""
    pnl = _numeric_series(df, "pnl")
    rr = _numeric_series(df, "rr_ratio")
    outcome_count = int(len(pnl))

    wr: Optional[float] = None
    avg_pnl: Optional[float] = None
    avg_r: Optional[float] = None
    if outcome_count > 0:
        wr = float((pnl > 0).sum()) / outcome_count
        avg_pnl = float(pnl.mean())
    if not rr.empty:
        avg_r = float(rr.mean())

    return {
        f"{prefix}_outcome_count": outcome_count,
        f"{prefix}_win_rate": round(wr, 4) if wr is not None else None,
        f"{prefix}_avg_pnl": round(avg_pnl, 4) if avg_pnl is not None else None,
        f"{prefix}_avg_r_multiple": round(avg_r, 4) if avg_r is not None else None,
    }


def _empty_filter_row(label: str, allowed_tiers: list[str]) -> dict[str, Any]:
    """Baseline zero row used when the joined frame is empty."""
    return {
        "threshold": label,
        "allowed_tiers": list(allowed_tiers),
        "allowed_buy_count": 0,
        "blocked_buy_count": 0,
        "allowed_outcome_count": 0,
        "allowed_win_rate": None,
        "allowed_avg_pnl": None,
        "allowed_avg_r_multiple": None,
        "blocked_outcome_count": 0,
        "blocked_win_rate": None,
        "blocked_avg_pnl": None,
        "blocked_avg_r_multiple": None,
    }


def shadow_filter_simulation(joined: pd.DataFrame) -> list[dict[str, Any]]:
    """
    Simulate "what if we had only allowed buys whose tier >= X?"

    Operates exclusively on rows where `action == "buy"` — skips are
    not part of the simulation because they never traded. For each
    threshold (A only, A+B, A+B+C) the buys are partitioned into
    `allowed` (tier meets threshold) and `blocked` (tier below it),
    and both sides are summarised by realized outcomes.

    SHADOW MODE ONLY. Nothing in the trading loop ever consults these
    numbers — this is an offline "what if" answer used to decide
    whether promoting alpha out of shadow would help or hurt.
    """
    if joined.empty or "tier" not in joined.columns or "action" not in joined.columns:
        return [_empty_filter_row(label, tiers) for label, tiers in SHADOW_FILTER_THRESHOLDS]

    buys = joined[joined["action"].astype(str).str.lower() == "buy"]
    if buys.empty:
        return [_empty_filter_row(label, tiers) for label, tiers in SHADOW_FILTER_THRESHOLDS]

    tier_col = buys["tier"].astype(str)
    out: list[dict[str, Any]] = []
    for label, allowed_tiers in SHADOW_FILTER_THRESHOLDS:
        allowed_set = set(allowed_tiers)
        allowed = buys[tier_col.isin(allowed_set)]
        blocked = buys[~tier_col.isin(allowed_set)]
        row: dict[str, Any] = {
            "threshold": label,
            "allowed_tiers": list(allowed_tiers),
            "allowed_buy_count": int(len(allowed)),
            "blocked_buy_count": int(len(blocked)),
        }
        row.update(_side_stats("allowed", allowed))
        row.update(_side_stats("blocked", blocked))
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Report builder.
# ---------------------------------------------------------------------------


def _source_meta(spec: PathSpec, rows: int) -> dict[str, Any]:
    """
    Build the per-source metadata dict.

    Phase 2.8: `spec` can be a single path, a glob, or a comma-separated
    list. Resolved files are listed so an operator can see exactly
    which CSVs were consumed.
    """
    paths = _resolve_paths(spec)
    return {
        "path": "" if spec is None else str(spec),
        "exists": len(paths) > 0,
        "resolved_files": len(paths),
        "resolved_paths": [str(p) for p in paths],
        "rows": int(rows),
    }


def build_report(
    alpha_path: PathSpec = DEFAULT_ALPHA_CSV,
    decision_path: PathSpec = DEFAULT_DECISION_CSV,
    journal_path: PathSpec = DEFAULT_JOURNAL_CSV,
    tolerance_minutes: int = DEFAULT_MATCH_TOLERANCE_MINUTES,
    min_required_outcomes: int = DEFAULT_MIN_REQUIRED_OUTCOMES,
) -> dict[str, Any]:
    """Assemble the full analysis report as a plain dict."""
    alpha_df = load_alpha_scores(alpha_path)
    decision_df = load_decision_log(decision_path)
    journal_df = load_journal(journal_path)

    sources = {
        "alpha_scores": _source_meta(alpha_path, len(alpha_df)),
        "decision_log": _source_meta(decision_path, len(decision_df)),
        "journal": _source_meta(journal_path, len(journal_df)),
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
        "decile_stats": decile_stats(joined),
        "promotion_readiness": promotion_readiness(
            joined, min_required_outcomes=min_required_outcomes
        ),
        "shadow_filter_simulation": shadow_filter_simulation(joined),
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
        files = meta.get("resolved_files", 1 if meta.get("exists") else 0)
        lines.append(
            f"  {name:<14} exists={meta['exists']!s:<5} "
            f"files={files:<3} rows={meta['rows']:<6} "
            f"requested={meta['path']}"
        )
        resolved_paths = meta.get("resolved_paths", [])
        if files > 1:
            for rp in resolved_paths:
                lines.append(f"    - {rp}")
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
    _section("By score decile", "decile", report.get("decile_stats", []))

    sim_rows = report.get("shadow_filter_simulation") or []
    if sim_rows:
        lines.append("")
        lines.append("Shadow filter simulation:")
        header = (
            f"  {'threshold':<8} "
            f"{'allowed_buys':>12} {'blocked_buys':>12} "
            f"{'a_out':>6} {'a_wr':>8} {'a_pnl':>10} {'a_R':>7} "
            f"{'b_out':>6} {'b_wr':>8} {'b_pnl':>10} {'b_R':>7}"
        )
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 2))
        for row in sim_rows:
            lines.append(
                f"  {str(row['threshold']):<8} "
                f"{row['allowed_buy_count']:>12} "
                f"{row['blocked_buy_count']:>12} "
                f"{row['allowed_outcome_count']:>6} "
                f"{_fmt(row['allowed_win_rate'], '.2%'):>8} "
                f"{_fmt(row['allowed_avg_pnl'], '.2f'):>10} "
                f"{_fmt(row['allowed_avg_r_multiple'], '.2f'):>7} "
                f"{row['blocked_outcome_count']:>6} "
                f"{_fmt(row['blocked_win_rate'], '.2%'):>8} "
                f"{_fmt(row['blocked_avg_pnl'], '.2f'):>10} "
                f"{_fmt(row['blocked_avg_r_multiple'], '.2f'):>7}"
            )

    readiness = report.get("promotion_readiness")
    if readiness:
        lines.append("")
        lines.append("Promotion readiness:")
        lines.append(f"  status      = {readiness['status']}")
        lines.append(
            f"  outcomes    = {readiness['outcome_count']}/"
            f"{readiness['min_required_outcomes']} required"
        )
        lines.append(
            f"  explanation = {readiness.get('explanation', '')}"
        )
        ab = readiness.get("ab", {})
        cdf = readiness.get("cdf", {})
        lines.append(
            f"  A/B tiers   : outcomes={ab.get('outcome_count', 0):<4} "
            f"win_rate={_fmt(ab.get('win_rate'), '.2%')} "
            f"avg_R={_fmt(ab.get('avg_r_multiple'), '.2f')}"
        )
        lines.append(
            f"  C/D/F tiers : outcomes={cdf.get('outcome_count', 0):<4} "
            f"win_rate={_fmt(cdf.get('win_rate'), '.2%')} "
            f"avg_R={_fmt(cdf.get('avg_r_multiple'), '.2f')}"
        )

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
    parser.add_argument(
        "--alpha", default=DEFAULT_ALPHA_CSV,
        help=(
            f"Path, glob, or comma-separated list of alpha_scores CSVs "
            f"(default: {DEFAULT_ALPHA_CSV}). "
            'Example: --alpha "data/alpha_scores_*.csv"'
        ),
    )
    parser.add_argument(
        "--decision", default=DEFAULT_DECISION_CSV,
        help=(
            f"Path, glob, or comma-separated list of decision_log CSVs "
            f"(default: {DEFAULT_DECISION_CSV})"
        ),
    )
    parser.add_argument(
        "--journal", default=DEFAULT_JOURNAL_CSV,
        help=(
            f"Path, glob, or comma-separated list of journal CSVs "
            f"(default: {DEFAULT_JOURNAL_CSV})"
        ),
    )
    parser.add_argument("--tolerance", type=int,
                        default=DEFAULT_MATCH_TOLERANCE_MINUTES,
                        help="Minutes of slack when joining alpha rows to trades")
    parser.add_argument("--min-outcomes", dest="min_outcomes", type=int,
                        default=DEFAULT_MIN_REQUIRED_OUTCOMES,
                        help=(
                            "Minimum realized-outcome count required for "
                            "promotion readiness to leave the 'not_ready' "
                            f"state (default: {DEFAULT_MIN_REQUIRED_OUTCOMES})"
                        ))
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
        min_required_outcomes=args.min_outcomes,
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
