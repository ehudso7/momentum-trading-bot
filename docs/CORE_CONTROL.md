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


### Phase 6.2 — manifest-backed API key authentication

Keys issued by `python -m trading_bot.api.keys issue` (Phase 6.0/6.1)
are now accepted by the live API server **without** editing
`TRADING_API_KEY` or `TRADING_API_PREMIUM_KEYS`. The server hashes
the presented bearer token (`SHA-256(api_key)[:32]`) and looks the
hash up in the issuance manifest. The raw key is never persisted —
the manifest only stores the hash, the same posture as Phase 6.0.

**Files**

| File | Default | Env var | Written by | Read by |
|---|---|---|---|---|
| Issuance manifest | `data/api_keys_manifest.jsonl` | `TRADING_API_KEYS_MANIFEST_PATH` | `keys issue` | server auth |
| Revocation log | `data/api_keys_revoked.jsonl` | `TRADING_API_KEYS_REVOKED_PATH` | `keys revoke` | server auth |

The manifest schema is unchanged from Phase 6.0/6.1 — `key_hash`,
`tier`, `created_at`, `label_hash`, `ref_code`,
`checkout_session_id`. The server only consults `key_hash` and
`tier`; the other fields stay for operator forensics.

The revocation log is also append-only JSONL:

```json
{
  "timestamp":  "2026-04-25T12:34:56.789012Z",
  "key_hash":   "<SHA-256(api_key)[:32]>",
  "reason":     "<operator free text — capped at 200 chars>"
}
```

**Authentication precedence** (top to bottom — first match wins):

```
1. revoked hash             → 403 (kill switch beats every source)
2. Stripe-cache premium      → premium  (Phase 4.7 contract preserved)
3. TRADING_API_PREMIUM_KEYS  → premium  (operator override)
4. manifest tier="premium"   → premium  (CLI-issued, hash lookup)
5. manifest tier="free"      → free     (CLI-issued, hash lookup)
6. TRADING_API_KEY exact     → free     (legacy single-tenant)
7. otherwise                 → 403
```

A revoked manifest hash is rejected even when the same raw key is
also listed in `TRADING_API_PREMIUM_KEYS` or cached by Stripe —
revocation is the unambiguous kill switch. Manifest-premium does
**not** override a Stripe cancellation: if Stripe drops a key from
its cache (cancellation / payment failure) and that key only
appears in the manifest as premium, the request resolves through
the manifest. If the manifest does NOT list it, the key falls back
to whatever the next applicable source returns.

**Fail-closed config**

Protected endpoints return `503` only when the deployment is
not configured for any auth source:

```
TRADING_API_KEY unset
  AND TRADING_API_PREMIUM_KEYS unset
  AND issuance manifest empty (no parseable rows)
```

If at least one of those is configured, an unknown bearer token
gets `403 Invalid API key` — never `503`. A revoked-only manifest
still counts as configured (revocation does not unconfigure the
deployment back to 503).

**Hot reload**

`trading_bot.api.key_store` caches both files in process memory
keyed on `(path, mtime)`. When a new key is issued or revoked, the
next request picks up the change automatically — no server
restart required.

**Operator workflow**

```bash
# Issue a free-tier key (Phase 6.0)
python -m trading_bot.api.keys issue --tier free --label "alice"
# → prints raw api_key ONCE; record it now.

# Customer hits the API directly with no env-var edit:
curl https://api.example.com/reports/latest \
  -H "Authorization: Bearer <api_key>"

# Revoke (preferred — no raw key needed):
python -m trading_bot.api.keys revoke \
  --key-hash <hash from issuance> \
  --reason "user-requested rotation"

# Revoke when only the raw key is on hand: hashed in-process,
# never written to disk:
python -m trading_bot.api.keys revoke \
  --api-key "<the raw key>" \
  --reason "leaked"
```

**Privacy invariants (every one tested)**

* The raw `api_key` is **never** persisted — not in the manifest,
  not in the revocation log, not in the audit log, not in the
  usage log. The `revoke --api-key` path hashes in-process and
  writes only the hash. Pinned by
  `tests/test_api_server.py::TestPhase62NoRawKeyOnDisk` and
  `tests/test_keys.py::TestPhase62RevokeCli::test_subprocess_revoke_by_raw_key_does_not_leak`.
* The raw `label` is **never** persisted (carry-over from
  Phase 6.0).
* Revocation always stores the hash only — `key_store.append_revocation`
  refuses to accept a raw key as input. Hashing happens at the CLI
  layer before the helper is called.
* `key_store` imports nothing from FastAPI, structlog, or the rest
  of the trading-bot package — the operator CLI continues to run
  in dependency-light environments. Pinned by
  `tests/test_keys.py::TestPhase62NoCoreImports`.
* No HTTP signup endpoint, no form, no JSON API. The entire
  Phase 6.2 surface is the same operator-only shell that Phase 6.0
  introduced, plus one new `revoke` subcommand.

**Module boundary** — `trading_bot/api/key_store.py` is a leaf
module. `trading_bot.api.keys` and `trading_bot.api.server` both
import from it; `key_store` itself imports only from the Python
stdlib.


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


## Phase 2.7 — dataset rotation (reference)


## Phase 2.7 — dataset rotation (reference)

See [`docs/DATASETS.md`](DATASETS.md) for the full spec. Short
version: set `TRADING_DATA_ROTATION=daily` to append to
`data/decision_log_YYYY-MM-DD.csv` and
`data/alpha_scores_YYYY-MM-DD.csv` instead of the canonical paths.
`journal.csv` is not rotated.
