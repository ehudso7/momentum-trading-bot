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
| `TRADING_DATA_ROTATION`                | `daily` / `none` | `none`    | Paper + live  | Rotate Core CSVs to `*_YYYY-MM-DD.csv`. (Phase 2.7) |
| `TRADING_ALPHA_FILTER_ENABLED`         | `true` / `false` | `false`   | **Paper only**| Allow the alpha filter to block weak trades. (Phase 3) |
| `TRADING_ALPHA_FILTER_MIN_TIER`        | `A` / `B` / `C`  | `B`       | Paper only    | Minimum alpha tier required to pass the filter. (Phase 3) |
| `TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL`  | URL string       | *(unset)* | Any           | Optional Slack/Discord webhook for warning/critical guardrail alerts. (Phase 3.4) |
| `TRADING_API_KEY`                      | opaque string    | *(unset)* | SaaS API only | Required bearer token for every protected `/reports` / `/experiments` endpoint. Unset → server refuses all protected traffic. (Phase 4.0) |
| `TRADING_API_REPORTS_DIR`              | path             | `reports` | SaaS API only | Directory holding `alpha_report_<DATE>.json`. (Phase 4.0) |
| `TRADING_API_MANIFEST_PATH`            | path             | `data/alpha_experiments.jsonl` | SaaS API only | Path to the append-only manifest. (Phase 4.0) |
| `TRADING_API_ALLOWED_ORIGINS`          | comma-sep origins | *(unset)* | SaaS API only | Optional CORS allow-list (e.g. `https://app.example.com,https://admin.example.com`). Unset → no cross-origin access. (Phase 4.2) |
| `TRADING_API_RATE_LIMIT_PER_MINUTE`    | positive int     | `60`      | SaaS API only | In-memory per-client-IP rate limit. Invalid values fall back to 60 fail-closed. (Phase 4.2) |
| `TRADING_API_AUDIT_LOG_PATH`           | path             | `data/api_access_audit.jsonl` | SaaS API only | Append-only JSONL file recording one metadata-only record per request. (Phase 4.4) |
| `TRADING_API_PREMIUM_KEYS`             | comma-sep tokens | *(unset)* | SaaS API only | Bearer tokens that grant premium-tier access. Any other accepted token is treated as free tier. (Phase 4.5) |
| `TRADING_API_USAGE_LOG_PATH`           | path             | `data/api_usage.jsonl` | SaaS API only | Append-only JSONL file with one metadata-only record per successful protected request. Raw API keys never written. (Phase 4.6) |
| `STRIPE_API_KEY`                       | `sk_...` secret  | *(unset)* | SaaS API only | Presence toggles Stripe-primary premium classification. Unset → Phase 4.5 env-var list is the only source. (Phase 4.7) |
| `STRIPE_WEBHOOK_SECRET`                | `whsec_...`      | *(unset)* | SaaS API only | HMAC secret used to verify Stripe webhook signatures. Unset → `POST /webhook/stripe` fail-closed 503. (Phase 4.7) |
| `STRIPE_PRICE_ID_PREMIUM`              | `price_...`      | *(unset)* | SaaS API only | Informational — operator-facing reference to the premium product. Not read by this codebase. (Phase 4.7) |
| `TRADING_STRIPE_PREMIUM_CACHE_PATH`    | path             | `data/stripe_premium_keys.json` | SaaS API only | Persistent JSON list of opaque API-key strings with active subscriptions. Survives restarts. (Phase 4.7) |
| `TRADING_API_CONVERSION_LOG_PATH`      | path             | `data/api_conversions.jsonl` | SaaS API only | Append-only JSONL of free → paid conversion events. Hashed keys only — no PII, no card data. (Phase 4.9) |
| `TRADING_API_GROWTH_LOG_PATH`          | path             | `data/api_growth.jsonl`      | SaaS API only | Append-only JSONL of `?ref=<code>` referral events. Dedup'd per (hash, ref) within 24h. Hashed keys only. (Phase 5.1) |
| `TRADING_API_KEYS_MANIFEST_PATH`       | path             | `data/api_keys_manifest.jsonl` | SaaS API only | Append-only JSONL of operator-issued keys (Phase 6.0/6.1). Server reads it as an auth source — keys whose hash appears here authenticate without env-var edits. Hashed keys only. (Phase 6.2) |
| `TRADING_API_KEYS_REVOKED_PATH`        | path             | `data/api_keys_revoked.jsonl`  | SaaS API only | Append-only JSONL of revocation events. A revoked hash is rejected with 403 even when the same raw key also matches `TRADING_API_KEY` or appears in the Stripe cache. Hashed keys only. (Phase 6.2) |

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


## Phase 3.4 — optional webhook alerts

Set `TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL` to a Slack or Discord
incoming-webhook URL to be notified when the daily guardrail
classification is `warning` or `critical`. Alerts fire once per
daily report — at shutdown and at the midnight rollover — not on
every tick. The URL is read at report time so an operator can flip
it on or off without a restart.

### Safety invariants

1. **Off by default.** When the env var is unset, no network call
   is made and behaviour is byte-identical to Phase 3.3.
2. **`ok` and `insufficient_data` are silent.** Only `warning` and
   `critical` trigger a POST. Daily "nothing to see here" pings
   would train operators to ignore the channel.
3. **Alerts never fail the report.** HTTP errors, connection
   errors, DNS failures, and timeouts are all caught. The text and
   JSON report files are written before the POST is attempted, so
   file creation is unaffected by network issues. The failure
   reason is recorded on `DailyReportResult.alert_error` and in the
   structured log `guardrail_alert.request_error` /
   `guardrail_alert.non_success`.
4. **No auto-toggling.** This phase does **not** flip
   `TRADING_ALPHA_FILTER_ENABLED` on or off — even `critical`
   alerts merely inform the operator.

### Payload shape

Compatible with both Slack (`text`) and Discord (`content`). Extra
structured fields are ignored by chat providers but are useful for
any generic webhook consumer.

```json
{
  "text":    "🚨 Alpha guardrail *CRITICAL* for 2026-04-24\nRecommended action: disable TRADING_ALPHA_FILTER_ENABLED and re-run the analysis\nReasons:\n• allowed avg R -0.433 < blocked avg R 1.000 at the A+B threshold\n• allowed win rate 0.00% < blocked win rate 100.00% at the A+B threshold\nText report: reports/alpha_report_2026-04-24.txt\nJSON report: reports/alpha_report_2026-04-24.json",
  "content": "...same as text...",
  "report_date": "2026-04-24",
  "status": "critical",
  "recommended_action": "disable TRADING_ALPHA_FILTER_ENABLED and re-run the analysis before re-enabling — the filter is rejecting better trades than it keeps",
  "reasons": [
    "allowed avg R -0.433 < blocked avg R 1.000 at the A+B threshold",
    "allowed win rate 0.00% < blocked win rate 100.00% at the A+B threshold"
  ],
  "text_report_path": "reports/alpha_report_2026-04-24.txt",
  "json_report_path": "reports/alpha_report_2026-04-24.json"
}
```

### Rollback

- **To disable alerts:** unset `TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL`
  or set it to an empty string, then restart the bot (or simply
  wait until the next report runs — the URL is read at report time,
  not cached). No code changes required.
- **To temporarily silence a noisy channel:** change the webhook URL
  to a known-dead endpoint; alerts will fail silently and the
  `guardrail_alert.non_success` log will record why.
- **To roll back the feature entirely:** revert the Phase 3.4 commit
  and the `requests` dependency calls — `trading_bot/reporting/
  daily_report.py` is the only touched file. Since the feature is
  fully opt-in (webhook URL unset by default), no cleanup of saved
  state is required.


## Phase 3.5 — scorer fingerprint + drift guard

Every alpha score is now stamped with a SHA-256 fingerprint of the
`RuleBasedAlphaScorer` configuration that produced it — weights,
tier cutoffs, and the regime-score map. This lets the calibration
pipeline detect when historical data was produced under a different
set of weights than what the bot is currently running, which
invalidates any comparison.

This is metadata / CI protection only. The fingerprint does not
affect scoring, filtering, execution, risk, or any other live-trade
code path.

### What's fingerprinted

`trading_bot.core.alpha.get_alpha_scorer_config()` returns the
exact dict that is hashed:

- `scorer`: always `"RuleBasedAlphaScorer"` in Phase 3.5.
- `weights`: `{gap, rvol, vol, regime, confidence, reason}`.
- `tier_thresholds`: `{A, B, C, D}` — the lower bounds used by
  `score_to_tier`.
- `regime_scores`: per-regime multiplier map.

Serialization uses `json.dumps(..., sort_keys=True, separators=(",",":"))`
so whitespace cannot flip the hash, and the fingerprint is stable
across processes, machines, and Python versions.

Helpers:
- `get_alpha_scorer_config() -> dict`
- `get_alpha_scorer_fingerprint() -> str` (64-char hex)

### Where it's surfaced

1. **`alpha_scores.csv`** — new optional trailing column
   `scorer_fingerprint`. Stamped per row on every write. Old CSV
   files without the column still load (see "Backward compat"
   below).
2. **`alpha_report_<DATE>.json`** — top-level field
   `scorer_fingerprint` is the CURRENT scorer's fingerprint at
   report time, and `scorer_fingerprints_in_data` is the sorted
   unique set of fingerprints observed in the source alpha file(s).
3. **`alpha_report_<DATE>.txt`** — the header now includes a line
   `Scorer fingerprint: <64-hex>` so operators can see the value
   without opening the JSON.

### Drift detection

`evaluate_guardrails` now receives a `scorer_fingerprints` list
(injected by `generate_daily_report` from the alpha file(s)):

- **Multiple distinct fingerprints present.**
  The guardrail raises `status="warning"` with reason
  `"multiple alpha scorer fingerprints detected (N distinct) — …"`.
  Sample data spans more than one weight configuration;
  promotion-readiness cannot be trusted across the boundary.

- **Fingerprint column missing entirely.**
  The guardrail raises `status="warning"` with reason
  `"alpha scorer fingerprint unavailable in source data — …"`.
  This happens when the data was produced by a pre-Phase-3.5 bot.

Drift is **never** a `critical` condition on its own. The documented
ordering is unchanged — `critical` rules (allowed vs blocked A+B
metrics) win over any drift finding; `insufficient_data` short-
circuits before drift is even examined.

### Backward compatibility

- `trading_bot.analysis.alpha_report.load_alpha_scores` still reads
  CSVs without the `scorer_fingerprint` column; legacy rows are
  returned untouched.
- `_collect_alpha_fingerprints` returns `[]` for files that lack
  the column, which then triggers the documented "unavailable"
  warning rather than a crash.
- Globs can mix old and new files freely — both flow through the
  same loader and the unique-fingerprint set is computed from
  whatever rows supply one.

### Example fingerprint

On the Phase 3.5 default weights the fingerprint is a 64-char
hexadecimal hash (example value — will differ if constants are
retuned):

```
24b6f9a1c5f7e0d3a8b2c9e4f60d7a31b8c5e2f9a7d03b61c8e5f2a9d7b30415
```

Example JSON excerpt:

```json
{
  "report_type": "daily_alpha_validation",
  "report_date": "2026-04-24",
  "scorer_fingerprint": "24b6f9a1c5f7e0d3…",
  "scorer_fingerprints_in_data": ["24b6f9a1c5f7e0d3…"],
  "guardrails": {
    "status": "ok",
    "reasons": ["…"],
    "recommended_action": "…"
  }
}
```

### CI protection

If you change any weight or threshold in `RuleBasedAlphaScorer`
you should expect:
- Every row from the first restart onward carries the new
  fingerprint.
- The next daily report lists both old and new fingerprints in
  `scorer_fingerprints_in_data` and emits the drift-warning
  guardrail.
- Operators know to re-collect calibration before trusting the
  promotion-readiness signal.


## Phase 3.6 — alpha experiment manifest

Every daily report now appends one JSONL record to
`data/alpha_experiments.jsonl` describing the exact state under
which that report was produced: scorer fingerprint, full scorer
config, env vars in play, guardrail outcome, A+B shadow-filter
summary, git commit, and the paths of the report files.

This is **append-only audit metadata**. Nothing in the trading
loop reads the manifest back. No secret is ever persisted: the
webhook URL is stored only as a boolean
`TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL_present`.

### Contents of a record

```json
{
  "timestamp": "2026-04-24T21:03:15",
  "report_date": "2026-04-24",
  "git_commit": "8c61963d3becf76c218724c49340221d1a378611",
  "scorer_fingerprint": "8c61963d3becf76c218724c49340221d1a3786110cf15a610fe5549a41b77a8b",
  "scorer_config": {
    "scorer": "RuleBasedAlphaScorer",
    "weights": {"gap": 0.2, "rvol": 0.25, "vol": 0.15,
                "regime": 0.1, "confidence": 0.25, "reason": 0.05},
    "tier_thresholds": {"A": 0.8, "B": 0.65, "C": 0.5, "D": 0.35},
    "regime_scores": {"trending_bullish": 1.0, "range_bound": 0.6,
                      "low_volatility": 0.5, "high_volatility": 0.4,
                      "trending_bearish": 0.2}
  },
  "env": {
    "TRADING_ALPHA_FILTER_ENABLED": "true",
    "TRADING_ALPHA_FILTER_MIN_TIER": "B",
    "TRADING_DATA_ROTATION": "daily",
    "TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL_present": true
  },
  "report_paths": {
    "text": "data/alpha_reports/alpha_report_2026-04-24.txt",
    "json": "data/alpha_reports/alpha_report_2026-04-24.json"
  },
  "totals": {"alpha_rows": 120, "buy_rows": 25, "skip_rows": 95,
             "matched_trades": 24, "journal_trades": 24},
  "promotion_readiness": {"status": "promising", "outcome_count": 24, "...": "..."},
  "guardrails": {"status": "ok", "reasons": ["..."],
                 "recommended_action": "no action — continue to monitor"},
  "shadow_filter_ab_summary": {
    "threshold": "A+B",
    "allowed_buy_count": 18, "blocked_buy_count": 7,
    "allowed_outcome_count": 16, "blocked_outcome_count": 7,
    "allowed_win_rate": 0.6875, "blocked_win_rate": 0.2857,
    "allowed_avg_pnl": 48.75, "blocked_avg_pnl": -22.14,
    "allowed_avg_r_multiple": 0.81, "blocked_avg_r_multiple": -0.43
  }
}
```

### Safety

- **Never stores secrets.** The webhook URL is listed only as a
  boolean suffixed `_present`. A substring check in the Phase 3.6
  tests asserts no raw URL ends up in the serialized record.
- **Never fails the report.** Every I/O path (git rev-parse,
  JSON serialization, file open/write) is wrapped; a failure sets
  `DailyReportResult.manifest_error` and leaves
  `DailyReportResult.success` unchanged so the operator still
  gets the text / JSON report.
- **Thread-safe.** Writes are serialized through a module-level
  `threading.Lock` so concurrent in-process callers cannot
  interleave JSONL rows. A separate test (`test_thread_safe_
  concurrent_writes`) verifies 50 threads produce exactly 50
  valid lines.

### Inspection CLI

```bash
# Last 10 records (pretty-printed)
python -m trading_bot.reporting.experiment_manifest --tail 10

# All records as one JSON object per line
python -m trading_bot.reporting.experiment_manifest --tail 0 --json-lines

# Alternate manifest path (useful in test / staging)
python -m trading_bot.reporting.experiment_manifest \
    --manifest path/to/alpha_experiments.jsonl --tail 5
```

### Programmatic access

`from trading_bot.reporting.experiment_manifest import read_manifest`
returns a list of record dicts with the same `tail` semantics,
skipping malformed lines so a partial write can never make
history unreadable.


## Phase 4 — SaaS Boundary Rules

The Core bot (scanner / strategy / risk / execution / portfolio / alpha
scoring / filter) stays **private** on the trading host. A separate,
strictly **read-only** analytics layer (`trading_bot.api.server`)
can be exposed to an external SaaS surface without exposing any
trading decision or scoring internal.

### What the API MAY expose

- Aggregated daily statistics: tier / reason / regime stats,
  decile calibration, shadow-filter simulation rows, totals.
- Guardrail classification and recommended action.
- Promotion-readiness status.
- The scorer **fingerprint** hash (a 64-char opaque string).
- The append-only experiment manifest, minus server-side paths
  and minus the scorer weight breakdown.

### What the API MUST NOT expose

- Trading execution. There is no POST / PUT / PATCH / DELETE
  endpoint and no path that could be interpreted as an order or
  trade hook — enforced by
  `tests/test_api_server.py::TestBoundaryEnforcement`.
- Alpha scoring weights, tier thresholds, regime-score maps —
  i.e., the content of `get_alpha_scorer_config()`. The
  sanitizer strips `scorer_config` from every response.
- Filesystem paths embedded in report sources or manifest
  `report_paths`. Stripped before serialization so the hosting
  layout cannot be inferred.
- Any live-decision state. The API never imports
  `trading_bot.core.alpha`, `trading_bot.main`, or any module
  under `execution/`, `portfolio/`, `risk/`, `scanners/`, or
  `strategies/`. A structural test enforces this at source level.
- Raw secrets. The upstream `snapshot_env()` already redacts the
  webhook URL to a presence boolean; the API further scrubs
  fields that shouldn't cross the boundary.

### Endpoints

| Method | Path                       | Auth | Description |
| ---    | ---                        | ---  | --- |
| GET    | `/`                        | No   | Public product / status page (Phase 4.3). |
| GET    | `/health`                  | No   | Liveness probe. |
| GET    | `/reports/latest`          | Yes  | Most recent daily report (sanitized). |
| GET    | `/reports/{date}`          | Yes  | Report for `YYYY-MM-DD` (sanitized). |
| GET    | `/experiments/recent?limit=N` | Yes | Last N manifest records (sanitized). |
| GET    | `/experiments/{n}`         | Yes  | Nth-most-recent manifest record (1 = most recent). |
| GET    | `/dashboard`               | Yes  | Read-only HTML dashboard (Phase 4.1). |

All non-`/health` endpoints require
`Authorization: Bearer <TRADING_API_KEY>`. Unset `TRADING_API_KEY`
on the server → every protected endpoint returns 503; this is a
deliberate fail-closed default.

### Running

```bash
export TRADING_API_KEY=<random secret>
# optional overrides
export TRADING_API_REPORTS_DIR=reports
export TRADING_API_MANIFEST_PATH=data/alpha_experiments.jsonl

uvicorn trading_bot.api.server:app --reload --host 0.0.0.0 --port 8000
```

Interactive docs are available at `/docs` and `/redoc`; they do not
bypass authentication.

### Deployment posture

- The API is designed to run on a separate host from the Core
  trading bot, or at least as a separate process that only has
  read access to the reports / manifest files.
- Because the API never writes to disk and never imports Core,
  compromising the API process cannot affect live trading.
- Rate limiting / DDOS protection are expected to come from the
  surrounding infrastructure (reverse proxy, API gateway). The
  server itself caps `/experiments/recent?limit=` at 100.

### Phase 4.1 — read-only dashboard (`/dashboard`)

