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


## Phase 2.7 — dataset rotation (reference)


## Phase 2.7 — dataset rotation (reference)

See [`docs/DATASETS.md`](DATASETS.md) for the full spec. Short
version: set `TRADING_DATA_ROTATION=daily` to append to
`data/decision_log_YYYY-MM-DD.csv` and
`data/alpha_scores_YYYY-MM-DD.csv` instead of the canonical paths.
`journal.csv` is not rotated.
