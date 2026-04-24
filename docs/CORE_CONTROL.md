# Core Conversion — Control Surface

This document is the operator reference for every environment-variable
switch introduced by the Core conversion phases. All switches are
**opt-in** — the default configuration reproduces the pre-Core bot
byte-for-byte.

See also:
- [`docs/DATASETS.md`](DATASETS.md) — schema of the CSV datasets
  produced by the conversion.
- `trading_bot/core/alpha.py` — alpha scoring + filter implementation.
- `trading_bot/analysis/alpha_report.py` — offline analysis /
  promotion-readiness report.


## At a glance

| Env var                           | Values        | Default | Scope               | What it does |
| ---                               | ---           | ---     | ---                 | --- |
| `TRADING_DATA_ROTATION`           | `daily` / `none` | `none` | Paper + live       | Rotate Core CSVs to `*_YYYY-MM-DD.csv`. (Phase 2.7) |
| `TRADING_ALPHA_FILTER_ENABLED`    | `true` / `false` | `false` | **Paper only**   | Allow the alpha filter to block weak trades. (Phase 3) |
| `TRADING_ALPHA_FILTER_MIN_TIER`   | `A` / `B` / `C` | `B`    | Paper only         | Minimum alpha tier required to pass the filter. (Phase 3) |

An **invalid value** for any switch silently falls back to the default
— a typo must never silently relax a safety rail.


## Phase 3 — paper-only alpha filter gate

The filter is an opt-in gate that can **block** weak trades in paper
mode. It is a tier-floor filter:

```
if active and alpha_tier is weaker than TRADING_ALPHA_FILTER_MIN_TIER:
    convert the would-be buy into a skip
    log reason "alpha_filter_blocked:tier=<tier>:min=<min_tier>"
```

### Safety invariants (enforced by code AND by test)

1. **Live mode ignores the filter entirely.**
   `AlphaFilter.active` is `False` whenever `run_mode != "paper"`.
   If an operator runs `TRADING_ALPHA_FILTER_ENABLED=true` in live,
   the filter logs `alpha_filter_ignored_in_live_mode` **once** at
   startup and all subsequent `check()` calls return `blocked=False`.

2. **Risk engine remains the final authority.**
   The filter is called in `main.py` *after* every existing gate:
   strategy evaluation → risk check → regime/leverage override →
   correlation → advisor. An already-rejected trade never reaches
   the filter. A test in
   `tests/test_alpha_filter.py::TestMainLoopOrdering` verifies the
   source-level ordering so a future refactor cannot silently put
   the filter ahead of risk.

3. **The filter can only BLOCK — never upsize, never approve.**
   `FilterDecision.blocked` is a pure boolean. There is no size
   multiplier or "tier boost" path. If a trade makes it past risk
   it either passes the filter as-is or is converted to a skip.

4. **Default is OFF.** Byte-identical behaviour when the env var is
   unset.


### CSV reason values

When a trade is blocked by the filter, `decision_log.csv` records:

```
alpha_filter_blocked:tier=<TIER>:min=<MIN_TIER>
```

`<TIER>` is the alpha tier the scorer assigned the candidate
(`A`/`B`/`C`/`D`/`F`) and `<MIN_TIER>` is the filter's configured
floor. Downstream analysis can `grep` or `startswith` on
`alpha_filter_blocked` to count how many trades the filter would
have rejected. The reason format is pinned by test so it is safe to
build dashboards on top of it.


### Tier ordering

| Tier | Score range       | Weakness index |
| ---  | ---               | ---            |
| A    | `score ≥ 0.80`    | 0 (strongest)  |
| B    | `0.65 ≤ score`    | 1              |
| C    | `0.50 ≤ score`    | 2              |
| D    | `0.35 ≤ score`    | 3              |
| F    | `score < 0.35`    | 4 (weakest)    |

A candidate passes the filter when
`index(candidate.tier) <= index(min_tier)`. `min_tier="B"` therefore
allows A and B, and blocks C, D, F.

`min_tier` is restricted to A / B / C by design — setting it to D or
F would mean "allow all" or "allow almost all" which is the same as
disabling the filter.


### Recommended workflow

1. **Collect data.** Run the bot with the filter OFF (the default)
   while Phase 2 shadow scoring records every decision plus its
   would-be tier. Use `TRADING_DATA_ROTATION=daily` if the run is
   going to span weeks.

2. **Evaluate readiness.** Run
   `python -m trading_bot.analysis.alpha_report --alpha "data/alpha_scores_*.csv" --decision "data/decision_log_*.csv" --journal data/journal.csv`.
   Inspect the `Promotion readiness` and `Shadow filter simulation`
   sections. You want the readiness status to reach
   `ready_for_shadow_filter_test` **and** a shadow-filter-simulation
   row whose `allowed_win_rate > blocked_win_rate` AND
   `allowed_avg_r_multiple > blocked_avg_r_multiple` across a sample
   of at least `--min-outcomes` trades (default 100).