An HTML page rendered server-side from the same sanitized helpers
the JSON endpoints use. No new frontend framework, no build step —
the page is a single self-contained HTML document with an inline
`<style>` block.

**Same auth as the JSON endpoints.** Bearer-token required;
`/dashboard` obeys the identical 401/403/503 rules.

**Sections (in order):**

1. Header + generation timestamp.
2. *Latest report* — date + scorer-fingerprint hash, guardrail
   block (status badge, recommended action, reasons), promotion
   readiness (A/B vs C/D/F outcomes table), totals, shadow-filter
   simulation table.
3. *Recent experiments* — last 10 manifest rows, newest first,
   with per-row guardrail + readiness badges and a truncated
   fingerprint.

**Empty states** (each rendered at HTTP 200):

- No reports on disk → "No daily reports available yet."
- No manifest → "(no experiments recorded yet)".
- Malformed report JSON → dashboard falls back to the empty-report
  state; the page still renders.

**Leakage guards (enforced by tests):**

- `scorer_config` is never present (the same sanitizer as the
  JSON endpoints is applied first).
- Filesystem paths from `sources` (`path`, `resolved_paths`) and
  from `report_paths` are stripped before rendering.
- Raw `TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL` never appears — even
  if a future bug caused the upstream manifest to leak one, the
  sanitizer drops `report_paths` and the Phase 3.6 manifest only
  stores a presence boolean for the webhook URL.
- Page has no `<form>`, no `<input>`, no `<button>`, no
  `onclick` / `onsubmit` handlers, no `method="POST|PUT|PATCH|DELETE"`.
  The dashboard is a read-only document.
- All dynamic strings are HTML-escaped via `html.escape`, so a
  compromised upstream file cannot inject `<script>` tags.

### Phase 4.2 — deployment hardening

The API process is designed to be safe to deploy publicly behind a
single Bearer API key. Phase 4.2 adds four overlapping safeguards
— all driven by env vars so ops can flip them without a restart.

#### Security headers

Every response (including 2xx, 401, 403, 404, 429, and 503) carries:

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline';
                         script-src 'none'; img-src 'self' data:;
                         frame-ancestors 'none'; base-uri 'none';
                         form-action 'none'
```

`script-src 'none'` makes it impossible to execute any JavaScript
even if an upstream data file were to sneak a tag past the
dashboard's HTML escaping. `frame-ancestors 'none'` plus
`X-Frame-Options: DENY` stops clickjacking. The dashboard's inline
`<style>` is whitelisted via `style-src 'unsafe-inline'`; scripts
are still forbidden.

#### CORS

Controlled by `TRADING_API_ALLOWED_ORIGINS`. When unset (default),
cross-origin requests get no CORS headers at all and preflights
return 403 — the browser won't let a third-party page call the API.

When set to a comma-separated allow-list, each match causes the
server to add:

```
Access-Control-Allow-Origin: <echoed origin>
Vary: Origin
```

Preflights (`OPTIONS`) for allowed origins return:

```
204 No Content
Access-Control-Allow-Origin: <origin>
Access-Control-Allow-Methods: GET, HEAD, OPTIONS
Access-Control-Allow-Headers: Authorization
Access-Control-Max-Age: 600
```

Disallowed origins' preflights return 403. Only `GET` is advertised
because the API exposes no mutating verbs.

#### Rate limiting

Fixed 60-second window, keyed by resolved client IP
(`X-Forwarded-For` first entry wins when present; otherwise the
socket peer). In-memory map per process; multi-process deployments
should rely on the reverse-proxy / WAF for cross-worker limits.

- Default: 60 requests per IP per minute.
- Override via `TRADING_API_RATE_LIMIT_PER_MINUTE=<positive int>`.
- Any invalid value — empty, non-numeric, negative, zero, NaN,
  inf, float — **falls back to 60**. A typo must never open the
  server to unlimited traffic.
- Over-limit requests are rejected with:
  ```
  HTTP/1.1 429 Too Many Requests
  Retry-After: <seconds until window reset>
  Content-Type: application/json
  {"detail": "rate limit exceeded"}
  ```

#### Request logging

One structured log line per request, tagged `api.request`. Fields:

| Field       | Source |
| ---         | --- |
| `method`    | `request.method` |
| `path`      | `request.url.path` |
| `status`    | final response status code |
| `duration_ms` | wall-clock handler time, rounded to 2 decimals |
| `client_ip` | `X-Forwarded-For` first entry, else socket peer |

The middleware **never** touches the `Authorization` header. A test
captures the log stream while an authenticated request is issued
with a unique-marker token and asserts the token does not appear
anywhere in the captured output.

#### Deployment env var summary

Minimum safe production deployment:

```bash
# Required
export TRADING_API_KEY="<32+ random chars>"

# Optional hardening
export TRADING_API_ALLOWED_ORIGINS="https://app.example.com"
export TRADING_API_RATE_LIMIT_PER_MINUTE="120"

# Optional pointing
export TRADING_API_REPORTS_DIR="/srv/analytics/reports"
export TRADING_API_MANIFEST_PATH="/srv/analytics/data/alpha_experiments.jsonl"

uvicorn trading_bot.api.server:app \
    --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers
