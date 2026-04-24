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


## Phase 2.7 — dataset rotation (reference)

See [`docs/DATASETS.md`](DATASETS.md) for the full spec. Short
version: set `TRADING_DATA_ROTATION=daily` to append to
`data/decision_log_YYYY-MM-DD.csv` and
`data/alpha_scores_YYYY-MM-DD.csv` instead of the canonical paths.
`journal.csv` is not rotated.
