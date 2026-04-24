# Core Conversion Datasets

This document describes the three CSV datasets that make up the Core
conversion data pipeline (Phases 1.5, 2, and existing). These are
**offline analysis targets** — the trading loop writes to them but
never reads them back for any accept/reject decision.

Used together with `trading_bot.analysis.alpha_report` to validate
whether alpha scores predict realized outcomes before promoting alpha
into live execution.

- [`data/decision_log.csv`](#datadecision_logcsv-phase-15)  — one row per candidate evaluated
- [`data/alpha_scores.csv`](#dataalpha_scorescsv-phase-2)  — one row per alpha score produced
- [`data/journal.csv`](#datajournalcsv-existing) — one row per closed trade
- [Join assumptions](#join-assumptions)
- [Timestamp assumptions](#timestamp-assumptions)
- [Rotation](#rotation)
- [Known limitations](#known-limitations)
- [Promotion rule](#promotion-rule)


## `data/decision_log.csv`  (Phase 1.5)

Produced by `trading_bot/persistence/decision_log.py`. Written from
`trading_bot.main.TradingBot._log_decision` for every candidate the
main loop evaluates — including skipped symbols (already-held,
empty-bars), rejected strategy / risk / correlation / advisor
outcomes, and executed buys.

| Column            | Type   | Source                       | Description |
| ---               | ---    | ---                          | --- |
| `timestamp`       | str    | `now_et()`                   | ET wall-clock, `YYYY-MM-DD HH:MM:SS`. |
| `symbol`          | str    | scanner `ScanResult.symbol`  | Uppercase ticker. |
| `price`           | float  | `ScanResult.price`           | Last price at scan time (USD). |
| `gap_pct`         | float  | `ScanResult.gap_pct`         | Percent gap from prior close. |
| `relative_volume` | float  | `ScanResult.relative_volume` | Session volume / 20-day avg. |
| `volatility`      | float  | `compute_atr(bars, 14) / price * 100` | ATR as % of price. 0.0 when bars are too thin. |
| `regime`          | str    | `RegimeDetector.detect(SPY)` | `trending_bullish`, `trending_bearish`, `range_bound`, `high_volatility`, `low_volatility`, or `unknown`. |
| `action`          | str    | per-branch                   | `"buy"` or `"skip"`. |
| `confidence`      | float  | `TradeSignal.confidence` or advisor, fallback 0.5 | Strategy/advisor confidence at the decision moment. |
| `reason`          | str    | per-branch                   | `"executed"` for buys. For skips: `"already_held"`, `"empty_bars"`, `"strategy:<detail>"`, `"risk:<detail>"`, `"correlation:<detail>"`, `"advisor:<detail>"`, `"broker_rejected"`, `"equity_api_failed"`, `"zero_equity"`. |

Header is created on first instantiation of `DecisionLogger`. Append
failures are logged at DEBUG and never raise into the trading loop.


## `data/alpha_scores.csv`  (Phase 2)

Produced by `trading_bot/core/alpha.py`. Written by `AlphaLogger`
immediately after every `SignalDecision` is logged, using the
matching `FeatureSnapshot` — so every row in `decision_log.csv` has
exactly one corresponding row in `alpha_scores.csv`.

**Shadow mode only.** The trading loop never consults an alpha score.

| Column            | Type   | Description |
| ---               | ---    | --- |
| `timestamp`       | str    | Copied from `AlphaScore.timestamp` (same as the paired decision row). |
| `symbol`          | str    | Ticker. |
| `score`           | float  | Alpha score in `[0.0, 1.0]`. |
| `tier`            | str    | `A` (≥0.80), `B` (≥0.65), `C` (≥0.50), `D` (≥0.35), else `F`. |
| `action`          | str    | `"buy"` / `"skip"` — mirrors the decision row. |
| `confidence`      | float  | Mirrors the decision row. |
| `regime`          | str    | Mirrors the snapshot row. |
| `gap_pct`         | float  | Mirrors the snapshot row. |
| `relative_volume` | float  | Mirrors the snapshot row. |
| `volatility`      | float  | Mirrors the snapshot row. |
| `reasons`         | str    | Pipe-separated human-readable reason tokens, e.g. `gap_sweet_spot(12.0%)\|rvol_strong(8.0x)\|regime_trending_bullish(1.00)\|confidence(0.85)\|action_buy`. |


## `data/journal.csv`  (existing)

Produced by `trading_bot/portfolio/manager.py` on each closed trade.
Predates the Core conversion — included here because the alpha
analysis layer joins to it.

| Column              | Type   | Description |
| ---                 | ---    | --- |
| `date`              | str    | `YYYY-MM-DD` (ET). |
| `symbol`            | str    | Ticker. |
| `side`              | str    | `buy`. Long-only. |
| `signal_type`       | str    | `vwap_pullback`, `ema_pullback`, `breakout_continuation`, `opening_range_breakout`, `red_to_green`, `premarket_high_break`. |
| `entry_price`       | float  | Fill price. |
| `exit_price`        | float  | Volume-weighted exit across scale-outs. |
| `shares`            | int    | Total shares opened. |
| `pnl`               | float  | Realized USD P&L. |
| `rr_ratio`          | float  | Realized R-multiple relative to initial stop distance. |
| `hold_time_minutes` | float  | Open → full close. |
| `entry_time`        | str    | `HH:MM:SS` (ET) of entry. |
| `exit_time`         | str    | `HH:MM:SS` (ET) of final exit. |
| `exit_reason`       | str    | `target_1r`, `target_2r`, `stop_loss`, `trailing_stop`, `hard_time_exit`, `shutdown`, `circuit_breaker_halt`, etc. |
| `notes`             | str    | Optional free-form text. Usually empty. |

The full entry datetime is reconstructed as `date + " " + entry_time`;
same for `exit_time`. See `load_journal` in `alpha_report.py`.


## Join assumptions

`alpha_report.build_report()` joins the three datasets using
`pandas.merge_asof` with:

- `by="symbol"`  — a trade can only match an alpha row with the same ticker.
- `direction="nearest"` — closest alpha row to each trade's entry.
- `tolerance=Timedelta(minutes=TOLERANCE)` — configurable via
  `--tolerance` (default 5 minutes).

The `decision_log.csv` and `alpha_scores.csv` rows share the same
`timestamp` by construction (both are written inside a single
`_log_decision` call in `main.py`), so they can be treated as 1:1
aligned without a fuzzy join. For speed the analysis layer currently
only needs the alpha-side denormalized copies of `gap_pct`, `regime`,
etc., so it does not re-join to `decision_log.csv` for stats — but
the datasets are kept in lockstep so a future analysis can do so.

The join to `journal.csv` is fuzzy: an execution path (broker confirm
latency, scale-outs, etc.) can drift an entry timestamp slightly
beyond the instant the alpha score was computed. 5 minutes is a
conservative bound that still excludes unrelated trades in the same
symbol on a different day.


## Timestamp assumptions

- All timestamps are **US/Eastern** wall-clock. The trading bot
  imposes this via `trading_bot.utils.helpers.now_et`; the journal
  inherits it via `PortfolioManager` (also ET).
- No explicit timezone is stored in the CSVs — DO NOT load these into
  a UTC-only pipeline without re-localizing.
- `timestamp` uses ISO format without fractional seconds:
  `YYYY-MM-DD HH:MM:SS`.
- The journal separates `date` and `entry_time` / `exit_time` — the
  analysis layer reconstructs the full datetime.
- `merge_asof` requires both sides sorted. The loaders sort
  internally so callers don't have to.


## Rotation

By default all Core datasets are written to their canonical paths
(`data/decision_log.csv`, `data/alpha_scores.csv`, `data/journal.csv`)
and grow indefinitely. For long-running shadow deployments this is
usually fine, but can make downstream analysis slow.

Phase 2.7 adds **optional daily rotation** for the two Core files:

- Env var: `TRADING_DATA_ROTATION=daily` → writes go to
  `data/decision_log_YYYY-MM-DD.csv` and
  `data/alpha_scores_YYYY-MM-DD.csv`, where the date is re-evaluated
  per-write (so midnight rollover works without a bot restart).
- Env var: `TRADING_DATA_ROTATION=none`, unset, or any other value
  → **default behaviour** — canonical path, no suffix.

Explicit constructor argument `rotation="daily"` takes precedence
over the env var and is used by tests. This is **opt-in** so any
existing deployment and any existing analysis script continues to
work without change. `journal.csv` is not rotated — it is owned by
the existing portfolio manager code and is unchanged.


## Known limitations

- **No timezone in CSVs.** Loaders treat timestamps as naive — the
  analysis module never needs to cross tz boundaries, but an
  external script that compares with UTC must localize explicitly.
- **1:1 alignment with decision log is by construction, not by
  constraint.** If someone edits `main.py` to log an alpha without a
  decision or vice versa, the two files will drift out of lockstep.
- **Score weights are hand-tuned.** See `RuleBasedAlphaScorer`.
  Weights can be changed but that invalidates the historical
  promotion-readiness signal — analyse before and after separately.
- **Fuzzy journal join.** 5-minute tolerance is a heuristic. A
  pathological broker latency (> 5 min) will leave an outcome
  unmatched and hence invisible to the tier/readiness stats.
- **No back-pressure on disk.** Both loggers do a synchronous append
  per decision. At ~25 µs + disk latency per row this is negligible,
  but a failing disk will silently drop rows (errors are logged at
  DEBUG and swallowed by design so the trading loop can never
  crash on analytics I/O).
- **Windowless readiness.** Promotion readiness looks at the entire
  dataset, not a trailing window. To evaluate regime-change or
  model drift, restrict the inputs with a date filter before
  invoking `alpha_report`.


## Promotion rule

Alpha scoring is currently in **shadow mode**. The main trading loop
computes an `AlphaScore` for every decision and writes it to
`alpha_scores.csv`, but never consults it when deciding whether to
submit, size, or block an order. Trading behaviour is byte-identical
to the pre-alpha baseline.

**Alpha must not affect execution until the calibration report
reports `promotion_readiness.status == "ready_for_shadow_filter_test"`
on a dataset of at least `min_required_outcomes` realized trades
(default 100).** This gate exists because:

1. Hand-tuned heuristic weights could be trivially miscalibrated.
   Requiring observed out-performance before trusting them is the
   whole point of the shadow phase.
2. A small sample can flip `promising` ↔ `weak` purely from noise.
   The minimum-sample threshold prevents promoting a 10-trade fluke.

A concrete promotion workflow:

1. Let the bot run in paper / live mode with alpha scoring enabled
   in shadow mode. Leave the data files growing (or rotate daily
   per the section above).
2. Periodically run
   `python -m trading_bot.analysis.alpha_report --min-outcomes 100`
   and inspect the `Promotion readiness` section.
3. While status is `not_ready` or `promising`, leave execution alone.
4. When status reaches `ready_for_shadow_filter_test`, open a PR
   that adds a tier-based filter in `main.py` (e.g. skip
   candidates whose tier is `F`). Do **not** remove or relax any
   existing risk/correlation/advisor check — add the tier filter as
   a new rejection stage, with its own `_log_decision` call so the
   effect is still observable in `decision_log.csv`.
5. If status flips back to `weak` after promotion, revert the
   filter. Never leave an under-performing alpha gate in place.