```

#### What Phase 4.2 does NOT do

- It does not add any new endpoint.
- It does not add any mutating verb — `TestPhase42BoundaryUnchanged`
  re-asserts every Phase 4.0 invariant after the middleware stack
  is in place.
- It does not import any Core module.
- It does not expose `scorer_config`, raw paths, or secrets.
- It does not automate or enable TLS — a reverse proxy (Caddy,
  nginx, Cloudflare) must terminate TLS in front of the app.

### Phase 4.3 — public product / status landing page

`GET /` now serves a publicly-accessible HTML page that explains the
analytics product without exposing a single byte of protected
content. Intended as both a marketing-style product page and a
"healthy deployment" confirmation for operators.

**Fully static by construction.** The handler returns the output of
`render_landing_page_html()`, a pure function with no I/O:

- Does not read any report file.
- Does not read the experiment manifest.
- Does not inspect env vars.
- Does not call a database or subprocess.

Because there is no dynamic substitution, the page cannot leak
scorer_config, filesystem paths, bearer tokens, report data,
experiment data, or anything else live. The Phase 4.3 tests plant
unique-marker strings into the reports directory and manifest,
request `/`, and assert none of the markers appear in the body.

**Content (read-only positioning, no operational data):**

1. "What this is" — the analytics-layer elevator pitch.
2. "Read-only alpha analytics" — A/B/C/D/F tiers, aggregated stats,
   shadow-filter simulation — WITHOUT ever gating a live trade.
3. "Guardrail monitoring" — names the four statuses
   (`ok` / `warning` / `critical` / `insufficient_data`) and explains
   what they classify.
4. "Daily validation reports" — one text + JSON pair per day.
5. "Experiment audit trail" — the Phase 3.6 append-only manifest.
6. "Protected dashboard" — operators with an API key can visit
   `/dashboard`; no login UI, no API-key hint beyond that sentence.
7. "Safety invariants" — bulleted summary of what the service will
   never do (execute, simulate, write to disk, import Core).
8. Footer — one-liner "all analytics endpoints require a Bearer API key".

**What the landing page MUST NOT contain** (enforced by tests):

- Live report or experiment data (any field value pulled from disk).
- Strings like `scorer_config`, `GAP_WEIGHT`, `TIER_A_MIN`.
- Filesystem paths (`/srv/`, `/var/lib/`, `/tmp/`, `/data/...`).
- The env-var name `TRADING_API_KEY`.
- Any `<form>`, `<input>`, `<button>`, `onclick`, `onsubmit`,
  `onchange`, or `method=POST|PUT|PATCH|DELETE`.
- Any `<script>` or `javascript:` URL — consistent with the CSP
  `script-src 'none'` directive.
- Phrases that imply operator controls: "place trade", "execute
  trade", "submit order", "enable/disable filter", "run simulation",
  "start bot", etc.

**Protected endpoints are unchanged.** `TestLandingPageIsPublic::
test_protected_endpoints_still_require_auth_after_root_exists`
explicitly re-asserts that every `/reports/*`, `/experiments/*`,
and `/dashboard` request without a Bearer token still returns 401.

### Phase 4.4 — access audit trail

Every request — public or protected, 2xx or 4xx or 429 or 500 — now
appends one metadata-only JSONL record to
`TRADING_API_AUDIT_LOG_PATH` (default `data/api_access_audit.jsonl`).
Requests also return a response header `X-Request-ID` for client
correlation.

#### Record shape

| Field              | Source |
| ---                | --- |
| `timestamp`        | `datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')` |
| `method`           | `request.method` |
| `path`             | `request.url.path` |
| `status_code`      | final response status |
| `duration_ms`      | wall-clock handler time, rounded to 2 decimals |
| `client_ip`        | `X-Forwarded-For` first entry, else socket peer |
| `authenticated`    | `False` when status ∈ {401, 403, 503}; else `True` |
| `user_agent_hash`  | first 32 hex chars of `SHA256(UA)`; `null` when no UA |
| `request_id`       | sanitized `X-Request-ID` header, else UUID4 hex |

Example (protected endpoint rejected with wrong key):

```json
{
  "timestamp": "2026-04-24T16:26:40.427876Z",
  "method": "GET",
  "path": "/reports/latest",
  "status_code": 403,
  "duration_ms": 2.63,
  "client_ip": "203.0.113.7",
  "authenticated": false,
  "user_agent_hash": "b9f43238f762d9e026e2765701a55ee0",
  "request_id": "f50be7067948454d874e63a5732201eb"
}
```

#### Leakage guards (enforced by tests)

- **Authorization header is never written.** Tests issue requests
  with a unique-marker Bearer token, on both success and failure
  paths, and assert the token, the word `Bearer`, and the word
  `Authorization` never appear in the audit file.
- **User-Agent is hashed, not stored.** The raw UA string never
  enters the audit file. A SHA-256 truncated to 32 hex chars
  stands in.
- **Report / experiment bodies are never written.** Planted marker
  strings inside upstream report JSON cannot appear in the audit
  log — the audit writer only records metadata about the request.
- **Client-supplied X-Request-ID is sanitized.** Only characters
  in `[A-Za-z0-9\-_:.]` survive; length is capped at 64. Anything
  stripped to empty triggers a UUID4 generation. Tests feed in
  `<script>`, newlines, null bytes, and 500-character strings and
  assert none of it lands in the log or the response header.

#### Operational behaviour

- **Best-effort.** Every I/O failure path is caught. If the audit
  file cannot be written (permissions, disk full, directory
  missing), the request still returns normally; the failure is
  logged at DEBUG via structlog. `TestAuditFailureDoesNotFailRequest`
  stubs the writer to raise and asserts the request still succeeds.
- **Thread-safe.** Writes are serialized through a module-level
  `threading.Lock`.
- **Parent directory auto-created.** Pointing
  `TRADING_API_AUDIT_LOG_PATH` at a non-existent nested directory
  causes the writer to `mkdir(parents=True)` on first append.

#### X-Request-ID response header

Every response carries the same `request_id` stored in the audit
record. Operators can use this to match a log entry, an audit row,
and a client-side trace.

### Phase 4.5 — access tier gating (free vs premium)

Two access tiers controlled by Bearer-token classification:

- **Free** — any accepted token that is NOT in
  `TRADING_API_PREMIUM_KEYS`. Includes the legacy single
  `TRADING_API_KEY` value when no premium env is set.
- **Premium** — any token in `TRADING_API_PREMIUM_KEYS` (comma-
  separated allow-list). Premium implicitly authenticates: a
  premium-only deployment can leave `TRADING_API_KEY` unset.

Both tiers must still send a valid `Authorization: Bearer <token>`
header. Tier classification only changes which DATA the caller is
allowed to read.

#### Per-endpoint limits

| Endpoint                       | Free                                  | Premium |
| ---                            | ---                                   | --- |
| `GET /reports/latest`          | allowed (sanitized)                   | allowed |
| `GET /reports/{date}`          | only the last `MAX_FREE_TIER_DAYS` (3) days; older → 403 | any date |
| `GET /experiments/recent`      | silent cap at `MAX_FREE_TIER_EXPERIMENTS` (3); explicit `?limit>3` → 403 | up to `?limit=100` |
| `GET /experiments/{n}`         | `n ≤ 3`; otherwise → 403              | any `n` (subject to manifest length) |
| `GET /dashboard`               | hides `Shadow filter simulation`; experiment table capped at 3 rows; "free tier" upgrade note shown | full layout |

The free-tier date window uses the API process's UTC date. Today
plus the previous two days are accepted. A future date is rejected
the same way an old date is.

#### Error shape

When a free-tier request exceeds a limit:

```
HTTP/1.1 403 Forbidden
Content-Type: application/json

{"detail": "upgrade required for full access"}
```

#### Helpers

- `_is_premium(api_key) -> bool` — True iff the key is listed in
  `TRADING_API_PREMIUM_KEYS`.
- `_enforce_free_limits(*, is_premium, date_requested=None,
  n_experiments=None, explicit_limit=None) -> None` — raises 403
  with the documented message when a free-tier request exceeds the
  per-endpoint cap. Premium requests are short-circuit no-ops.
- `_premium_keys_set() -> set[str]` — env-var parser used by
  `_is_premium` and `require_api_key`.

#### Deployment recipes

```bash
# Free-only public deployment (anyone with the shared key gets free tier)
export TRADING_API_KEY="shared-free-key"

# Mixed: free for the shared key, premium for whitelisted enterprise keys
export TRADING_API_KEY="shared-free-key"
export TRADING_API_PREMIUM_KEYS="ent-key-acme,ent-key-globex,ent-key-initech"

# Premium-only deployment (no free tier)
unset TRADING_API_KEY
export TRADING_API_PREMIUM_KEYS="ent-key-acme,ent-key-globex"
```

#### Out of scope

- No payment processing. Tier promotion is operator-driven (edit
  the env var, rotate keys).
- No mutating endpoints — the boundary tests still enforce
  GET/HEAD/OPTIONS only after Phase 4.5.

### Phase 4.6 — per-key usage metrics

Every **successful** protected request appends one JSONL record to
`TRADING_API_USAGE_LOG_PATH` (default `data/api_usage.jsonl`).
Public endpoints (`/`, `/health`) and requests that failed auth
(401 / 403 / 503) are NOT recorded here — they remain visible in
the Phase 4.4 audit trail.

The raw API key is never written. It is anonymized by
`_hash_api_key(api_key)` which returns the first 32 hex characters
of `SHA-256(api_key)`. The same key always maps to the same hash,
so records cluster by caller for adoption measurement, but the
mapping is one-way and the raw token never leaves memory.

#### Record shape

| Field          | Source |
| ---            | --- |
| `timestamp`    | `datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')` |
| `key_hash`     | `SHA-256(api_key)[:32]` |
| `tier`         | `"free"` or `"premium"` — classification at request time |
| `method`       | `request.method` |
| `path`         | `request.url.path` |
| `status_code`  | final response status |
| `duration_ms`  | wall-clock handler time, rounded to 2 decimals |
| `request_id`   | matches the `X-Request-ID` returned on the response |

#### Example

```json
{
  "timestamp": "2026-04-24T18:30:14.501824Z",
  "key_hash": "4a55ea81e1313e68a8feea48e98110bc",
  "tier": "free",
  "method": "GET",
  "path": "/reports/latest",
  "status_code": 200,
  "duration_ms": 4.23,
  "request_id": "a18664d06c464ccab2eca45706f2aff3"
}
```

#### Leakage guards (enforced by tests)

- **Raw keys never in the file.** Tests issue authed requests with
  unique-marker tokens (both free and premium) and assert the token
  strings never appear in the usage log.
- **Attempted tokens never in the file.** A 403 with a unique-marker
  Bearer value produces **zero** usage records — only successful
  auth counts — so rejected tokens can never be scraped from the
  usage log.
- **`Authorization` / `Bearer` header tokens never in the file.**
  Tests scan the entire file for the header words.
- **Report/experiment bodies never in the file.** A planted marker
  inside a report JSON that a caller requests is confirmed absent
  from the usage log — the writer records metadata only.

#### Operational behaviour

- **Best-effort write.** Every I/O path is caught — if the file
  cannot be written, the request still returns normally.
  `TestUsageWriteFailureDoesNotFailRequest` stubs the writer to
  raise and confirms HTTP 200 is still returned.
- **Thread-safe.** Writes are serialized through a module-level
  `threading.Lock` (`_usage_write_lock`, separate from the audit
  lock).
- **Parent directory auto-created** on first append.
- **Authenticated-only.** The usage middleware reads the validated
  token from `request.state.api_key`, which is set by
  `require_api_key` on success. Failed auth paths never set that
  attribute, so they are guaranteed to produce no usage record.
- **Does not double-count.** Each request produces at most one
  record; public-path skipping is based on exact path match against
  `_PUBLIC_PATHS_NO_USAGE = {"/", "/health"}`.

#### Out of scope

- No payment tracking, no billing, no per-user contact info.
- No rate-limiting decisions made from usage data — the Phase 4.2
  rate limiter is still purely per-IP.
- No cross-record correlation beyond what `request_id` and
  `key_hash` already enable for downstream pipelines.

### Phase 4.7 — Stripe billing integration (`POST /webhook/stripe`)

Promotes users from **free → premium automatically** when they
start an active Stripe subscription, and revokes premium the
moment Stripe tells us the subscription ended or payment failed.

**Never stores card data, PAN, CVV, emails, names, or any other
sensitive payment field.** The only thing persisted locally is a
JSON list of opaque API-key strings whose owners currently have an
active subscription. A dedicated test plants PII fields into a
webhook body and asserts none of them appear in the persisted
cache file or the webhook response body.

Implementation avoids adding a `stripe` pip dependency: signature
verification is the documented Stripe v1 scheme
(`HMAC-SHA256(secret, f"{t}.{body}")`) implemented with stdlib
`hmac` + `hashlib`, so failure modes and replay protection are
well-understood without a library surface.

#### How API keys map to Stripe customers

The Stripe customer metadata **must** include `api_key=<user_key>`.
When issuing a subscription via Stripe Checkout or Billing Portal,
also set `subscription_data.metadata.api_key = <user_key>` so the
value is copied onto the subscription object — webhooks do not
expand the customer by default, and we deliberately do not call
Stripe's API on the critical webhook path.

Without this metadata, the webhook silently ignores the event
(`action: "ignored", reason: "no_api_key_on_event"`). The
subscription is still valid at Stripe's end, but the SaaS API
does not know which user it belongs to until the metadata is
corrected and a subsequent event (e.g., `subscription.updated`)
carries it.

#### `POST /webhook/stripe`

Request:
- Headers: `Stripe-Signature: t=<ts>,v1=<hmac>` (required).
- Body: raw JSON exactly as Stripe sends it (no client mutation).

Responses:
- `200 {"received": true, "action": "added|removed|ignored", "type": <event_type>}`
- `400 {"detail": "invalid webhook signature"}` — bad HMAC or stale timestamp.
- `400 {"detail": "invalid webhook payload"}` — body is not valid JSON.
- `503 {"detail": "billing webhook not configured"}` — `STRIPE_WEBHOOK_SECRET` unset.

Handled event types:

| Event                              | Behaviour |
| ---                                | --- |
| `customer.subscription.created`    | Adds `api_key` to the premium cache iff `status in {active, trialing}`. |
| `customer.subscription.deleted`    | Removes `api_key` from the premium cache (idempotent). |
| `invoice.payment_failed`           | Removes `api_key` immediately (fail-closed on billing failure). |
| *(anything else)*                  | `action: "ignored", reason: "unhandled_type"` — no side effects. |

#### Precedence (when both are configured)

```
is_premium(api_key):
    if Stripe configured AND api_key in Stripe cache  → True
    if api_key in TRADING_API_PREMIUM_KEYS            → True   (operator override)
    else                                              → False  (free tier)
```

The env-var list continues to work even when Stripe is configured,
so operators / enterprise accounts can be granted access without
going through the billing flow.

#### Fallback when Stripe is not configured

- `STRIPE_API_KEY` unset → Phase 4.5 semantics exactly: premium is
  determined entirely by `TRADING_API_PREMIUM_KEYS`. The webhook
  endpoint still exists but returns 503 if called (nothing to
  verify against).

#### Safety invariants (enforced by tests)

- **Tamper rejection.** A single-byte change to the body makes the
  HMAC fail and the webhook returns 400.
- **Replay rejection.** Signed timestamps outside the 300-second
  tolerance window are rejected.
- **Secret rotation.** Changing `STRIPE_WEBHOOK_SECRET` invalidates
  all in-flight deliveries — they all fail 400 immediately.
- **No Core imports.** `tests/test_billing.py::TestBillingBoundary`
  greps `trading_bot/api/billing.py` for forbidden imports.
- **No mutating routes except this one.** Every boundary test
  carves out exactly `POST /webhook/stripe` — any other mutating
  verb on any route causes the test to fail.
- **No sensitive data on disk.** The cache file contains only
  opaque `api_key` strings; planted PII / card-number markers in
  webhook bodies are asserted absent.
- **Not recorded in the per-key usage log.** The Stripe webhook is
  a system-to-system call — it carries no Authorization header
  and no caller-owned API key, so the Phase 4.6 usage middleware
  correctly skips it.

#### Setup instructions for Stripe

1. In the Stripe Dashboard → Developers → API keys, copy the
   secret key (`sk_live_...` for production) and set it as
   `STRIPE_API_KEY` on the server.
2. In Developers → Webhooks, create a new endpoint pointing at
   `https://your-host/webhook/stripe`. Select events:
   `customer.subscription.created`,
   `customer.subscription.deleted`,
   `invoice.payment_failed`.
   Copy the Signing secret (`whsec_...`) and set it as
   `STRIPE_WEBHOOK_SECRET`.
3. Create the premium product and copy the price ID to
   `STRIPE_PRICE_ID_PREMIUM` (informational).
4. When creating a Checkout Session, set
   `customer_creation=always` plus
   `subscription_data.metadata.api_key=<user_key>` so the
   webhook event carries the user identity. Also set
   `customer.metadata.api_key=<user_key>` as a defence-in-depth
   fallback.
5. Restart the server. `POST /webhook/stripe` now accepts
   signed Stripe deliveries and routes subscription state
   into the premium-key cache.

#### Rollback

- Unset `STRIPE_API_KEY` → server falls back to the env-var
  premium list. The webhook starts returning 503 (no more
  subscription events accepted). Existing Stripe-granted access
  persists until the operator deletes the cache file.
- Delete `data/stripe_premium_keys.json` → all Stripe-granted
  premium access is revoked. A restart reloads the (now empty)
  cache.
- Revert the Phase 4.7 commit → the `/webhook/stripe` endpoint
  disappears and the boundary tests revert to "no mutating
  verbs anywhere". Existing integrations keep working because
  nothing in the trading loop depends on billing state.

### Phase 4.8 — operator-only Stripe Checkout link generator

An operator CLI that mints a Stripe Checkout URL the operator can
email / message to a customer so the customer can self-serve
upgrade to premium. **No public endpoint is added** — checkout
generation is deliberately not exposed via the dashboard or any
FastAPI route, and the test suite explicitly asserts no
`/checkout`, `/subscribe`, `/upgrade`, or `/billing/checkout`
route was introduced. Existing "POST only on /webhook/stripe"
boundary tests continue to pass.

#### Function: `create_checkout_session(api_key, success_url, cancel_url)`

Lives in `trading_bot.api.billing`. Reads:

- `STRIPE_API_KEY` — Stripe secret key. Required.
- `STRIPE_PRICE_ID_PREMIUM` — premium price id. Required.
- (Does NOT read `STRIPE_WEBHOOK_SECRET` — webhooks and checkout
  are independent concerns.)

Builds a Stripe Checkout Session payload with:

- `mode=subscription`
- `line_items[0][price]=<STRIPE_PRICE_ID_PREMIUM>`, `quantity=1`
- `success_url`, `cancel_url` (operator-supplied, HTTP header
  injection characters are rejected)
- `customer_creation=always`
- `metadata[api_key]=<api_key>` AND
  `subscription_data[metadata][api_key]=<api_key>` — so the
  Phase 4.7 webhook handler can promote this api_key to premium
  regardless of whether Stripe expands the customer in the
  delivered event.

Return value (caller-safe — raw `api_key` is **never** included):

```python
{
    "checkout_session_id": "cs_test_abcdef…",
    "checkout_url":        "https://checkout.stripe.com/c/pay/cs_test_…",
    "api_key_hash":        "<32 hex chars of SHA-256>",
}
```

Fail-closed exceptions:

- `BillingConfigError` — `STRIPE_API_KEY` or
  `STRIPE_PRICE_ID_PREMIUM` is unset.
- `BillingAPIError` — Stripe returned non-2xx, bad JSON, or
  the network failed. The error message never includes the raw
  `api_key`.
- `ValueError` — missing / whitespace-only `api_key`,
  `success_url`, or `cancel_url`; newline / control characters
  in either URL.

HTTP transport:

- Uses `requests.post` with HTTP Basic auth `(STRIPE_API_KEY, "")`
  against `https://api.stripe.com/v1/checkout/sessions` — matches
  the Stripe SDK's wire protocol.
- No Stripe SDK dependency is added.
- `http_post` is an injectable keyword argument for tests so
  the entire function can be exercised offline.

#### CLI

```bash
python -m trading_bot.api.billing checkout \
    --api-key <user-api-key> \
    --success-url https://app.example.com/billing/success \
    --cancel-url https://app.example.com/billing/cancel
```

Sample output (success):

```
checkout_url:         https://checkout.stripe.com/c/pay/cs_test_a1b2c3…
checkout_session_id:  cs_test_a1b2c3d4e5f6
api_key_hash:         223e7ccef94c4d39c9c54bbc61d3b051
```

Sample output (missing env):

```
error: STRIPE_API_KEY is not configured
```

Exit codes:

| Code | Meaning |
| ---  | --- |
| `0`  | Checkout URL created. |
| `2`  | Configuration error (missing env var, bad URL). |
| `3`  | Stripe API error (non-2xx, bad JSON, network failure). |

The raw `--api-key` value is **never** printed to stdout or
stderr — the CLI prints only the `checkout_url`, session id,
and hashed `api_key_hash`. A dedicated test runs the CLI as a
subprocess with a uniquely-marked api key and asserts the marker
is absent from both streams.

#### Safety invariants (enforced by tests)

- **No new FastAPI route.** `TestPhase48NoNewApiRoute` iterates
  `app.routes` and fails if any path contains `/checkout`,
  `/subscribe`, `/upgrade`, or `/billing/checkout`.
- **Mutating verbs still limited to `POST /webhook/stripe`.** The
  Phase 4.0 – 4.7 boundary is re-asserted.
- **Raw API key never persisted.** `create_checkout_session`
  returns only the hashed form. The only on-the-wire usage of
  the raw key is sent directly to Stripe via HTTPS; it never
  enters a log, a report, or the Phase 4.6 usage file.
- **URL injection rejected.** Success / cancel URLs containing
  `\r`, `\n`, `\t`, or `\x00` are rejected with `ValueError`
  before any network call is made.
- **No Core imports.** Billing module grep — still zero matches
  for `trading_bot.core`, `trading_bot.main`, etc.

#### When to use

- Manual concierge upgrades: customer reaches out to support,
  operator runs the CLI, emails the checkout URL back.
- Internal staging or demo environments where operators want to
  invite specific testers to premium without building a public
  flow.
- As a stopgap before Phase 4.9 (which may expose a public,
  authenticated checkout endpoint guarded by rate-limiting and
  per-user quotas).

### Phase 4.9 — free → paid conversion tracking

Appends one privacy-preserving JSONL event to
`TRADING_API_CONVERSION_LOG_PATH` every time a Stripe subscription
goes active (or trialing) for an API key we haven't previously
counted as converted. Combined with the Phase 4.6 per-key usage
log, this is enough to compute:

- **Free users** (usage log, `tier == "free"` rows) — count of
  distinct `key_hash`.
- **Premium users** (usage log, `tier == "premium"` rows) — count
  of distinct `key_hash`.
- **Conversion events** (this log, `event == "converted"`) — count
  of distinct `api_key_hash`.
- **Conversion rate** = conversions / distinct-free-keys.
- **Time-to-convert** = `timestamp` − `first_seen_timestamp` for
  each record whose `first_seen_timestamp` is populated.

#### Record shape

| Field                  | Description |
| ---                    | --- |
| `timestamp`            | `datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')` of the webhook event |
| `api_key_hash`         | `SHA-256(api_key)[:32]` — matches the Phase 4.6 usage-log hash exactly so BI pipelines can join the two files |
| `event`                | Always `"converted"` in Phase 4.9. Reserved slot for future lifecycle events. |
| `source`               | `"stripe"` today; reserved for future sources (`"manual"`, `"promo"`, etc.) |
| `price_id`             | Extracted from `items.data[0].price.id` (or the legacy `plan.id`) — identifies the product, not the customer |
| `first_seen_timestamp` | Earliest timestamp where this `api_key_hash` appears in the usage log; `null` when no prior usage recorded |

Example (end-to-end, from a live webhook):

```json
{
  "timestamp": "2026-04-24T19:14:08.069220Z",
  "api_key_hash": "033014d328ab4d22057093c2e3912b11",
  "event": "converted",
  "source": "stripe",
  "price_id": "price_premium_monthly_usd_2999",
  "first_seen_timestamp": "2026-04-01T08:30:00.000000Z"
}
```

This user's time-to-convert is therefore 23 days ≈ 23 × 86 400 s.

#### Dedup semantics

Per `api_key_hash`: once a conversion event has been recorded for a
key, subsequent `subscription.created` deliveries (retries,
reactivations after cancellation, plan changes) are **deduped** and
do NOT create a new row. This matches the analytics question "did
this user ever convert?" rather than "how often did they
resubscribe?".

The in-memory dedup set is rehydrated from disk on first call after
a process restart, so the semantics survive redeploys.

Concurrent calls on the same key race under a single
`threading.Lock` — at most one thread ever writes a row for a
given hash. A 20-thread stress test confirms this.

#### Integration point

`trading_bot.api.billing.handle_webhook_event` calls
`trading_bot.api.conversion.record_conversion` inside a try/except
whenever a `customer.subscription.created` event upgrades an API
key to premium. The call is strictly best-effort: any exception
during conversion tracking is caught, logged at DEBUG, and swallowed
so the webhook response is unaffected (a dedicated test stubs
`record_conversion` to raise and asserts the webhook still returns
`action: "added"`).

#### Privacy invariants (enforced by tests)

- **No raw API key on disk.** Planted unique-marker tokens in
  webhook bodies never appear in the conversion file.
- **No PII.** Planted `customer.email`, `customer.name`,
  `customer.metadata.pan`, `customer.metadata.cvv` in webhook
  bodies never appear in the conversion file (only the opaque
  `api_key_hash` and the non-sensitive `price_id` survive).
- **No `Authorization` header, no webhook secret, no Stripe API
  key** is ever written.
- **Hash alignment.** The conversion log's `api_key_hash` equals
  `SHA-256(api_key)[:32]` — identical to
  `trading_bot.api.server._hash_api_key` and
  `trading_bot.api.billing._hash_api_key` — a dedicated test
  verifies all three produce the same digest.

#### What you can compute once this is flowing

```
free users                = distinct(usage_log.key_hash) where tier="free"
premium users             = distinct(usage_log.key_hash) where tier="premium"
conversions               = distinct(conversion_log.api_key_hash)
conversion rate           = conversions / free-user count
avg time-to-convert (s)   = mean(conversion.timestamp
                                 - conversion.first_seen_timestamp)
conversions by price_id   = count(*) group by conversion.price_id
```

All of it is plain SQL / pandas on two JSONL files — no database,
no PII, no card data.

### Phase 5.1 — growth loop tracking

Records one JSONL event to `TRADING_API_GROWTH_LOG_PATH` whenever
an **authenticated** caller hits any endpoint with a `?ref=<code>`
query parameter. Pair this with the Phase 5.0 pricing report to
identify which referral / distribution sources drive actual usage
and conversion — instead of just "pageviews" from marketing tools.

No new env var required for the feature to work; it's always on.
Events are append-only JSONL with the same schema discipline as
every other Core-conversion log (hashed keys only, no PII).

#### Record shape

| Field          | Description |
| ---            | --- |
| `timestamp`    | UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ` |
| `api_key_hash` | `SHA-256(api_key)[:32]` — identical scheme to server / billing / conversion / usage logs, so this file JOINs with all of them on one column |
| `ref_code`     | Sanitized `?ref=` value — only `[A-Za-z0-9\-_:.]` survives, capped at 64 chars |
| `path`         | `request.url.path` (HTTP route, not any query-string content) |
| `request_id`   | Matches the `X-Request-ID` returned on the response, so a single request can be traced across audit + usage + growth |

Example record:

```json
{
  "timestamp": "2026-04-18T10:00:00.000000Z",
  "api_key_hash": "0f0dc1c3ff361d4e3948d6fa7752e7f6",
  "ref_code": "hn_launch",
  "path": "/reports/latest",
  "request_id": "a18664d06c464ccab2eca45706f2aff3"
}
```

#### Dedup policy

The same `(api_key_hash, ref_code)` pair is recorded at most ONCE
per sliding 24-hour window. A user who reloads the same
`?ref=hn_launch` URL a hundred times counts as one. A user who
clicks two distinct ref codes (`hn_launch` then `twitter_q2`)
produces two rows.

Dedup survives process restarts: on first call the in-memory
cache rehydrates from recent rows in the file.

Constants:

- `DEDUP_WINDOW_HOURS = 24`
- `DEDUP_WINDOW_SECONDS = 86_400`

#### Integration point

`trading_bot.api.server.growth_middleware` runs after
`usage_middleware` (so it sees the validated
`request.state.api_key`) but before `cors_middleware`. If
`?ref=` is present AND the caller is authenticated, it calls
`trading_bot.api.growth.record_growth_event(...)` inside a
try/except so any write failure is logged at DEBUG and
swallowed. `test_does_not_break_request_if_growth_write_fails`
stubs the writer to raise and asserts the response is still 200.

#### Safety / privacy invariants (enforced by tests)

- **No raw API key on disk.** Planted unique-marker token in the
  API key → absent from the growth file.
- **ref_code sanitized before persistence.** `<script>`-shaped
  input is stripped of `<`, `>`, and any other non-safe char
  before anything is written. Tests:
  `test_bogus_ref_chars_sanitized_in_file`.
- **No `Authorization`, `Bearer`, `email`, `password`, `pan`,
  `cvv`, or `card` words in the file.** Not added by this module,
  and the autouse test sweeps the file for all of them.
- **Public endpoints don't record.** `/health?ref=...` never
  creates a row because `require_api_key` never runs for
  public paths, so `request.state.api_key` stays unset.
- **Unauthenticated requests don't record.** 401 responses on
  protected endpoints produce no growth rows either.
- **No Core imports.** Boundary test re-greps `growth.py` for
  `trading_bot.core.*`, `execution`, `portfolio`, `risk`,
  `scanners`, `strategies`, `main` — zero matches.

#### CLI

```
python -m trading_bot.api.growth --summary
python -m trading_bot.api.growth --summary --json
python -m trading_bot.api.growth --summary --top 5
python -m trading_bot.api.growth --summary --path path/to/file.jsonl
```

Sample text output:

```
Total growth events    = 10
Distinct ref codes     = 3
Distinct unique users  = 9

  ref_code                           events  unique_users   paths
  ---------------------------------------------------------------
  hn_launch                               5             5       1
  twitter_q2                              3             3       1
  newsletter_apr                          2             2       1
```

Sample JSON output:

```json
{
  "total_events": 10,
  "distinct_refs": 3,
  "distinct_users": 9,
  "refs": [
    {"ref_code": "hn_launch",      "events": 5, "unique_users": 5, "paths": 1},
    {"ref_code": "twitter_q2",     "events": 3, "unique_users": 3, "paths": 1},
    {"ref_code": "newsletter_apr", "events": 2, "unique_users": 2, "paths": 1}
  ]
}
```

#### What you can compute with this

- **Unique users per ref code** — drives "which distribution channel
  actually brings new active users" (vs. just bot / click traffic).
- **Paths per ref code** — how many different endpoints a source's
  users end up touching. Deep engagement signal.
- **Overlap between refs** — join the growth log with itself on
  `api_key_hash` to see users who arrived via both `hn_launch` and
  `twitter_q2`.
- **Conversion per ref** — join the growth log with the Phase 4.9
  conversion log on `api_key_hash`; count how many users in each
  ref bucket also appear as `event == "converted"`. Now you have
  a per-channel conversion rate with no PII, no tracking pixel,
  no third-party analytics.


### Phase 5.2 — landing-page conversion optimisation

`GET /` is still fully public and unauthenticated, but the page is
now optimised to convert visitors into API-key holders instead of
just describing the product. All prior Phase 4.3 invariants hold:
no forms, no JavaScript, no disk reads, no env-var lookups, no
leaking of reports / manifest data. The only runtime input is the
optional `?ref=<code>` query parameter.

Page structure (five `<section>` blocks):

1. **Hero** — tagline _"See which trades your system should have
   taken — before risking money."_, the existing "Read-only SaaS
   layer. No trading. No execution." sub-line, and (when a ref
   query param was supplied) a one-line `Invited by: <code>…</code>`
   banner.
2. **How it works** — three-step ordered list: scoring →
   publishing daily validation reports → guardrail + audit trail.
3. **Example output** — a small illustrative table showing a
   report date, a `status-ok` guardrail badge, a Tier × Rows ×
   Allowed × Blocked table, and a two-row shadow-threshold table.
   The section header explicitly labels the content as
   _"Illustrative snapshot — not live data."_ so a visitor cannot
   confuse it with a live feed.
4. **Upgrade** — a three-row Free vs Premium comparison table
   (daily validation reports, experiment audit trail, protected
   dashboard) that mirrors the Phase 4.5 access-tier caps (free =
   last 3 days, premium = full history). Followed by two hardcoded
   soft-conversion cues: _"Most users upgrade after ~7 days"_ and
   _"Premium users run 3–5x more requests than free users"_. Both
   cues are compile-time constants — they are not derived from the
   live usage / conversion logs, so the public page carries zero
   data leak risk.
5. **Get started** — CTA copy pointing at operator-issued Bearer
   API keys.

**Ref query param.** The handler reads `request.query_params.get("ref")`
and sanitises it with the same helper the growth middleware uses
(`trading_bot.api.growth._sanitize_ref_code`). That helper strips
every character outside `[A-Za-z0-9\-_:.]` and caps the result at
64 characters. The sanitised value is HTML-escaped as defence in
depth before being echoed inside a single `<code>` element in the
hero banner. Concretely, this means:

- `?ref=twitter-launch_2026` → banner reads
  `Invited by: twitter-launch_2026`.
- `?ref=<script>alert(1)</script>` → banner reads
  `Invited by: scriptalert1script` — the payload is neutralised.
- `?ref=` (empty) → no banner rendered.
- `?ref=<500 chars>` → echoed value is capped at 64 chars.

Because the sanitiser is the same one the growth logger uses, the
page has a _what-you-see-is-what-gets-logged_ property: if the
banner shows a character, so does the audit record, and vice
versa.

**Determinism.** `render_landing_page_html(ref_code: str = "")` is
a pure function of its sanitised argument. Called with the same
ref code twice it returns byte-identical HTML; called with no ref
(or empty string) it returns byte-identical HTML to the legacy
handler shape. The body builder `_landing_page_body(ref_code)` has
the same contract.

**Safety invariants (unchanged + re-tested in Phase 5.2):**

- No `<form>`, `<input>`, `<button>`, `onclick`, `onsubmit`, or
  `method="POST|PUT|PATCH|DELETE"` anywhere on the page.
- No `<script>` tag, no `javascript:` URI.
- No execution-adjacent terms (`place trade`, `submit order`,
  `start bot`, `run simulation`, …) in the rendered copy.
- No leaking of `scorer_config`, `TRADING_API_KEY`, env-var names,
  or file paths — even when a ref param is supplied and reports /
  manifest are populated with unique-marker fixtures.
- The only non-read HTTP verb on the entire SaaS API is still
  `POST /webhook/stripe` (Phase 4.7).

**No new env vars.** Phase 5.2 is a pure presentation change;
operators tune it by changing the source. The legacy env vars
(`TRADING_API_REPORTS_DIR`, `TRADING_API_MANIFEST_PATH`, etc.) are
not read by the landing handler.


### Phase 5.4 — free-tier daily usage caps

Controlled friction for free-tier callers, intended to lift
conversion rate without affecting premium behaviour. Both caps
are per-API-key / per-UTC-day and are fully reversible via env
vars — unset both vars and the server reverts to the pre-Phase-5.4
surface.

**Env vars**

| Variable | Default | Effect |
|---|---|---|
| `TRADING_FREE_MAX_REQUESTS_PER_DAY` | `50` | Max total protected requests per free-tier key per UTC day. Excess → `429`. |
| `TRADING_FREE_MAX_REPORT_CALLS` | `10` | Stricter cap on `/reports/*` calls for free-tier keys. Excess → `403`. |

Both resolvers are **fail-closed**: any non-positive-int value
(`""`, `"abc"`, `"-1"`, `"0"`, `"1.5"`, `"nan"`) is rejected and
the documented default is used instead. A typo cannot disable the
cap.

**Response headers (free-tier only).** Every free-tier response —
success, 403, or 429 — carries:

    X-Free-Tier-Usage: <current>/<limit>
    X-Free-Tier-Remaining: <remaining>

Premium responses never carry these headers, so a client SDK can
trivially tell which tier it's on without a separate /tier
endpoint.

**Rejection bodies**

    HTTP/1.1 429 Too Many Requests
    Content-Type: application/json
    X-Free-Tier-Usage: 50/50
    X-Free-Tier-Remaining: 0

    {"detail": "free tier limit reached — upgrade for continued access"}

    HTTP/1.1 403 Forbidden
    Content-Type: application/json
    X-Free-Tier-Usage: 2/500
    X-Free-Tier-Remaining: 498

    {"detail": "upgrade required for full access"}

**Surfaces NOT affected (strict invariants, tested):**

1. Premium users — exempted before any count is loaded. Premium
   requests never touch the usage log read path here.
2. Public paths `/` and `/health` — same "fully unauthenticated"
   contract as Phase 4.0.
3. `POST /webhook/stripe` — system-to-system call, never a free
   user. Exempt regardless of what the free-tier caller count is.
4. Anonymous / unknown-key requests — pass through to
   `require_api_key`, which still returns `401` / `403` /
   `503`. The free-tier middleware never returns a response for
   such requests (so the headers cannot be used as an
   account-exists oracle).

**Counter.** `_count_free_tier_usage_today(key_hash)` scans the
Phase 4.6 usage log (`TRADING_API_USAGE_LOG_PATH`, default
`data/api_usage.jsonl`) and returns
`(total_today, report_calls_today)`. A report call is any row
whose `path` starts with `/reports/`. Corrupt lines, missing
file, and I/O errors all degrade gracefully to zeros — we prefer
to let a request through than block a paying user on a disk
outage.

**Dashboard nudge.** When `/dashboard` is rendered for a free
user, a yellow banner appears above the first report block:

    You're using the free tier — upgrade for full access.

Premium users see no banner. The banner is plain HTML — no JS,
no form, no CTA link — so it adds zero attack surface and
respects the "read-only" invariant from Phase 4.1.


### Phase 5.5 — upgrade CTA telemetry

Records one JSONL event every time a free-tier caller encounters
a conversion-relevant friction point. Purely additive — no
premium behaviour changes, no request ever blocks or slows down
on a telemetry failure, and no raw API key ever lands on disk.

**Event log**

    data/api_upgrade_events.jsonl   (default path)

Env var: `TRADING_API_UPGRADE_EVENTS_LOG_PATH`.

**Event names (exact strings, emitted only when `tier == "free"`)**

| Event | Fires when |
|---|---|
| `dashboard_banner_seen` | `/dashboard` renders for a free user |
| `daily_request_limit_hit` | Phase 5.4 global 429 fires on any protected path |
| `report_limit_hit` | Phase 5.4 403 fires on `/reports/*` |
| `old_report_blocked` | Phase 4.5 rejects `/reports/{date}` outside the 3-day window |
| `experiment_limit_blocked` | Phase 4.5 rejects `/experiments/{n}` or `/experiments/recent?limit=…` over `MAX_FREE_TIER_EXPERIMENTS` |

**Record schema**

    {
      "timestamp":     "2026-04-24T14:23:11.500000Z",
      "api_key_hash":  "3bcae9a335c1e77f182d8d02372f2f89",
      "event":         "dashboard_banner_seen",
      "path":          "/dashboard",
      "request_id":    "3a2f8b0e1c2d4ef9",
      "tier":          "free",
      "ref_code":      "twitter-q2"
    }

* `api_key_hash` is `SHA-256(api_key)[:32]` — identical to the
  hash used by server/billing/conversion/growth, so this file
  joins cleanly against all four on a single column.
* `ref_code` reuses the Phase 5.1 growth-log sanitiser
  (`[A-Za-z0-9\-_:.]`, capped at 64 chars); missing or empty
  becomes `null`.
* `request_id` is the same id that appears on the response's
  `X-Request-ID` header (Phase 4.4) — operators can correlate a
  user report with the exact telemetry row.
* `tier` is always the constant string `"free"` (premium callers
  never reach an emission site; the middleware and gate both
  short-circuit before that).

**Safety invariants (tested)**

* Raw API keys are **never** persisted; only the opaque hash.
* Telemetry is best-effort: disk failure, serialisation error, or
  a broken parent directory all return silently — the caller's
  request still succeeds, and the failure surfaces via structlog
  at DEBUG.
* Thread-safe JSONL append via a module-level `threading.Lock`.
* Unknown event names are dropped silently.
* Unauthenticated requests, the Stripe webhook, and premium
  callers never produce a row.
* The upgrade_events module imports nothing from Core (verified
  by source-grep test) and nothing from any other api module at
  import time (server.py does the call via a lazy import so the
  SaaS boundary stays unambiguous).

**CLI**

    python -m trading_bot.api.upgrade_events --summary
    python -m trading_bot.api.upgrade_events --summary --json
    python -m trading_bot.api.upgrade_events --summary \
        --path data/api_upgrade_events.jsonl \
        --top-paths 10 --top-refs 10

Summary output (text or JSON) includes:

* `total_events` — total rows read.
* `events` — per-event count and unique users.
* `top_paths` — the paths that fire events most often.
* `ref_codes` — per-`ref_code` count, unique users, and number
  of distinct events; present only when at least one row carried
  a ref_code.

**BI joins (reference)**

Join `data/api_upgrade_events.jsonl` with:

* `data/api_usage.jsonl` (Phase 4.6) on `(api_key_hash == key_hash)`
  — measure how many free users hit each friction point relative
  to request volume.
* `data/api_conversions.jsonl` (Phase 4.9) on `api_key_hash`
  — compute per-event conversion rate ("of users who saw the
  banner, how many upgraded?").
* `data/api_growth.jsonl` (Phase 5.1) on
  `(api_key_hash, ref_code)` — attribute each friction hit back
  to the channel the user arrived from.

No PII. No paths leaking raw secrets. Reversible by simply
ignoring the log file.


### Phase 5.7 — dynamic free-tier nudge copy

The three free-tier upgrade prompts the server emits — the
`/dashboard` banner, the Phase 5.4 daily-request `429`, and the
Phase 5.4 report-limit `403` — are now operator-tunable via env
vars. Premium behaviour is untouched, no API endpoints change,
and unsetting all three env vars reverts the server to the
pre-Phase-5.7 surface byte-for-byte.

**Env vars + defaults**

| Env var | Surface | Default copy |
|---|---|---|
| `TRADING_UPGRADE_BANNER_COPY` | `<p class="free-tier-banner">` on `/dashboard` (free tier only) | `You're using the free tier — upgrade for full access` |
| `TRADING_LIMIT_HIT_COPY` | `429 detail` from `free_tier_middleware` | `free tier limit reached — upgrade for continued access` |
| `TRADING_REPORT_LIMIT_COPY` | `403 detail` from `free_tier_middleware` on `/reports/*` | `upgrade required for full access` |

**Resolver semantics** (`_resolve_nudge_copy`):

1. env unset → default
2. value strips to `""` → default
3. value contains any ASCII control character (NUL..0x1F or 0x7F,
   incl. tab / newline / CR) → default
4. value > `MAX_NUDGE_COPY_LENGTH` (180 chars) → truncated to 180

The returned string is **raw**. Callers MUST escape on output:

* the dashboard banner runs the resolved copy through
  `html.escape` before inserting into `<p class="free-tier-banner">`;
* the `429` / `403` paths place the resolved copy inside a
  `JSONResponse` `{"detail": ...}` body, where FastAPI's JSON
  encoder handles escaping.

A test asserts the dashboard renders `<script>alert(1)</script>` as
`&lt;script&gt;alert(1)&lt;/script&gt;` (no live tag).

**What is NOT touched by Phase 5.7**

* Phase 4.5's `/reports/{date}` (out-of-window date) and
  `/experiments/*` (over-cap) `403`s — semantically distinct
  from "you ran out of report calls today" and intentionally
  keep their `upgrade required for full access` literal. A
  dedicated test (`test_phase45_403s_keep_legacy_copy`) pins
  this contract.
* Premium responses — the dashboard renders no banner element
  for premium users, regardless of how the env var is set. The
  custom copy never appears anywhere in a premium response.
* The copy does NOT propagate into telemetry. The Phase 5.5
  upgrade-events JSONL only stores the event name (e.g.
  `dashboard_banner_seen`), the user hash, the path, and the
  request_id — never the rendered copy. So an operator can A/B
  the banner without polluting the audit trail.

**Examples**

Custom banner element rendered for a free user:

    <p class="free-tier-banner">Like what you see? Upgrade to unlock the full audit trail.</p>

Custom `429` response after a free user exhausts the daily cap:

    HTTP/1.1 429 Too Many Requests
    content-type: application/json
    x-free-tier-usage: 1/1
    x-free-tier-remaining: 0

    {"detail":"You hit your daily request budget — upgrade for unlimited access."}

**Reversibility.** `unset TRADING_UPGRADE_BANNER_COPY
TRADING_LIMIT_HIT_COPY TRADING_REPORT_LIMIT_COPY` restores the
default messages immediately on the next request — no restart
needed; the resolver runs per-request.


### Phase 5.8 — nudge copy performance report

Two changes in one phase, both additive and reversible:

1. The Phase 5.5 upgrade-events log gains an optional
   `copy_variant_hash` field. The three "carries-copy" events
   record the hash of whichever Phase 5.7 nudge string was
   actually rendered to the user.
2. A new offline analysis module groups the events log by that
   hash to measure which copy variants drive paid conversions.

**`copy_variant_hash` field (writer side)**

Computed as `SHA-256(resolved_copy)[:32]`. The raw copy is
**never** persisted — `_hash_copy_variant` is called inside
`record_upgrade_event`, the hash lands on the row, and the
input string is dropped at function exit. Pinned by
`test_raw_copy_is_never_persisted`.

The field is set on:

* `dashboard_banner_seen` — hash of `_upgrade_banner_copy()`
* `daily_request_limit_hit` — hash of `_limit_hit_copy()`
* `report_limit_hit` — hash of `_report_limit_copy()`

Other events (`old_report_blocked`, `experiment_limit_blocked`)
have no operator-tunable copy and record `copy_variant_hash:
null`. Pinned by `TestPhase58OtherEventsHaveNullHash`.

`record_upgrade_event` accepts an optional `copy_variant=None`
kwarg. Calls that pre-date Phase 5.8 still work — the row simply
records `copy_variant_hash: null` (backward compat is pinned by
`test_backward_compat_call_without_copy_variant`).

**Same-string contract.** The 429 and 403 paths resolve the
copy ONCE per request and pass the same string to both the
JSON response body and the telemetry hasher. So
`SHA-256(response.body.detail)[:32] == row.copy_variant_hash`
on every recorded rejection — pinned by
`test_429_event_carries_copy_hash_matching_response_body` and
its 403 counterpart.

**Analysis module — `trading_bot.analysis.nudge_report`**

Inputs (paths overridable on the CLI):

    data/api_upgrade_events.jsonl   (Phase 5.5/5.8)
    data/api_conversions.jsonl      (Phase 4.9)

Per `copy_variant_hash` row:

    {
      "copy_variant_hash":            "<32-hex>",
      "event_count":                  12,
      "unique_users":                 8,
      "converted_users":              3,
      "conversion_rate":              0.375,
      "delta_samples":                3,
      "median_time_to_convert_seconds": 432000.0,
      "p90_time_to_convert_seconds":   518400.0,
      "median_time_to_convert_days":   5.0,
      "p90_time_to_convert_days":      6.0,
      "events":                       ["dashboard_banner_seen"]
    }

Time delta = `(earliest conversion ts) − (earliest event ts for
THIS variant_hash)`, per converted user. Negative deltas are
dropped.

Headline pick: `strongest_variant` returns the variant with the
highest `conversion_rate`, gated by
`MIN_SUPPORT_FOR_RANKING = 2` distinct users (override via
`--min-users-for-ranking`). Ties break by hash alphabetically
for byte-determinism.

**Privacy invariants (tested):**

* No raw API key on disk — both inputs already hash.
* No raw copy text on disk — Phase 5.8 hashes at write time.
* The per-variant report row deliberately **omits**
  `api_key_hash` so BI pipelines can't derive which users saw
  which variant from the report alone
  (`test_per_variant_row_does_not_include_api_key_hash`).
* Boundary: `nudge_report.py` imports nothing from Core or
  `trading_bot.api.*`. Pinned by source-grep test.

**CLI**

    python -m trading_bot.analysis.nudge_report
    python -m trading_bot.analysis.nudge_report --json-only
    python -m trading_bot.analysis.nudge_report --text-only
    python -m trading_bot.analysis.nudge_report \
        --events data/api_upgrade_events.jsonl \
        --conversions data/api_conversions.jsonl \
        --reports-dir reports \
        --date 2026-04-24 \
        --min-users-for-ranking 5

Writes `reports/nudge_report_<DATE>.{txt,json}`.

**Operator workflow.** Operators correlate the hash back to the
copy variant via their own deployment notes (e.g. the env-var
value at the time of rollout). They never need access to the
raw copy through this pipeline — and cannot get it from disk.


### Phase 5.9 — landing page visual polish

`GET /` is still public, static, and deterministic — but now
ships a polished SaaS-style design instead of a plain document
layout. Every Phase 4.3 / 5.2 / 5.7 invariant continues to hold.

**Design layout (5 `<section>` blocks unchanged):**

1. **Hero** — gradient background (CSS `linear-gradient`),
   centred headline + tagline + sub-line, plus the optional
   `<p class="ref">Invited by: <code>…</code></p>` banner inside
   the hero when `?ref=` is present.
2. **How it works** — three-step `<ol class="feature-grid">`
   styled as numbered cards via CSS `counter()`, single column
   on mobile, three columns at ≥ 600 px.
3. **Example output** — same illustrative tables, now wrapped in
   `<div class="card example-card">` with a soft border, shadow,
   and pill-style `.status-ok` badge.
4. **Upgrade** — comparison table inside `<div class="card
   compare-card">`, followed by two side-by-side `.cue` cards
   carrying the soft conversion cues.
5. **Get started** — CTA paragraph in `<div class="cta-card">`
   with a light-blue gradient panel.

**Stylesheet** — a new `_LANDING_PAGE_CSS` constant in
`trading_bot/api/server.py`. Self-contained inline `<style>`
block with:

* CSS custom properties (`--primary`, `--surface`, `--border`,
  …) for theming;
* a single mobile-first breakpoint (`@media (min-width: 600px)`)
  that collapses the feature grid from 1 → 3 columns and the
  cue row from 1 → 2;
* a second breakpoint at 880 px purely for whitespace;
* zero JS, zero `<form>`, zero external resources (no Google
  fonts, no CDN, no analytics tag) — pinned by
  `test_no_external_resources`.

**Invariants re-asserted by Phase 5.9 tests:**

* No `<script>`, no `javascript:` URI, no `<form>` /
  `<input>` / `<button>` / `on*` handlers / mutating
  `method=…` attributes.
* Hero gradient (`linear-gradient` + `.hero` selector),
  feature-grid, compare-card, example-card, cue-row, and
  cta-card class hooks all present in the rendered HTML.
* Two `class="cue"` blocks render the soft cues from Phase 5.2
  ("Most users upgrade after ~7 days." and "Premium users run
  3–5x more requests than free users.").
* Five `<section>` blocks (Phase 5.2 contract).
* Legacy positioning phrases — `read-only`, `guardrail`,
  `daily validation`, `audit trail`, `protected dashboard` —
  all still present.
* `<p class="ref">Invited by: <code>…</code></p>` exact format
  for the ref banner; sanitiser unchanged
  (`_sanitize_landing_ref_code` reused from Phase 5.1 growth
  log).
* Output is byte-identical for the same `?ref=` value across
  calls; independent of env vars and disk state.
* No leak of `scorer_config`, `TRADING_API_KEY`, or any
  reports/manifest contents — even when fixtures with planted
  markers are populated underneath the server.
* The `/` route still accepts only `GET / HEAD / OPTIONS`.


### Phase 6.0 — operator-only API key issuance CLI

`trading_bot.api.keys` ships a single `issue` subcommand that
generates one new API key (free or premium) and appends one row
to an operator-only manifest. There is **no** public sign-up
endpoint, no form, no JSON API for any of this — the entire
surface is invoked by hand from a deployment shell.

**Command**

    python -m trading_bot.api.keys issue \
        --tier free|premium \
        --label "<operator-supplied label>" \
        [--checkout \
         --success-url <https://...> \
         --cancel-url  <https://...>] \
        [--manifest-path <path>]

**Manifest** — `data/api_keys_manifest.jsonl` (override via
`TRADING_API_KEYS_MANIFEST_PATH`). Append-only, thread-safe,
schema:

    {
      "created_at":           "2026-04-24T12:00:00.000000Z",
      "key_hash":             "<SHA-256(api_key)[:32]>",
      "label_hash":           "<SHA-256(label)[:32]>",
      "tier":                 "free" | "premium",
      "checkout_session_id":  "cs_..." | null
    }

**Stdout** prints the raw `api_key` exactly once (the operator
delivers it to the user) plus the same five hash/metadata
fields, and — when `--checkout` is used — `checkout_session_id`
and `checkout_url` for the operator to forward to the customer.

**Privacy invariants (every one tested)**

* Raw `api_key` is **never** persisted. Only `SHA-256(...)[:32]`,
  byte-identical to the hash used by server / billing /
  conversion / growth / upgrade_events so the manifest joins
  cleanly with every other Phase 4/5 log on a single column.
* Raw `label` is **never** persisted. Only its SHA-256 prefix.
  An operator who issues a key for `alice@example.com` cannot
  reverse the manifest into a list of customer emails — the
  hash is one-way.
* The Stripe Checkout `url` is **never** persisted. Only the
  short-lived `checkout_session_id` is. URLs contain redirect
  state we don't need to retain.
* No customer email / name / IP / payment field is ever stored.

**Atomicity contract.** If `--checkout` is requested and Stripe
fails (network error, missing `STRIPE_API_KEY`, missing
`STRIPE_PRICE_ID_PREMIUM`, etc.), the manifest row is NOT
written and the CLI exits with code 3. So a manifest row implies
"issuance + checkout-session-creation succeeded end-to-end" —
operators can retry cleanly without leaving phantom keys behind.
Pinned by `test_stripe_failure_does_not_write_manifest` and
`TestCheckoutInProcessViaPatch::test_checkout_failure_returns_three`.

**CLI exit codes**

| Code | Meaning |
|---|---|
| 0 | Success — key issued, manifest row written |
| 2 | Argument validation failure (bad tier, missing URLs, blank label, …) |
| 3 | Stripe / billing failure during `--checkout` (no manifest row written) |

**Generation parameters**

* Key body: `secrets.token_urlsafe(32)` → 43 URL-safe characters
  drawn from `[A-Za-z0-9\-_]`. Far above the 32-byte minimum
  the spec asks for; all randomness from the OS CSPRNG.
* Hash: `SHA-256(api_key)[:32]` — 128 bits of grouping
  precision, identical to every other Phase 4/5 log.

**Boundary** — the keys module imports nothing from
`trading_bot.core.*`, `trading_bot.execution`,
`trading_bot.portfolio`, `trading_bot.risk`,
`trading_bot.scanners`, `trading_bot.strategies`, or
`trading_bot.main`. Only the lazy import of
`trading_bot.api.billing.create_checkout_session` for the
`--checkout` path. Pinned by source-grep test.


### Phase 6.1 — referral key issuance flow

The Phase 6.0 `issue` subcommand grows a single optional flag —
`--ref <code>` — so an operator can pre-attribute a key to a
referral / source channel at creation time, instead of relying
on the user later hitting `?ref=`.

**Command (additive)**

    python -m trading_bot.api.keys issue \
        --tier free|premium \
        --label "<operator label>" \
        --ref  "<channel code>"          # NEW; optional
        [--checkout --success-url … --cancel-url …]

**Manifest schema (single new field)**

    {
      "created_at":           "...Z",
      "key_hash":             "<SHA-256(api_key)[:32]>",
      "label_hash":           "<SHA-256(label)[:32]>",
      "tier":                 "free" | "premium",
      "checkout_session_id":  "cs_..." | null,
      "ref_code":             "<sanitised>" | null   # ← Phase 6.1
    }

**Sanitisation contract.** `--ref` is run through the same
`_sanitize_ref_code` helper the live `?ref=` middleware (Phase
5.1 growth log) uses:

* Charset: `[A-Za-z0-9\-_:.]`. Anything else is stripped.
* Cap: 64 chars (truncated, not rejected).
* Empty input, `None`, or a value that the sanitiser strips to
  `""` is treated as "no ref" — the manifest stores `null` and
  no Stripe metadata is set.

Pinning this to the same helper means the manifest's `ref_code`
is byte-identical to whatever the growth log would have stored
for the same input, so downstream BI joins work cleanly across
both files. Asserted by
`TestPhase61RefSanitization::test_sanitiser_matches_growth_sanitiser`.

**Stripe metadata pass-through.** `billing.create_checkout_session`
gains a single optional `ref_code: Optional[str] = None` kwarg.
When `--checkout` and a non-empty sanitised `--ref` are both
present, the value flows into the Stripe POST body's
`metadata[ref_code]` and `subscription_data[metadata][ref_code]`
fields — alongside the existing Phase 4.7 `metadata[api_key]`
fields. The Phase 4.7 webhook handler still keys on
`metadata[api_key]`; `ref_code` is purely for downstream
attribution analysis.

End-to-end stub-HTTP test:
`TestPhase61BillingMetadataFields::test_ref_code_lands_in_stripe_metadata`.

**Backward compatibility (tested):**

* `--ref` is optional. Omitting it leaves the manifest row
  byte-equivalent to a Phase 6.0 row except for the new
  `"ref_code": null` field.
* The CLI stdout line for `ref_code` only appears when present.
  Operators who don't use `--ref` see the exact Phase 6.0
  output.
* Calls to `issue_key()` that don't pass the `ref_code=` kwarg
  still work.
* Calls to `create_checkout_session()` that don't pass
  `ref_code=` still produce the exact same Stripe POST body as
  before (no `metadata[ref_code]` field) — pinned by
  `test_no_ref_means_no_ref_metadata`.

**Privacy invariants (re-asserted):**

* Raw `api_key` never persisted (only the hash).
* Raw `label` never persisted (only the hash).
* Raw `--ref` value is sanitised before any persistence or
  Stripe call. A `<script>alert(1)</script>` becomes
  `scriptalert1script` on the manifest and on Stripe; the angle
  brackets, slashes, and parens never appear on disk or in the
  POST body. Pinned by `test_unsafe_chars_stripped` and
  `test_only_sanitised_ref_passed_never_raw`.
* Stripe checkout `url` still never persisted.
* No customer email / IP / payment field is ever stored.

**No new public surface.** Phase 6.1 adds no HTTP endpoint, no
form, no JSON API, no env var. The whole feature lives behind
the existing operator-only `python -m trading_bot.api.keys issue`
shell command.


### Phase 6.2 — manifest inspection CLI (`keys list`)

A new operator-only `list` subcommand on the existing keys CLI
lets the operator safely inspect the issuance manifest without
ever revealing a raw key, raw label, or checkout URL.

**Command**

    python -m trading_bot.api.keys list                  # all rows
    python -m trading_bot.api.keys list --tier free      # filter by tier
    python -m trading_bot.api.keys list --tier premium
    python -m trading_bot.api.keys list --ref hn-launch  # filter by ref_code
    python -m trading_bot.api.keys list --tier free --ref hn-launch   # AND
    python -m trading_bot.api.keys list --json           # machine output
    python -m trading_bot.api.keys list --manifest-path <path>

**Public field set** — the only fields ever emitted, by either
the text or JSON formatter:

    LIST_OUTPUT_FIELDS = (
        "created_at",
        "key_hash",
        "tier",
        "ref_code",
        "checkout_session_id",
    )

`label_hash` is in the manifest (Phase 6.0) but **not** in the
list output. Operators who need it can read the file directly;
the public CLI surface stays as small as possible.

**Defense-in-depth contract.** The reader projects every row
to `LIST_OUTPUT_FIELDS` BEFORE rendering. Even if the manifest
were hand-edited (or mis-written by some future code path) and
contained stray fields like `api_key`, `label`, or
`checkout_url`, the `list` view would silently drop them.
Pinned by `TestPhase62OutputProjection`.

**Tolerance**

* Missing manifest → empty output (text: `(no records)`; JSON: `[]`).
* Blank lines → skipped.
* Malformed JSON → skipped.
* Non-dict JSON values → skipped.
* Unreadable file → empty (no exception).

**Filter semantics**

* `--tier`: case-insensitive exact match on `tier`.
* `--ref`: the operator-supplied value is run through the same
  Phase 5.1 growth sanitiser used at write time, then compared
  by exact equality. So `--ref "<script>xss</script>"` matches
  rows whose stored `ref_code == "scriptxssscript"`. A filter
  value that sanitises to the empty string (e.g. `"!@#"`) is
  treated as **no filter** — better than silently filtering to
  zero rows.
* Combined filters AND together; a row must pass every supplied
  filter.

**Sort** — newest first by `created_at` (ISO-8601 with `Z`
suffix sorts lexicographically the same as chronologically).

**No new public surface.** The `list` command lives behind the
existing operator-only `python -m trading_bot.api.keys` shell
entry-point. There is still no HTTP endpoint, no form, no JSON
API, no env var added. The dispatcher's "available commands"
help text now lists both `issue` and `list`.


### Phase 6.3 — key manifest inspection CLI

Three read-only subcommands on the existing operator CLI let an
operator audit issued and revoked keys without ever surfacing a
raw secret. No HTTP endpoint, no public surface — same shell-only
posture as the rest of Phase 6.

```
python -m trading_bot.api.keys list
python -m trading_bot.api.keys list --tier free|premium
python -m trading_bot.api.keys list --ref <ref_code>
python -m trading_bot.api.keys list --include-revoked
python -m trading_bot.api.keys list --json

python -m trading_bot.api.keys show --key-hash <hash>
python -m trading_bot.api.keys show --key-hash <hash> --json

python -m trading_bot.api.keys stats
python -m trading_bot.api.keys stats --json
```

**Output surface (pinned allow-list)**

Every inspection command projects manifest rows into the same
fixed field set before printing. Anything outside the allow-list
— including fields that a future `issue_key` schema bump might
quietly add — is dropped:

| Field                   | Source                         |
|---|---|
| `created_at`            | manifest row                    |
| `key_hash`              | manifest row                    |
| `tier`                  | manifest row                    |
| `ref_code`              | manifest row                    |
| `checkout_session_id`   | manifest row (may be `null`)    |
| `revoked`               | derived from the revocation log |
| `revoked_at`            | revocation row (if revoked)     |
| `revoked_reason`        | revocation row (if present)     |

**Never printed** — `api_key`, `label`, `label_hash`,
`checkout_url`. The first three are never persisted to begin with
(Phase 6.0/6.2); the last is never persisted to begin with
(Phase 6.0). `label_hash` is on disk but the inspection surface
omits it — operators who need it grep the manifest directly.

**Behaviour**

* `list` sorts newest first by `created_at`; unparseable
  timestamps fall to the bottom.
* Filters (`--tier`, `--ref`) are ANDed. `--ref` runs through
  the Phase 5.1 growth sanitiser first so it matches whatever
  the manifest actually stored (i.e. the same transformation
  the live `?ref=` middleware applies).
* Revoked rows are hidden by default; `--include-revoked`
  shows them alongside active rows and adds a `revoked` column.
* Missing files (manifest or revocation log) produce a clean
  "0 active" report rather than a crash.
* Malformed JSONL rows, rows missing `key_hash`, and rows with
  an unrecognised `tier` are silently skipped — same posture
  as the auth-path reader.
* `show` looks a single manifest row up by `key_hash` and
  returns exit-code 2 with a stderr message if no row matches.
* `stats` counts `total_issued`, `active`, `revoked`, plus
  per-tier / per-ref_code / per-created-date breakdowns. The
  `revoked` counter counts manifest rows that have been revoked
  — stray revocation rows for hashes never issued are ignored.

**Boundary** — Phase 6.3 adds no new module and no new import
into Core. The commands live in `trading_bot.api.keys` alongside
`issue` and `revoke`, so the dependency-light CLI posture
(no `structlog`, no `fastapi`) is preserved. Pinned by
`tests/test_keys.py::TestPhase63NoCoreImports`.


### Phase 6.4 — Railway persistent manifest strategy

Phase 6.0 / 6.2 / 6.3 added the issuance / auth / inspection
plumbing. Phase 6.4 covers the production deployment story:
where the manifest and revocation log **actually live** on the
Railway side so issued keys survive restarts and re-deploys.

There is no new code in Phase 6.4 — every flag (`--manifest-path`,
`--revoked-path`) and every env var (`TRADING_API_KEYS_MANIFEST_PATH`,
`TRADING_API_KEYS_REVOKED_PATH`) was already shipped in 6.2 / 6.3.
The contribution is operational guidance:

* point both env vars at a persistent volume (`/data/...` on Railway);
* issue / revoke / inspect from a Railway shell against that volume;
* keep the manifest out of git;
* never confuse a *local* manifest with the *production* manifest
  — a key issued locally has never reached Railway and will 403.

Full operator runbook — production env vars, Railway volume layout,
worked CLI examples, three deployment options, and the local-vs-
production warning — lives in [`DEPLOYMENT.md`](DEPLOYMENT.md).


### Phase 7.0 — Stripe → key activation bridge

Phase 7.0 closes the loop between the Phase 6 issuance model and the
Phase 4.7 Stripe billing cache. A paid subscription now activates
premium access **automatically** on `customer.subscription.created`
without the operator editing env vars or touching the manifest.

Two hardening changes ship together:

**1. Hash-only premium cache.** `data/stripe_premium_keys.json` now
stores SHA-256[:32] hashes exclusively. `add_premium_key` hashes its
input in-process; the raw api_key never reaches disk. Legacy cache
files that still contain raw keys are transparently re-hashed and
rewritten on the next load — no operator intervention required.

**2. Manifest verification gate.** Before adding an api_key to the
cache, the webhook handler calls
`trading_bot.api.key_store.verify_api_key(api_key)`. A hit means
the key is in the issuance manifest AND has not been revoked. A
miss — unknown hash, or revoked hash — causes the webhook to return

```
{ "action": "ignored", "reason": "key_not_in_manifest_or_revoked" }
```

No cache mutation, no conversion-log row, no side effects. The
revoked-key path is the important one: a key rotated off the
manifest can never be re-promoted by a Stripe replay. Cancellation
(`customer.subscription.deleted`) and payment failure
(`invoice.payment_failed`) still flow through without the gate so a
cancelled customer immediately loses premium even if their manifest
row was already deleted.

**Auth precedence (Phase 7.0, unchanged from Phase 6.2)**

```
1. revoked hash             → 403 (kill switch)
2. Stripe cache (premium)    → premium   ← webhook-driven
3. TRADING_API_PREMIUM_KEYS  → premium   (operator override)
4. manifest tier="premium"   → premium   (CLI-issued)
5. manifest tier="free"      → free      (CLI-issued)
6. TRADING_API_KEY exact     → free      (legacy single-tenant)
7. otherwise                 → 403
```

Stripe always overrides the manifest tier. A key issued as free
(step 5) is promoted to premium (step 2) the moment the webhook
fires. A cancellation drops the key out of step 2 and the next
request resolves via step 5 again — free access restored.

**Privacy invariants (pinned by tests)**

* Raw `api_key` never reaches disk (Phase 7.0 migration of the
  Stripe cache). Pinned by
  `tests/test_billing.py::TestPhase70HashOnlyPersistence` and
  `tests/test_api_server.py::TestPhase70NoRawKeyInStripeCache`.
* The webhook NEVER mutates the issuance manifest. Pinned by
  `tests/test_billing.py::TestPhase70ManifestNotMutated` —
  manifest bytes compare equal before/after a
  `subscription.created` delivery.
* Customer email, customer name, PAN, CVV, and payment-method
  fields are never persisted — the webhook handler only reads
  `object.metadata.api_key`. Pinned by
  `tests/test_billing.py::TestNoSensitiveDataStored`.
* Revoked keys cannot be re-promoted by a Stripe replay. Pinned by
  `tests/test_billing.py::TestPhase70ManifestGate::test_revoked_key_webhook_ignored`.

**No new public surface, no Core imports.** `billing.py` still
imports nothing from `trading_bot.core.*`, `trading_bot.execution`,
`trading_bot.portfolio`, `trading_bot.risk`, `trading_bot.scanners`,
`trading_bot.strategies`, or `trading_bot.main`. `key_store` is
lazy-imported inside the webhook handler so the billing module
remains loadable in environments where the manifest file is not
configured. No new HTTP endpoint is added — the Stripe webhook
already existed (`POST /webhook/stripe`, Phase 4.7).


### Phase 7.1 — browser icon noise cleanup

Browsers auto-request `/favicon.ico`, `/apple-touch-icon.png`, and
`/apple-touch-icon-precomposed.png` on every page view. We do not
ship icon assets, so those requests would otherwise 404 and bloat
the audit log. Three explicit routes return `204 No Content` with
no body, no auth required, and the standard security-header set
still applied. The icon paths are added to both
`_FREE_TIER_EXEMPT_PATHS` and `_PUBLIC_PATHS_NO_USAGE` so a browser
that refreshes a page 100 times cannot consume free-tier quota or
pollute per-key usage metrics. Pinned by
`tests/test_api_server.py::TestPhase71IconRoutes`.


### Phase 7.2 — production launch lockdown

Hardening + verification + cleanup wrapped in one phase so a real
launch can ship safely.

**1. `railway.toml` is locked.** The production start command
`chmod -R 777 /app/data || true && uvicorn trading_bot.api.server:app
--host 0.0.0.0 --port 8080` is fixed in the repo and pinned by
`tests/test_launch_check.py::TestRailwayTomlLockdown`. The toml file
must NOT invoke `keys issue` / `revoke` / `list` at boot — issuance
remains operator-only.

**2. `python -m trading_bot.api.launch_check`** is a pure-stdlib
operator CLI that verifies the deployment is launch-ready: every
required env var is set, paths are writable, and nothing points at
`/tmp` (rejected unless `--allow-tmp`) or repo-local `data/`
(rejected when `RAILWAY_ENVIRONMENT` is set). Returns exit 0 + prints
`READY` on success, exit 1 + `NOT READY` on any failure. JSON output
via `--json`.

**3. `python -m trading_bot.api.launch_check --smoke ...`** combines
the env check with the Phase 6.5 HTTP smoke runner — same six
checks (public landing, health, 401, 403/503, 200/404, dashboard).
The supplied `--api-key` is hashed in-process; the raw value never
appears in any output. The smoke runner is injectable so the
combined wrapper is fully unit-testable without real network IO.

**4. `python -m trading_bot.api.keys revoke-many --key-hash H ...`**
appends one revocation row per `--key-hash`. Useful for launch-day
cleanup when several test keys need to be invalidated at once.
Duplicates are accepted; the read side (`list`, `show`) deduplicates
into "revoked = yes". The CLI accepts ONLY pre-hashed values so an
operator cannot accidentally land a raw key on disk via this path.

**5. Launch Day Checklist** — the full operator runbook lives in
[`DEPLOYMENT.md`](DEPLOYMENT.md) ("Launch Day Checklist" section).
Eight steps: lock `railway.toml`, set Railway env vars, issue real
keys from the Railway shell, revoke test keys, run `launch_check`,
run `launch_check --smoke`, wire Stripe AFTER auth passes,
hand-deliver keys securely.

**No new public endpoints.** Phase 7.2 adds three CLIs and zero
HTTP routes. Phase 7.3 (the next section) explicitly adds one new
mutating route — `POST /billing/checkout` — alongside the existing
`POST /webhook/stripe`; the single-mutating-route invariant in
`tests/test_launch_check.py::TestNoNewPublicEndpoints` lists both.


### Phase 7.3 — authenticated Stripe Checkout endpoint

> The user requested this work as "Phase 7.1"; it ships here as 7.3
> to avoid collision with the previously-shipped Phase 7.1 (browser
> icon noise cleanup) and Phase 7.2 (production launch lockdown).
> The behaviour is identical to the spec.

A free-tier user calls `POST /billing/checkout` to receive a Stripe
Checkout Session URL that flips them to premium on payment via the
existing Phase 4.7 / 7.0 webhook plumbing. There is no public
sign-up form — the caller must already authenticate with an issued
manifest key.

**Request**

```
POST /billing/checkout
Authorization: Bearer <issued-key>
```

**Response (200)**

```json
{
  "checkout_session_id": "cs_test_...",
  "checkout_url": "https://checkout.stripe.com/c/cs_test_...",
  "key_hash": "<SHA-256(api_key)[:32]>",
  "tier_to": "premium"
}
```

The `checkout_url` is short-lived and lives in memory only — it is
returned to the caller but persisted nowhere on the server.

**Status codes**

| Code | When |
|---|---|
| 200 | Free key, Stripe Checkout Session created. |
| 401 | Missing `Authorization: Bearer` header. |
| 403 | Key not in manifest (or revoked). |
| 409 | Already premium — manage the existing subscription via Stripe. |
| 502 | Stripe API returned a non-2xx or malformed payload. |
| 503 | `STRIPE_SECRET_KEY`, `STRIPE_PREMIUM_PRICE_ID`, or `TRADING_PUBLIC_BASE_URL` is unset. |

**Env vars (required for live operation)**

| Env var | Purpose | Fallback |
|---|---|---|
| `STRIPE_SECRET_KEY` | Stripe REST auth (Basic, password empty). | `STRIPE_API_KEY` (legacy Phase 4.7 name) |
| `STRIPE_PREMIUM_PRICE_ID` | Stripe Price ID of the premium subscription. | `STRIPE_PRICE_ID_PREMIUM` (legacy) |
| `TRADING_PUBLIC_BASE_URL` | Absolute URL prefix for success/cancel redirects (e.g. `https://your-host.example.com`). | — |

**Env vars (optional)**

| Env var | Default |
|---|---|
| `STRIPE_CHECKOUT_SUCCESS_PATH` | `/dashboard?checkout=success` |
| `STRIPE_CHECKOUT_CANCEL_PATH`  | `/dashboard?checkout=cancel`  |

**What goes to Stripe**

The handler builds a subscription-mode Checkout Session POST with:

```
mode                                          = subscription
line_items[0][price]                          = STRIPE_PREMIUM_PRICE_ID
line_items[0][quantity]                       = 1
success_url                                   = TRADING_PUBLIC_BASE_URL + success path
cancel_url                                    = TRADING_PUBLIC_BASE_URL + cancel path
client_reference_id                           = <key_hash>
metadata[key_hash]                            = <key_hash>
metadata[tier_from]                           = free
metadata[tier_to]                             = premium
subscription_data[metadata][key_hash]         = <key_hash>
subscription_data[metadata][tier_to]          = premium
customer_creation                             = always
```

Note: `metadata[api_key]` (legacy Phase 4.7 raw-key field) is
**deliberately absent** from this code path. The Stripe webhook
handler extends to consume `metadata[key_hash]` in a future phase;
until then the existing operator-CLI flow (`keys issue --checkout`)
continues to populate `metadata[api_key]` for back-compat.

**Privacy invariants (every one tested)**

* The raw `api_key` is **never** sent to Stripe. Pinned by
  `test_metadata_contains_key_hash_not_raw_key` and
  `test_stripe_metadata_contains_key_hash_not_raw`.
* The `checkout_url` is **never** persisted to the manifest, the
  revocation log, the usage log, the audit log, the Stripe premium
  cache, the conversion log, or the upgrade-events log. Pinned by
  `test_checkout_url_never_persisted_to_any_log`.
* The 409 response for an already-premium caller does not echo
  the raw key. Pinned by `test_premium_key_returns_409`.
* Misconfiguration responses (503) name the missing env var, never
  the caller's key. Pinned by the `TestPhase73CheckoutMisconfigured`
  class.

**Boundary**

* The checkout helper (`billing.create_checkout_session_for_hash`)
  reuses Phase 4.8's `_post_to_stripe`, `BillingConfigError`, and
  `BillingAPIError` — no new HTTP code paths to maintain.
* `POST /billing/checkout` joins `POST /webhook/stripe` as the only
  mutating routes in the entire app. Pinned across
  `tests/test_billing.py`, `tests/test_api_server.py`, and
  `tests/test_launch_check.py`.


### Phase 7.4 — hash-based Stripe webhook promotion

Closes the loop on the Phase 7.3 hash-only Checkout flow. The
Stripe webhook handler now accepts ``metadata[key_hash]`` directly,
so a customer paying through `POST /billing/checkout` is auto-
promoted to premium without any raw API key ever reaching Stripe
or this server's billing path.

**Webhook identity extraction (Phase 7.4 contract)**

`_extract_identity_from_event_object` returns a `(api_key, key_hash)`
tuple, pulling each independently from `object.metadata` and
defensively from `object.customer.metadata`. The handler prefers
`key_hash` when both are present.

| Source on Stripe object | Phase | Promoted as |
|---|---|---|
| `metadata[key_hash]` | 7.3 / 7.4 (preferred) | hash directly |
| `metadata[api_key]` | 4.7 / 4.8 (legacy operator-CLI) | hash of api_key |
| neither | — | `{action: ignored, reason: no_identity_on_event}` |

The handler's response gains an `"identity"` field
(`"key_hash"` or `"api_key"`) so operators can see which path the
event flowed through.

**Manifest gate (unchanged semantics, hash-input variant added)**

* `_verify_against_manifest(api_key)` (Phase 7.0) — hashes then
  validates.
* `_verify_hash_against_manifest(key_hash)` (new in Phase 7.4) —
  validates the hash directly. Both check
  `key_store.lookup_key_hash(...) is not None` AND
  `not key_store.is_revoked(...)`. A revoked hash can never be
  re-promoted by a Stripe replay, regardless of which identity
  field the event carries.

**Premium-cache helpers (hash-only, Phase 7.0 schema preserved)**

| Function | Input | Notes |
|---|---|---|
| `add_premium_key(api_key)` | raw key | Hashes, then delegates to `add_premium_hash`. |
| `add_premium_hash(key_hash)` | hash | New in Phase 7.4 — bypasses the redundant hash. |
| `remove_premium_key(api_key)` | raw key | Hashes, then delegates to `remove_premium_hash`. |
| `remove_premium_hash(key_hash)` | hash | New in Phase 7.4 — symmetric removal. |

The on-disk schema (`data/stripe_premium_keys.json` — list of
SHA-256[:32] hashes) is unchanged.

**Conversion logging (Phase 4.9 surface, hash-input variant added)**

`record_conversion(api_key, ...)` now delegates to a new
`record_conversion_for_hash(key_hash, ...)`. The Phase 7.4 webhook
handler picks the variant matching the event's identity source. The
on-disk row schema (`data/api_conversions.jsonl`) is unchanged —
both paths land an `api_key_hash` row.

**`is_stripe_configured()` widened**

Now returns True if EITHER `STRIPE_API_KEY` (legacy) OR
`STRIPE_SECRET_KEY` (Phase 7.3 preferred) is set. Operators
migrating to the new env-var name no longer need to set both.

**Auth precedence (unchanged)**

Stripe still wins over manifest tier, regardless of which path
populated the cache. A key promoted via the Phase 7.4 hash flow
is indistinguishable from one promoted via the Phase 4.7 raw-key
flow on the read side.

**Privacy invariants pinned by tests**

| Invariant | Pinned by |
|---|---|
| Raw API key never required for the Phase 7.4 promotion path | `TestPhase74WebhookHashPath::test_valid_key_hash_promotes` |
| Premium cache contains only hashes after a hash-path event | `TestPhase74WebhookPersistence::test_premium_cache_contains_only_hash_after_hash_path` |
| Planted PII (email, name, PAN, CVV) in the event never reaches the cache | `TestPhase74WebhookPersistence::test_planted_pii_in_event_does_not_reach_cache` |
| Revoked hashes cannot be re-promoted by Stripe replay | `TestPhase74WebhookHashPath::test_revoked_key_hash_does_not_promote` |
| Legacy `metadata[api_key]` flow still works | `TestPhase74LegacyApiKeyPathStillWorks` (2 tests) |
| Hash-path cancellation/payment_failed correctly remove premium | `TestPhase74WebhookCancellationByHash` (2 tests) |
| End-to-end /billing/checkout → webhook[key_hash] → premium | `TestPhase74CheckoutWebhookEndToEnd::test_checkout_then_webhook_promotes_to_premium` |

`POST /billing/checkout` is now fully hash-only end-to-end:
checkout → Stripe → webhook → premium cache. No raw key on the
wire to Stripe; no raw key in the cache file; no raw key in any
operator log.


### Phase 8.1 — tier-aware usage enforcement

Turns the product from "authenticated API" into "metered SaaS".
A new `usage_enforcement_middleware` reads the existing Phase 4.6
per-key usage log and rejects calls that would push the caller
above their tier's daily request cap.

**Env vars (all optional with sane defaults)**

| Env var | Default | What it does |
|---|---|---|
| `TRADING_USAGE_ENFORCEMENT_ENABLED` | `true` | Disable the entire layer with `false`/`0`/`no`/`off`. |
| `TRADING_FREE_DAILY_REQUEST_LIMIT` | `50` | Per-key per-UTC-day cap for free tier. Invalid → default. |
| `TRADING_PREMIUM_DAILY_REQUEST_LIMIT` | `1000` | Per-key per-UTC-day cap for premium tier. Invalid → default. |
| `TRADING_USAGE_LIMIT_EXEMPT_PATHS` | unset | Comma-separated extra exempt paths (joined with the default exempt set). |

**Default exempt paths (no enforcement, no headers)**

```
/                                      (public landing)
/health                                (liveness probe)
/favicon.ico                           (Phase 7.1 browser noise)
/apple-touch-icon.png                  (Phase 7.1 browser noise)
/apple-touch-icon-precomposed.png      (Phase 7.1 browser noise)
/webhook/stripe                        (Stripe → server webhook)
```

**Response headers (added on every non-exempt response from
authenticated callers — both 2xx and 4xx route responses)**

| Header | Value |
|---|---|
| `X-Usage-Limit` | the active tier cap |
| `X-Usage-Remaining` | `max(0, limit - count_after_this_request)` |
| `X-Usage-Tier` | `"free"` or `"premium"` |
| `Retry-After` | (429 only) seconds until next UTC midnight |

**Rejection contract**

```
HTTP/1.1 429 Too Many Requests
X-Usage-Limit: 50
X-Usage-Remaining: 0
X-Usage-Tier: free
Retry-After: 27384
Content-Type: application/json

{"detail": "usage limit reached — upgrade for higher limits"}
```

**Tier resolution & precedence (unchanged from Phase 7.4)**

The middleware uses the same `_is_premium` resolver everything
else uses, so a key that was promoted by the Stripe webhook (Phase
7.0/7.4) is automatically billed against the premium cap on the
very next request — no restart, no env edits.

**Layering with Phase 5.4**

The Phase 5.4 free-tier middleware (per-`/reports/*` sub-cap +
older free-only daily cap with `X-Free-Tier-*` headers) is kept
intact for backward compatibility. Phase 8.1 is registered to fire
FIRST in the request pipeline so its 429 short-circuits when both
would otherwise reject the same call. The two never produce
conflicting responses on the same request.

**Failure posture (best-effort, fail-open)**

* Missing/empty/malformed usage log → `_count_free_tier_usage_today`
  returns `(0, 0)` → enforcement treats the caller as having made
  zero requests today (lets them through). Pinned by
  `TestPhase81MissingUsageLogFailsOpen`.
* Disabled toggle → middleware passes every request through with
  no headers added. Pinned by
  `TestPhase81EnforcementCanBeDisabled`.

**Privacy invariants (every one tested)**

* The usage log schema is unchanged — Phase 4.6 already stored
  `key_hash` only, never the raw api_key. Pinned by
  `TestPhase81UsageLogStoresHashOnly`.
* Unauthenticated requests do NOT count against any user — the
  middleware exits early when the bearer is missing or unknown.
  Pinned by `TestPhase81NoCountForUnauthenticated`.
* Stripe webhooks are exempt by path, even when called with a
  forged `Authorization` header. Pinned by
  `TestPhase81PublicPathsExempt::test_webhook_stripe_never_429s`.
* No new mutating route added. Pinned by
  `TestPhase81DoesNotIntroduceMutatingRoute`.


### Phase 8.2 — feature-level tier differentiation

Free vs premium are now meaningfully differentiated at the
response level. The tier resolution path (Phase 6.2 / 7.0 / 7.4)
is unchanged; Phase 8.2 only changes what tier-gated routes return
once the caller is classified.

**New helper**

`_is_premium_user(request) -> bool` — fast tier classifier for
route handlers. Reads ``request.state.api_key_tier`` (cached by
``require_api_key``) first; falls back to extracting the bearer
and consulting `_is_premium`. Returns False on any unauthenticated
request, so callers can use it without a separate guard.

**Differentiated endpoints**

| Route | Free | Premium |
|---|---|---|
| `GET /reports/latest` | curated subset (high-level summary + upgrade hint) | full sanitised report |
| `GET /reports/history` (new) | 403 with the documented gated response | `{"count": N, "dates": [...]}` |
| `GET /dashboard` | banner + projected report (premium-only fields hidden) | full HTML dashboard |

The free `/reports/latest` projection is driven by a fixed
allow-list (``_FREE_REPORT_ALLOWED_FIELDS``):

```
report_type, report_date, scorer_fingerprint, totals,
promotion_readiness
```

Anything outside that allow-list is dropped — a future schema
addition cannot accidentally surface to free users. The free
response also carries a small ``upgrade`` envelope so client UIs
can render a consistent call-to-action:

```json
{
  "report_type": "daily_alpha_validation",
  "report_date": "2026-04-25",
  "scorer_fingerprint": "...",
  "totals": {"alpha_rows": 100, "buy_rows": 25, "skip_rows": 75},
  "promotion_readiness": {"ready": true, "consecutive_passing_days": 21},
  "tier": "free",
  "upgrade": {
    "detail": "premium feature — upgrade required",
    "hint": "upgrade for full access"
  }
}
```

**Uniform 403 contract**

Every premium-only feature uses the same helper:

```
HTTP/1.1 403 Forbidden
X-Usage-Tier: free
X-Usage-Limit: 50
X-Usage-Remaining: 47
Content-Type: application/json

{"detail": "premium feature — upgrade required"}
```

The body string is the constant ``PREMIUM_FEATURE_DETAIL``;
``X-Usage-Tier`` reflects the caller's actual tier so a client can
branch without parsing the body. The Phase 8.1 usage headers ride
along because the request DID authenticate — premium-feature 403s
consume the caller's daily quota (otherwise free users could spam
gated endpoints for free).

**`/reports/history` route ordering**

Registered BEFORE ``/reports/{date}`` so FastAPI's path matcher
treats "history" as a literal segment, not a date path-param. A
free caller hitting `/reports/history` gets the documented 403,
not "invalid date". Pinned by
`TestPhase82ReportsHistoryPremiumUser::test_history_does_not_collide_with_date_route`.

**Boundary**

* No new mutating routes — `/reports/history` is GET-only. Pinned
  by `TestPhase82DoesNotIntroduceMutatingRoute`.
* No new persistence — Phase 8.2 is read-only logic on top of
  existing data files.
* Raw API keys never appear in any Phase 8.2 response body or
  header. Pinned by `TestPhase82NoRawKeyInResponses` (2 tests).
* Phase 8.1 enforcement layer's 403/429 skip rule narrowed to
  {401, 503} so Phase 8.2 gated 403s carry the usage headers
  too — auth-failure responses still don't leak count to
  unauthenticated callers.


### Phase 8.3 — upgrade pressure system

Converts the Phase 8.1 / 8.2 tier signals from passive ("here's
your limit") into active ("here's a checkout URL — click to
upgrade now"). One helper, three trigger points, zero new
mutating routes.

**Helper**

`_build_upgrade_payload(request, *, reason, required, is_premium=None, key_hash=None) -> dict | None`

* Returns ``None`` for premium callers (no upgrade needed) and on
  any Stripe-side failure (so callers degrade gracefully).
* Returns the documented dict for free callers when Stripe
  Checkout creation succeeds:

  ```json
  {
    "required":     true,
    "reason":       "usage_limit",
    "checkout_url": "https://checkout.stripe.com/c/cs_…",
    "hint":         "upgrade for full access"
  }
  ```

* The ``checkout_url`` is freshly minted via
  ``billing.create_checkout_session_for_hash`` (Phase 7.3 —
  hash-only metadata). It is **never** persisted by this helper or
  any caller. Pinned by
  `TestPhase83CheckoutUrlNotPersisted::test_url_absent_from_every_operator_log`.

**Three trigger points**

| Trigger | Status | `reason` | `required` | Source |
|---|---|---|---|---|
| Daily-cap exhausted | 429 | `usage_limit` | `true` | `usage_enforcement_middleware` (Phase 8.1) |
| Premium-gated feature | 403 | `feature_locked` | `true` | `_premium_required_response` (replaces `_premium_required` from Phase 8.2) |
| Free-tier curated body | 200 | `limited_access` | `false` | `/reports/latest` handler |

The `required: true | false` flag is a UX hint:
`true` means "the request was blocked"; `false` means "the
response is a curated subset — the caller can still use it".

**Body shapes (free user, Stripe configured)**

429 — usage limit exhausted:

```json
{
  "detail": "usage limit reached — upgrade for higher limits",
  "upgrade": {
    "required": true,
    "reason": "usage_limit",
    "checkout_url": "https://checkout.stripe.com/c/cs_…",
    "hint": "upgrade for full access"
  }
}
```

403 — premium-only feature:

```json
{
  "detail": "premium feature — upgrade required",
  "upgrade": {
    "required": true,
    "reason": "feature_locked",
    "checkout_url": "https://checkout.stripe.com/c/cs_…",
    "hint": "upgrade for full access"
  }
}
```

200 — curated `/reports/latest`:

```json
{
  "report_type": "daily_alpha_validation",
  "report_date": "2026-04-25",
  "scorer_fingerprint": "...",
  "totals": {...},
  "promotion_readiness": {...},
  "tier": "free",
  "upgrade": {
    "required": false,
    "reason": "limited_access",
    "checkout_url": "https://checkout.stripe.com/c/cs_…",
    "hint": "upgrade for full access"
  }
}
```

Premium callers receive the same base response WITHOUT the
``upgrade`` key — no Stripe call is made on their behalf. Pinned
by `TestPhase83Usage429UpgradePayload::test_premium_429_does_not_include_payload`
and `TestPhase83ReportsLatestLimitedAccess::test_premium_reports_latest_omits_payload`.

**Headers (unchanged)**

The Phase 8.1 usage headers (`X-Usage-Limit`, `X-Usage-Remaining`,
`X-Usage-Tier`, `Retry-After`) and the Phase 8.2 `X-Usage-Tier`
on 403s ride along verbatim. The upgrade payload is body-only —
intentionally NOT duplicated into headers.

**Failure posture (every path tested)**

* Stripe API non-2xx → log at DEBUG, return base response WITHOUT
  the upgrade payload. Pinned by
  `TestPhase83StripeFailureGracefulDegradation` (3 tests).
* `TRADING_PUBLIC_BASE_URL` unset → no checkout possible → return
  base response without the upgrade payload. Pinned by
  `test_no_public_base_url_skips_payload`.
* Premium short-circuits BEFORE any Stripe call. Pinned by
  `test_premium_user_explicit_arg_skips_stripe`.

**Performance posture**

Every free-tier 429 / 403 / curated-200 response triggers ONE
outbound Stripe POST. Operators uncomfortable with that
trade-off can disable specific trigger points by:

* `TRADING_USAGE_ENFORCEMENT_ENABLED=false` (kills the 429 path).
* unsetting `TRADING_PUBLIC_BASE_URL` (skips all three Phase 8.3
  payloads while preserving Phase 8.1/8.2 base behaviour).

**Privacy invariants (every one tested)**

* Raw API key never sent to Stripe (Phase 7.3 hash-only contract).
  Pinned by
  `test_free_429_includes_payload` (asserts
  `metadata[api_key]` absent and `metadata[key_hash]` present).
* Raw API key never appears in any Phase 8.3 response body or
  header.
* `checkout_url` never persisted to the manifest, revocation log,
  usage log, audit log, Stripe premium cache, conversion log, or
  upgrade-events log.
* Premium callers never trigger a Stripe call from any of the
  three trigger points.

**Boundary**

* No new HTTP routes — Phase 8.3 is helper code reused by existing
  routes / middleware. Pinned by
  `test_payload_does_not_introduce_mutating_route`.
* `_premium_required` (Phase 8.2 raise-style) replaced by
  `_premium_required_response` (Phase 8.3 return-style) so the
  body can carry both `detail` and `upgrade`. The `detail`
  constant is unchanged.
* Phase 8.1 enforcement layer still applies to Phase 8.3 responses
  — gated 429s still 429. Pinned by
  `test_usage_enforcement_still_applies_after_payload_attached`.


### Phase 8.4 — conversion-funnel tracking

Three-stage upgrade funnel logged into the existing
``data/api_upgrade_events.jsonl`` file (Phase 5.5/5.8 path,
backward-compatible schema). One row per stage per user per
trigger so an operator can answer:

  * how many free users **saw** an upgrade prompt today
  * how many of those **clicked** through to Checkout
  * how many of those **completed** the subscription

The funnel reuses the Phase 5.5 file but adds two operator-facing
columns (``reason``, ``endpoint``); legacy Phase 5.5 events
continue to use ``copy_variant_hash`` / ``ref_code``. Readers
branch on ``event``.

**Three new event constants**

| Event | Fired from | When |
|---|---|---|
| ``upgrade_shown`` | ``server._build_upgrade_payload`` | a Phase 8.3 upgrade payload was attached to a response (one row per response, never duplicated). |
| ``upgrade_clicked`` | ``server.billing_checkout`` handler | the caller successfully created a Stripe Checkout Session via ``POST /billing/checkout``. |
| ``upgrade_completed`` | ``billing.handle_webhook_event`` | Stripe ``customer.subscription.created`` event flipped the key to premium. |

**Helper signature**

```python
record_upgrade_funnel_event(
    key_hash: str,                          # SHA-256[:32], hash-only
    event: str,                             # one of the three constants
    *,
    reason:     Optional[str] = None,       # e.g. "usage_limit"
    endpoint:   Optional[str] = None,       # e.g. "/reports/latest"
    request_id: Optional[str] = None,
    now:        Optional[datetime] = None,
) -> dict
```

The helper accepts ONLY ``key_hash`` (the signature documents
``key_hash``, not ``api_key``) — every call site in the codebase
pre-hashes via Phase 7.4's ``_hash_api_key``. Operators MUST do
the same. Reasons / endpoints are stripped + capped (64 / 128
chars respectively) before persistence; blank values become
``None``.

**Persisted row shape**

```json
{
  "timestamp":    "2026-04-25T14:30:00.000000Z",
  "api_key_hash": "<SHA-256[:32]>",
  "event":        "upgrade_shown",
  "tier":         "free",
  "request_id":   "<32-hex>" | null,
  "reason":       "usage_limit" | "feature_locked" | "limited_access" | "checkout_initiated" | "stripe_webhook" | null,
  "endpoint":     "/reports/latest" | "/reports/history" | "/billing/checkout" | "/webhook/stripe" | null
}
```

**Sample funnel reads**

A single free user hitting their daily cap, clicking the upgrade
URL, paying through Stripe:

```jsonl
{"event":"upgrade_shown","reason":"usage_limit","endpoint":"/reports/latest","api_key_hash":"bcd5...","tier":"free",...}
{"event":"upgrade_clicked","reason":"checkout_initiated","endpoint":"/billing/checkout","api_key_hash":"bcd5...","tier":"free",...}
{"event":"upgrade_completed","reason":"stripe_webhook","endpoint":"/webhook/stripe","api_key_hash":"bcd5...","tier":"free",...}
```

**Funnel math**

```
shown    = count(distinct api_key_hash) where event = "upgrade_shown"
clicked  = count(distinct api_key_hash) where event = "upgrade_clicked"
completed= count(distinct api_key_hash) where event = "upgrade_completed"

clickthrough_rate   = clicked   / shown
conversion_rate     = completed / clicked
overall_funnel_rate = completed / shown
```

Filter by ``reason`` to compare which trigger ("usage_limit",
"feature_locked", "limited_access") drives the most upgrades, or
by ``endpoint`` to see which gated route is the strongest funnel
entrypoint.

**Privacy invariants (every one tested)**

* Raw API key never in the funnel log — pinned by
  ``TestPhase84FunnelLeakGuard::test_no_raw_key_after_full_funnel``.
* Legacy ``metadata[api_key]`` webhook path hashes the raw key
  before logging — pinned by
  ``test_webhook_legacy_api_key_path_emits_completed_with_hash``.
* The funnel helper's signature is hash-only (``key_hash``, not
  ``api_key``) — pinned by
  ``TestPhase84RecordUpgradeFunnelEventSchema::test_records_documented_fields``.

**Failure / dedup posture**

* Funnel writer failures are best-effort — wrapped in try/except
  at every call site so a disk failure NEVER breaks the user's
  request. Pinned by
  ``TestPhase84FunnelLoggingFailureDoesNotBreakRequest`` and
  ``TestPhase84RecordUpgradeFunnelEventNoLeak::test_disk_failure_does_not_raise``.
* Premium callers never emit any of the three events because the
  Phase 8.3 helper short-circuits BEFORE the Stripe call AND
  before the funnel write. Pinned by
  ``test_premium_does_not_emit_shown``.
* "No duplicate spam events in same request" is structurally
  guaranteed — each event has exactly one call site per response
  flow, so a single request produces at most one row per stage.
  Pinned by ``test_no_dedupe_spam_on_single_response``.
* Cancellation / payment-failure webhook events do NOT emit
  ``upgrade_completed`` — pinned by
  ``test_cancellation_does_not_emit_completed``.

**Boundary**

* No new HTTP route. Phase 8.4 is helper code wired into existing
  routes / middleware / webhook.
* The three new event constants extend the Phase 5.5
  ``VALID_EVENTS`` set; the legacy ``record_upgrade_event`` and
  the new ``record_upgrade_funnel_event`` share the same JSONL
  file. Readers branch on ``event``.
* No new operator log file. The default path
  (``data/api_upgrade_events.jsonl``) and env var
  (``TRADING_API_UPGRADE_EVENTS_LOG_PATH``) are unchanged.


### Phase 9.1 — insight layer

A small, deterministic insight builder that decorates the existing
``/reports/latest`` JSON and the ``/dashboard`` HTML with an
``insights`` field. Pure function — same inputs always produce the
same outputs; no Stripe / network / time-of-day side effects.

**Module**

``trading_bot/api/insights.py`` — pure stdlib, imports nothing
from FastAPI / structlog / the rest of ``trading_bot``. Two
public entry points:

```python
build_insights(report, prev_report=None) -> list[dict]
truncate_for_free(insights, *, max_count=FREE_INSIGHT_LIMIT) -> list[dict]
```

**Insight schema** (every entry conforms)

| Field | Type | Notes |
|---|---|---|
| ``id``         | ``str``                            | one of ``KNOWN_INSIGHT_IDS`` |
| ``title``      | ``str``                            | one-line headline |
| ``summary``    | ``str``                            | 1-2 sentence explanation |
| ``confidence`` | ``float``                          | clamped to ``[0.0, 1.0]`` |
| ``severity``   | ``"info"`` / ``"warn"`` / ``"critical"`` | ops attention level |
| ``evidence``   | ``dict``                           | rule-specific supporting numbers |
| ``action``     | ``str``                            | next-step hint for the operator |

**Three deterministic rules**

| ID                       | When it fires | Severity | Confidence basis |
|---|---|---|---|
| ``trend.buy_delta``      | both reports have ``totals.buy_rows`` | ``info``, or ``warn`` when buys drop ≥ 50% vs prior day | ``abs(delta) / prev`` clamped |
| ``promotion.readiness``  | report has ``promotion_readiness`` | ``info`` when ready or building, ``warn`` when blocked | ``1.0`` ready / streak-scaled / ``0.8`` blocked |
| ``regime.dominant``      | report has ``regime_stats`` with non-zero hits | ``info`` | dominance share |

The output list is ordered ``[trend, readiness, regime]`` — fixed
regardless of the input report. Rules with insufficient data
silently drop themselves.

**Tier-aware projection**

* Premium → full ``insights`` list with full ``evidence`` per
  entry.
* Free → at most ``FREE_INSIGHT_LIMIT`` (``= 2``) entries; each
  surviving entry's ``evidence`` is projected through a per-rule
  allow-list:

  | ID                       | Free-tier evidence keys |
  |---|---|
  | ``trend.buy_delta``      | ``delta``, ``direction`` |
  | ``promotion.readiness``  | ``ready`` |
  | ``regime.dominant``      | ``regime`` |

  Insights with an unrecognised ``id`` are dropped entirely
  (fail-closed for future rules without an allow-list).

**Wired call sites**

* ``GET /reports/latest`` — best-effort prior-day load (falls
  back to ``None`` on missing / parse error), then
  ``build_insights`` → tier-aware truncation → ``insights`` field
  on the response. Free response also still carries the
  ``upgrade`` envelope (Phase 8.3) when Stripe is configured.
* ``GET /dashboard`` — same prior-day load, same insights compute,
  threaded through ``render_dashboard_html`` as a new optional
  ``insights`` kwarg. Renderer adds an "Insights" ``<section>`` of
  ``<li>`` entries with severity-tagged CSS classes
  (``insight-info`` / ``insight-warn`` / ``insight-critical``).

**Privacy invariants (every one tested)**

* No raw API key in any insight or in any rendered HTML. Pinned by
  ``TestPhase91ReportsLatestNoLeak::test_raw_key_absent_from_insights_response``.
* The rule helpers consume only the sanitised report (Phase 4.0
  ``_sanitize_report`` strips ``scorer_config`` / filesystem
  paths). The insight layer cannot surface Core internals because
  it can't see them.
* Free tier truncation is mechanical — the allow-list lives next
  to the rule definitions in ``insights.py`` so a new rule that
  forgets its allow-list is dropped from free output.

**Boundary**

* No new HTTP route. Phase 9.1 is helper code wired into existing
  routes / renderer. Pinned by
  ``TestPhase91CrossCutting::test_no_new_mutating_route``.
* No new logs. No new env vars.
* Phase 8.1 / 8.2 / 8.3 / 8.4 still apply unchanged — the
  ``insights`` field rides alongside the ``upgrade`` field, and a
  429 from the usage layer short-circuits before the report
  handler ever runs (so insights are simply not computed for
  rate-limited requests).
* Pure stdlib import — pinned by
  ``tests/test_insights.py::TestBoundary::test_module_imports_only_stdlib_typing``.


### Phase 9.2 — daily hook & retention

A "what changed since yesterday" hook surfaced as a top-of-page
banner on the dashboard and as a ``daily_hook`` field on the
``/reports/latest`` JSON. Designed to drive repeat usage by
making the day-over-day signal the very first thing a returning
caller sees.

**Module**

``trading_bot/api/daily_hook.py`` — pure stdlib, imports nothing
from FastAPI / structlog / the rest of ``trading_bot`` (not even
``insights``; the trend insight is consumed by ID via the
``insights`` argument). Two public entry points:

```python
build_daily_hook(report, prev_report, insights=None) -> dict | None
truncate_for_free(hook) -> dict | None
```

**Hook schema** (every entry conforms when ``build_daily_hook``
does not return ``None``)

| Field | Type | Notes |
|---|---|---|
| ``headline``   | ``str``                            | human-readable tagline ("Buys up 10 vs prior day") |
| ``change``     | ``"up"`` / ``"down"`` / ``"flat"`` | direction |
| ``magnitude``  | ``int``                            | absolute change |
| ``confidence`` | ``float``                          | clamped to ``[0.0, 1.0]`` (premium only) |
| ``since``      | ``str`` / ``None``                 | ISO date of the prior report |
| ``driver``     | ``str``                            | ``"trend.buy_delta"`` or ``"totals.buy_rows"`` (premium only) |
| ``cta``        | ``str``                            | stable "Open the dashboard for the full breakdown" |

**Source preference order**

1. **Phase 9.1 trend insight** (preferred) — when the ``insights``
   list contains a ``trend.buy_delta`` entry with valid evidence,
   the hook borrows its ``delta`` / ``direction`` / ``confidence``.
   ``driver`` reports ``"trend.buy_delta"``.
2. **Direct totals fallback** — when the insight is missing or
   malformed, the hook falls back to a direct
   ``totals.buy_rows`` delta. Confidence collapses to a fixed
   ``0.4`` and ``driver`` reports ``"totals.buy_rows"``.
3. **Hook absent** — when there's no prior-day report, or both
   sources lack data, ``build_daily_hook`` returns ``None`` and
   the consumer omits the field entirely.

**Tier-aware projection**

* Premium → full hook with all seven fields.
* Free → ``confidence`` and ``driver`` dropped; the user-facing
  fields (``headline``, ``change``, ``magnitude``, ``since``,
  ``cta``) are kept verbatim.

**Wired call sites**

* ``GET /reports/latest`` — best-effort prior-day load (Phase 9.1
  already does this), then ``build_daily_hook(curr, prev,
  insights)`` → tier-aware truncation → ``daily_hook`` field on
  the response. Field is OMITTED when the hook is ``None``.
* ``GET /dashboard`` — same compute. The renderer adds an
  ``<aside class="daily-hook daily-hook-{change}">`` block at the
  top of the page (above the latest-report section). The aside
  is OMITTED when the hook is ``None``. Both tiers see the same
  markup; the underlying truncation is what differs.

**Sample free response excerpt**

```json
{
  "report_type": "daily_alpha_validation",
  "report_date": "2026-04-25",
  "tier": "free",
  "daily_hook": {
    "headline": "Buys up 10 vs prior day",
    "change": "up",
    "magnitude": 10,
    "since": "2026-04-24",
    "cta": "Open the dashboard for the full breakdown"
  },
  "insights": [...],
  "upgrade": {...}
}
```

**Sample premium response excerpt**

```json
{
  "report_type": "daily_alpha_validation",
  "report_date": "2026-04-25",
  "daily_hook": {
    "headline": "Buys up 10 vs prior day",
    "change": "up",
    "magnitude": 10,
    "confidence": 0.5,
    "since": "2026-04-24",
    "driver": "trend.buy_delta",
    "cta": "Open the dashboard for the full breakdown"
  },
  "insights": [...]
}
```

**Sample dashboard banner HTML**

```html
<aside class="daily-hook daily-hook-up">
  <strong>Buys up 10 vs prior day</strong>
  <span class="daily-hook-since">since 2026-04-24</span>
  <span class="daily-hook-cta">Open the dashboard for the full breakdown</span>
</aside>
```

**Privacy invariants (every one tested)**

* No raw API key in any hook field or rendered HTML. Pinned by
  ``TestPhase92NoLeak`` (2 tests).
* Hook absent when prior-day report is missing — no synthetic
  values, no bogus zeros. Pinned by
  ``TestPhase92ReportsLatestHookAbsent`` (2 tests).
* Free truncation is mechanical via the ``_FREE_HOOK_ALLOWLIST``
  set, so any future field added to ``build_daily_hook`` stays
  premium-only until its allow-list entry is added.

**Boundary**

* No new HTTP route. Phase 9.2 is helper code wired into existing
  routes / renderer. Pinned by
  ``TestPhase92CrossCutting::test_no_new_mutating_route``.
* No new persistence — hook is computed per request from the
  same files Phase 9.1 already reads.
* No new env vars.
* ``trading_bot/api/daily_hook.py`` imports only stdlib + typing.
  Pinned by
  ``tests/test_daily_hook.py::TestBoundary::test_module_imports_only_stdlib_typing``.


### Phase 9.3 — stickiness loop

Two retention signals derived from the same daily report data:

  * **streak** — consecutive passing days from
    ``promotion_readiness.consecutive_passing_days``. Surfaces as
    a positive-reinforcement banner / JSON field whenever the
    streak is ≥ 1 day.
  * **nudge (missed_day)** — gap > 1 day between the latest two
    report dates. Surfaces as a re-engagement banner / JSON field
    when a returning user has missed at least one day.

**Module**

``trading_bot/api/stickiness.py`` — pure stdlib, imports nothing
from FastAPI / structlog / the rest of ``trading_bot``. Four
public entry points:

```python
build_streak(report, prev_report=None)        -> dict | None
build_nudge(report, prev_report)              -> dict | None
truncate_streak_for_free(streak)              -> dict | None
truncate_nudge_for_free(nudge)                -> dict | None
```

**Streak schema**

| Field | Type | Notes |
|---|---|---|
| ``days``           | ``int``  | ``promotion_readiness.consecutive_passing_days`` |
| ``label``          | ``str``  | "5-day passing streak" |
| ``milestone``      | ``bool`` | True iff ``days`` is in ``MILESTONE_DAYS`` |
| ``next_milestone`` | ``int``  | next ladder rung (premium only) |

The milestone ladder is fixed: ``(3, 5, 7, 10, 14, 21, 30, 60, 90,
180, 365)``. Past 365 the helper extends by 30-day rungs so the
streak always has a target to chase.

**Nudge schema**

| Field | Type | Notes |
|---|---|---|
| ``kind``         | ``"missed_day"``                  | only kind in 9.3 |
| ``headline``     | ``str``                           | "You missed 2 days of reports." |
| ``days_missed``  | ``int``                           | gap − 1 |
| ``since``        | ``str``                           | ISO date of the prior report |
| ``cta``          | ``str``                           | stable re-engagement prompt |

**Source rules**

* Streak fires from the current report alone — no prev needed.
  Returns ``None`` when ``consecutive_passing_days`` is missing,
  not an int, zero, or negative.
* Nudge fires only when both reports have parseable
  ``report_date`` strings AND the gap is ≥ 2 days. Same-day or
  out-of-order dates return ``None``.
* Both helpers are pure functions — same inputs always produce
  the same outputs.

**Tier-aware projection**

* Streak: free callers get ``days`` / ``label`` / ``milestone``;
  the forward-looking ``next_milestone`` stays premium.
* Nudge: every field is user-facing, so free callers see the
  full schema. The ``truncate_nudge_for_free`` helper still
  exists as a defensive copy so a future premium-only field can
  be added without churning the call sites.

**Wired call sites**

* ``GET /reports/latest`` — both fields attached when present.
  Either field may be absent independently (e.g. a returning
  user mid-streak: both present; a single-day deployment with
  a streak: only ``streak``; a returning user whose run reset:
  only ``nudge``).
* ``GET /dashboard`` — two new ``<aside>`` banners. Render order
  (top → bottom):
  1. **nudge** — re-engagement message comes first when present.
  2. **streak** — positive reinforcement.
  3. **daily_hook** — yesterday-vs-today (Phase 9.2).
  4. existing free-tier upgrade banner / report / insights /
     experiments.

  Banners are omitted entirely when the underlying signal is
  ``None``. Both tiers see the same markup; the underlying
  truncation is what differs.

**Sample free response excerpt**

```json
{
  "report_type": "daily_alpha_validation",
  "report_date": "2026-04-25",
  "tier": "free",
  "streak": {
    "days": 5,
    "label": "5-day passing streak",
    "milestone": true
  },
  "nudge": {
    "kind": "missed_day",
    "headline": "You missed 2 days of reports.",
    "days_missed": 2,
    "since": "2026-04-22",
    "cta": "Open the dashboard to see what changed while you were away"
  },
  "daily_hook": {...},
  "insights": [...],
  "upgrade": {...}
}
```

**Sample premium response excerpt** — same fields plus
``next_milestone`` on the streak.

**Sample dashboard banner HTML**

```html
<aside class="nudge nudge-missed_day">
  <strong>You missed 2 days of reports.</strong>
  <span class="nudge-since">since 2026-04-22</span>
  <span class="nudge-cta">Open the dashboard to see what changed while you were away</span>
</aside>
<aside class="streak streak-milestone">
  <strong>5-day passing streak</strong>
  <span class="streak-next">next milestone: 7 days</span>
</aside>
```

**Privacy invariants (every one tested)**

* No raw API key in any streak / nudge field or rendered HTML.
  Pinned by ``TestPhase93NoLeak`` (2 tests).
* Both signals fail soft: missing data → ``None`` → field omitted
  cleanly. No synthetic zeros, no fabricated dates.
* Free truncation is mechanical via the per-helper allow-list
  set, so any new field added to either schema stays premium-only
  until its allow-list entry is added.

**Boundary**

* No new HTTP route. Phase 9.3 is helper code wired into existing
  routes / renderer. Pinned by
  ``TestPhase93CrossCutting::test_no_new_mutating_route``.
* No new persistence — both signals are computed per request from
  the same files Phase 9.1 / 9.2 already read.
* No new env vars.
* ``trading_bot/api/stickiness.py`` imports only stdlib + typing.
  Pinned by
  ``tests/test_stickiness.py::TestBoundary::test_module_imports_only_stdlib_typing``.


### Phase 10.3 — viral loop optimization

Phase 10.3 closes the growth loop by tracking **what we send out**
(``share_generated``) and **what comes back in** (``inbound_visit``).
Both events are written to a new JSONL log that follows the same
schema, hashing, and best-effort failure posture as the Phase 5.1 /
5.5 / 8.4 logs, so BI pipelines can join all five files on
``api_key_hash``.

**The two events**

| Event              | Emitted when                                         |
|--------------------|------------------------------------------------------|
| ``share_generated``| ``server._build_share_payload`` attached a share envelope to a response (one row per response, never duplicated). |
| ``inbound_visit``  | A request arrived with a non-empty ``?src=<token>`` query parameter; first-touch source captured. |

**The on-the-wire share envelope** (attached to ``/reports/latest``
for both tiers — sharing is a viral-loop signal, not a tier
gate)::

    {
      "share": {
        "share_url": "https://example/.../reports/latest?src=<key_hash>",
        "hint":      "Share this preview",
        "src":       "<sanitised inbound src>" | null,
        "endpoint":  "/reports/latest"
      }
    }

  * ``share_url`` carries the caller's own ``key_hash`` as the
    outbound ``?src=`` token so an inbound visit triggered by the
    share can be attributed back to the originator without ever
    leaking the raw API key. The hash is the same SHA-256[:32] used
    by every other API log.
  * When ``TRADING_PUBLIC_BASE_URL`` is unset the URL falls back
    to a relative form; the payload is still attached and the event
    still fires so the operator gets the same telemetry
    in development.

**The recorded JSONL row** (``data/api_share_events.jsonl`` by
default, override via ``TRADING_API_SHARE_EVENTS_LOG_PATH``)::

    {
      "timestamp":    "2026-04-25T12:34:56.000000Z",
      "api_key_hash": "<SHA-256[:32]>",
      "event":        "share_generated" | "inbound_visit",
      "endpoint":     "/reports/latest",
      "src":          "<sanitised src>" | null,
      "request_id":   "<32-hex>" | null
    }

  * ``record_share_event(key_hash, type, endpoint, *, src=None, request_id=None)``
    is the single public helper. Like Phase 8.4's funnel writer, it
    refuses raw API keys: callers MUST pre-hash. Unknown event
    types and empty hashes are dropped silently with a structured
    DEBUG log; the helper never raises.
  * ``src`` is sanitised to the same charset as the Phase 5.1
    growth ``ref_code`` (``[A-Za-z0-9\-_:.]``, capped at 64 chars)
    so the share log and the growth log can be joined cleanly on
    ``(api_key_hash, src)`` ↔ ``(api_key_hash, ref_code)``.

**Privacy posture**

  * Raw API keys are **never** persisted — only the SHA-256[:32]
    hash that already appears in
    server / billing / conversion / growth / upgrade-events logs.
  * No IP, no User-Agent, no email, no name, no payment field
    ever enters a record. The only inputs are the opaque key hash,
    the event name, the request endpoint, the (sanitised) src
    token, and the Phase 4.4 request_id.
  * A leak-guard test plants unique markers in every input slot
    and asserts they never appear in the persisted JSONL.

**Failure posture**

  * The share-events writer is best-effort: every exception path
    is caught and logged at DEBUG. A disk failure inside the
    writer must NEVER fail the underlying ``/reports/latest``
    response. Pinned by
    ``tests/test_share_events.py::TestServerIntegration::test_logging_failure_does_not_break_response``.
  * Thread-safe via a module-level ``threading.Lock``; concurrent
    callers cannot interleave a single JSONL line.

**Boundary**

* No new HTTP route. Phase 10.3 is helper code wired into the
  existing ``/reports/latest`` route. Existing tests that assert
  the route inventory continue to pass unchanged.
* No new mutating endpoint — both events are side-effects of a
  GET request.
* No new env vars are required to enable the feature; the only
  knob is ``TRADING_API_SHARE_EVENTS_LOG_PATH`` for relocating the
  JSONL file.
* ``trading_bot/api/share_events.py`` is a sibling of
  ``upgrade_events.py`` and shares its CLI shape::

      python -m trading_bot.api.share_events --summary
      python -m trading_bot.api.share_events --summary --json


### Phase 10.4 — growth intelligence layer

Phase 10.4 turns the existing JSONL telemetry (Phase 8.4 upgrade
events + Phase 10.3 share events) into operator-facing growth
insights. It is **read-only**: no new endpoint, no new file
written, no schema changes. It joins the two logs on the shared
``api_key_hash`` column to surface conversion + viral metrics.

**The headlines** surfaced by ``summarize`` and the CLI:

| Headline                  | Definition                                                   |
|---------------------------|--------------------------------------------------------------|
| ``top_converting_trigger``| The Phase 8.4 ``reason`` value with the highest shown→completed rate (subject to ``min_impressions`` floor). |
| ``top_performing_insight``| The ``endpoint`` with the highest shown→completed rate. Each Phase 9.1/9.2/9.3 insight surface is hosted at a known endpoint, so endpoint stands in as the operator-facing insight identifier. |
| ``best_source``           | The inbound ``src`` token whose visitors converted at the highest rate, attributed via ``api_key_hash``. |
| ``overall_conversion_rate``| ``upgrade_completed`` / ``upgrade_shown`` across the whole funnel. |

**Public API**

::

    from trading_bot.api.growth_intel import (
        load_upgrade_events,
        load_share_events,
        summarize,
        format_summary_text,
    )

    summary = summarize()                       # default paths
    summary = summarize(
        upgrade_path="...",                     # explicit override
        share_path="...",
        min_impressions=5,                      # noise floor
        min_inbound=3,
    )

The summary dict is JSON-serialisable and stable: ``conversion_funnel``,
``by_reason``, ``by_insight``, ``share_funnel``, ``by_src``,
``attribution_by_src``, ``headlines``, ``totals``.

**CLI**

::

    python -m trading_bot.api.growth_intel --summary
    python -m trading_bot.api.growth_intel --summary --json
    python -m trading_bot.api.growth_intel --summary \
        --upgrade-path path/to/upgrade.jsonl \
        --share-path  path/to/share.jsonl \
        --min-impressions 5 --min-inbound 3

**Privacy posture**

* Only aggregated dimensions reach the summary — no
  ``api_key_hash`` column appears in the output, even though
  the loaders read it for the cross-funnel join.
* The leak-guard test plants a unique marker as the would-be raw
  key, hashes it, and asserts neither the raw marker nor the
  individual hash appears in the rendered summary.
* Loaders are tolerant of missing / blank / malformed rows;
  summary always succeeds even on an empty pair of logs.

**Boundary**

* ``trading_bot/api/growth_intel.py`` imports only stdlib +
  structlog (DEBUG-only) + the ``share_events`` / ``upgrade_events``
  sibling modules for their event constants. No FastAPI / Stripe /
  Core import. Pinned by
  ``tests/test_growth_intel.py::TestBoundary::test_module_does_not_import_core``.
* No new HTTP route, no new mutating endpoint, no new persistence —
  the module is invoked offline (CLI, notebook, or BI pipeline).
* No new env vars beyond the existing
  ``TRADING_API_UPGRADE_EVENTS_LOG_PATH`` and
  ``TRADING_API_SHARE_EVENTS_LOG_PATH``.


### Phase 10.5 — optimization loop

Phase 10.5 turns the Phase 10.4 ``summarize`` output into a
deterministic, ordered list of operator-facing recommendations.
Same summary in ⇒ same numbered output. No clock reads, no disk
reads, no network — all rules are pure functions of the
``summary`` dict.

**Public API**

::

    from trading_bot.api.growth_intel import (
        generate_recommendations,
        format_recommendations_text,
        summarize,
    )

    recs = generate_recommendations(summarize())
    print(format_recommendations_text(recs))

**Recommendation schema** — every entry on the returned list::

    {
      "id":        str,   # stable identifier (see VALID_RECOMMENDATION_IDS)
      "priority":  str,   # "high" | "medium" | "low"
      "title":     str,   # short headline
      "rationale": str,   # cites concrete metrics from the summary
      "action":    str,   # concrete next step
    }

**Rules** (deterministic; each rule is a pure function of the
summary):

| Rule id                     | Fires when                                                                                                           |
|-----------------------------|----------------------------------------------------------------------------------------------------------------------|
| ``amplify_top_trigger``     | ``headlines.top_converting_trigger`` is non-null. Priority scales with the trigger's shown→completed rate.           |
| ``feature_top_insight``     | ``headlines.top_performing_insight`` is non-null. Priority scales with the endpoint's shown→completed rate.          |
| ``double_down_best_source`` | ``headlines.best_source`` is non-null. Priority scales with the source's completion rate.                            |
| ``tighten_free_limit``      | ``by_reason.usage_limit`` exists, its absolute rate ≥ 10 %, and the rate is ≥ 1.5× the overall conversion rate.      |
| ``insufficient_data``       | Fallback when no other rule fires. Priority is always ``low``.                                                       |

Priority bands::

    rate >= 25 %  →  "high"
    rate >= 10 %  →  "medium"
    otherwise     →  "low"

The returned list is ordered HIGH → MEDIUM → LOW, with stable
rule order preserved within each priority bucket so the numbered
CLI output is repeatable.

**CLI**

::

    python -m trading_bot.api.growth_intel --recommend
    python -m trading_bot.api.growth_intel --recommend --json
    python -m trading_bot.api.growth_intel --summary --recommend

JSON shape rules (preserves the Phase 10.4 contract):

* ``--summary --json`` alone   → bare summary dict.
* ``--recommend --json`` alone → bare list of recommendations.
* ``--summary --recommend --json`` → ``{summary, recommendations}``.

**Privacy posture**

* The summary surface already strips ``api_key_hash`` columns
  (Phase 10.4); the recommendation surface inherits that and
  cites only aggregated dimensions (reason, endpoint, src) plus
  rate / count integers. A leak-guard test plants a unique raw-
  key marker, hashes it, and asserts neither the marker nor the
  individual hash appears in the rendered recommendations.
* No raw API key ever flows into a recommendation field — pinned
  by ``tests/test_growth_intel.py::TestRecommendationsLeakGuard``.

**Boundary**

* No new HTTP route, no new persistence, no new env vars.
* ``generate_recommendations`` is pure: deterministic, side-effect
  free, never raises (defensive ``try`` wraps each rule).
* The "tighten free limit" rule references reversible Phase 5.4 /
  8.1 knobs; it never recommends raising bounds beyond their
  hard-coded safety ceilings, and the action text spells out the
  revert criteria.


### Phase 11 — execution layer

Phase 11 closes the optimisation loop by **safely** applying the
highest-priority Phase 10.5 recommendation under deterministic
bounds. Today only one recommendation kind is mechanically
actionable — ``tighten_free_limit`` — and even that change happens
via an in-process ``os.environ`` mutation: nothing on disk
changes except a single audit row in
``data/api_execution_log.jsonl``.

**Operator workflow**

::

    # Read-only dry-run.
    python -m trading_bot.api.execution --apply

    # Mutate the env var in this process + append one audit row.
    python -m trading_bot.api.execution --apply --force

    # JSON envelope for scripts.
    python -m trading_bot.api.execution --apply --force --json

The mutation is **transient**: it survives only the lifetime of
the Python process. A rolling-restart-safe rollout requires the
operator to update the deployment env file separately. This
keeps the CLI's blast radius bounded — a misclick reverts the
moment the shell exits.

**Guardrails** (hard-coded; cannot be overridden by env vars)

| Bound                     | Value                                  |
|---------------------------|----------------------------------------|
| ``FREE_LIMIT_MIN``        | 10  (free tier never collapses)         |
| ``FREE_LIMIT_MAX``        | 200 (free tier never exceeds premium)   |
| ``MAX_CHANGE_PCT``        | 0.30 (per-apply blast radius cap)       |
| ``TIGHTEN_REDUCTION_PCT`` | 0.25 (default 25 % reduction step)      |

The validation order (most-specific-first):

1. ``rejected_no_change``         — proposed value equals current
2. ``rejected_out_of_bounds``     — outside ``[10, 200]``
3. ``rejected_change_too_large``  — relative change > 0.30

A rejected plan never mutates ``os.environ``. In ``--force``
mode the rejected attempt is still appended to the audit log
(with ``outcome="rejected_..."``) so the audit trail captures
every attempted change. In ``--apply`` (dry-run) mode the audit
log is never touched, even on rejection.

**Action log schema** — one row per ``--force`` invocation
(default path: ``data/api_execution_log.jsonl``, override via
``$TRADING_API_EXECUTION_LOG_PATH``)::

    {
      "timestamp":          "...Z",
      "recommendation_id":  "tighten_free_limit",
      "priority":           "high" | "medium" | "low",
      "action":             "set_free_limit",
      "env_var":            "TRADING_FREE_DAILY_REQUEST_LIMIT",
      "previous_value":     int,
      "new_value":          int,
      "delta":              int,
      "delta_pct":          float (signed),
      "outcome":            "applied" | "rejected_..." | ...,
      "rejection_reason":   str (only on rejected outcomes)
    }

  * No ``api_key_hash`` field — the execution layer operates on
    aggregated metrics, not per-user data. A leak-guard test
    plants would-be raw markers and asserts the row contains no
    32-char hex token.

**Exit codes** (so CI / scripts can branch deterministically)

* ``0`` — applied / dry_run / no_actionable_recommendation
* ``2`` — usage error (``--apply`` not provided)
* ``3`` — rejected_* (operator must investigate)

**Boundary**

* No new HTTP route, no new mutating endpoint, no new persistence
  surface beyond the audit log.
* ``trading_bot/api/execution.py`` imports only stdlib +
  structlog (DEBUG-only) + ``growth_intel`` for its public API;
  pinned by
  ``tests/test_api_execution.py::TestBoundary::test_module_does_not_import_core``.
* No raw API key is ever read or written; the layer's only inputs
  are aggregated Phase 10.4 metrics.
* The set of applicable recommendations is gated by the private
  ``_APPLICABLE_REC_IDS`` frozenset. Marketing / copy /
  dashboard recommendations (``amplify_top_trigger``,
  ``feature_top_insight``, ``double_down_best_source``,
  ``insufficient_data``) are surfaced to the operator without
  any automatic action.


## Phase 2.7 — dataset rotation (reference)


## Phase 2.7 — dataset rotation (reference)

See [`docs/DATASETS.md`](DATASETS.md) for the full spec. Short
version: set `TRADING_DATA_ROTATION=daily` to append to
`data/decision_log_YYYY-MM-DD.csv` and
`data/alpha_scores_YYYY-MM-DD.csv` instead of the canonical paths.
`journal.csv` is not rotated.