3. **Enable in paper.** Set
   `TRADING_ALPHA_FILTER_ENABLED=true` and optionally
   `TRADING_ALPHA_FILTER_MIN_TIER=<A|B|C>`, restart the bot in paper
   mode. Watch decision_log.csv for `alpha_filter_blocked:...` rows
   and re-run the report daily to confirm the filter is still
   helping.

4. **Leave live alone.** The filter will be ignored in live even if
   the env var is set. If you later want to promote it to live,
   that becomes Phase 4 and must come with a fresh evaluation —
   do NOT remove the paper-only guard without a ticket.


### Disabling / rolling back

- Unset `TRADING_ALPHA_FILTER_ENABLED` (or set it to `false`) and
  restart. No code changes required.
- There is no state stored by the filter — rollback is instant.

### Failure modes

- **Misconfigured env var** (e.g. `TRADING_ALPHA_FILTER_MIN_TIER=D`):
  silently resolves to `B`. A log line records the resolved value
  at startup.
- **Scorer raises** (should never happen — `RuleBasedAlphaScorer` is
  pure Python with no I/O): would propagate out of `check()` and be
  caught by the per-tick `try/except` in `TradingBot._run_live_loop`,
  counted as a tick error by the health monitor. Filter is inactive
  on the next tick if the error is transient.
- **Alpha scoring weights changed**: invalidates the promotion
  readiness signal. Re-collect at least `min_required_outcomes`
  fresh trades before trusting the filter again.


## Phase 3.3 — alpha performance guardrails

The automated daily report (Phase 3.2) now emits a top-level
`guardrails` block inside `alpha_report_<DATE>.json` and an
`Alpha guardrails:` section inside `alpha_report_<DATE>.txt`. It is
pure post-run classification — no trading-loop behaviour depends on
it, nothing in the filter or scorer is changed.

### Output fields

| Field                | Type        | Description |
| ---                  | ---         | --- |
| `status`             | str         | One of `ok`, `warning`, `critical`, `insufficient_data`. |
| `reasons`            | list[str]   | Human-readable bullets explaining the status. |
| `recommended_action` | str         | Operator instruction matching the status. |

The CLI prints the `status` alongside the file paths, e.g.
`guardrail status: ok`. The bot's shutdown log line
`bot.daily_alpha_report_written` also carries the status via
`daily_report.generated`.

### Classification rules

Evaluated in this order; the first rule that fires wins.

1. **`insufficient_data`** — fewer than
   `GUARDRAIL_MIN_MATCHED_TRADES` (20) matched trades in the joined
   dataset. No further classification is attempted; the recommended
   action is "collect more data".
2. **`critical`** — the A+B row of the shadow-filter simulation
   shows that the trades the filter would KEEP did WORSE than the
   ones it would REJECT, by either of:
   - `allowed_avg_r_multiple < blocked_avg_r_multiple`, or
   - `allowed_win_rate      < blocked_win_rate`.
   Recommended action: disable `TRADING_ALPHA_FILTER_ENABLED` and
   re-run the analysis.
3. **`warning`** — either of:
   - `promotion_readiness.status == "weak"`, or
   - A/B tier `outcome_count` is below
     `min_required_outcomes`.
   Recommended action: review the filter before promotion; keep
   `TRADING_ALPHA_FILTER_ENABLED=false` in any environment that
   matters until warnings clear.
4. **`ok`** — default. Allowed side matches or outperforms blocked
   on both win rate and avg R at the A+B threshold, readiness is
   not weak, and A/B outcome count meets the configured minimum.

The comparison in rule 2 only fires when BOTH sides have realized
outcomes. A threshold where only one side has trades leaves the
critical rule silent — you cannot declare the filter harmful from
a side-less comparison.

### Operational interpretation

- `insufficient_data` is **normal** in the first days of a paper
  run. Do not act on it.
- A sustained `warning` state across multiple days suggests the
  scoring weights or threshold need tuning before promotion.
- A `critical` state is the only signal that demands operator
  action: disable the filter until the next analysis shows a clear
  A+B advantage again.

Guardrails are a diagnostic layer. They can neither enable nor
disable the filter themselves — that is still an explicit operator
decision via the `TRADING_ALPHA_FILTER_ENABLED` env var.


## Phase 2.7 — dataset rotation (reference)

See [`docs/DATASETS.md`](DATASETS.md) for the full spec. Short
version: set `TRADING_DATA_ROTATION=daily` to append to
`data/decision_log_YYYY-MM-DD.csv` and
`data/alpha_scores_YYYY-MM-DD.csv` instead of the canonical paths.
`journal.csv` is not rotated.
