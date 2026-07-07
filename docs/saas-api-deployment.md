# Deploying the SaaS Analytics API

This document covers deploying **`trading_bot.api.server:app`** —
the read-only SaaS analytics + billing webhook surface — to a PaaS
host (Railway, Heroku, Render, Fly).

It is **separate** from the trading-bot dashboard deploy, which is
documented in the project README. Both can co-exist on the same
account using two services.

---

## TL;DR runbook

```bash
# 1. Set Stripe + auth env vars on the platform.
railway variables --set STRIPE_SECRET_KEY=sk_test_...
railway variables --set STRIPE_API_KEY=sk_test_...        # legacy fallback
railway variables --set STRIPE_PREMIUM_PRICE_ID=price_...
railway variables --set STRIPE_PRICE_ID_PREMIUM=price_... # legacy fallback
railway variables --set STRIPE_WEBHOOK_SECRET=whsec_...
railway variables --set TRADING_API_KEY=<single free-tier key>
# Optional: comma-separated allow-list of premium API keys.
# railway variables --set TRADING_API_PREMIUM_KEYS=<key1,key2>

# 2. Deploy.
railway up

# 3. Tail logs.
railway logs --lines 200

# 4. Confirm /health responds publicly.
curl -i https://<your-railway-host>/health
```

Expected `/health` response:
```json
{"status":"ok","service":"momentum-trading-bot-analytics","timestamp":"…Z"}
```

---

## startCommand

Use the `trading-bot-api` entry point, not a hand-rolled `uvicorn`
invocation:

```toml
# railway.toml or platform UI
[deploy]
startCommand = "trading-bot-api"
healthcheckPath = "/health"
healthcheckTimeout = 5
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

`trading-bot-api`:

* Resolves the bind port from `$PORT` (PaaS convention) → falls
  back to 8080. Out-of-range / malformed values fail-soft to 8080
  so an operator typo cannot cause a healthcheck deploy loop.
* Resolves the bind host from `$HOST` → `0.0.0.0` (PaaS-friendly).
* Runs `uvicorn trading_bot.api.server:app` with structured
  request logging via the existing middleware stack.
* No reload / dev features — `--reload` exists for local dev but
  is documented as forbidden in production.

The full CLI:

```
trading-bot-api [--port N] [--host H] [--log-level info|debug|…] [--dry-run] [--reload]
```

`--dry-run` prints the resolved bind address and exits 0 without
starting uvicorn — useful for CI / pre-deploy smoke checks.

---

## Healthcheck contract

The SaaS API exposes **`GET /health`** (not `/healthz`) — it's the
public unauthenticated liveness probe and returns 200 with:

```json
{
  "status": "ok",
  "service": "momentum-trading-bot-analytics",
  "timestamp": "<ISO-8601 UTC>"
}
```

* Always 200 once the app has started.
* No external dependencies (no Stripe call, no DB read, no disk
  write). Returns purely from in-process state.
* Accepts NO `Authorization` header — exercising it from a
  healthchecker that strips auth is fine.

Set the platform's `healthcheckPath` to `/health`.

> If you previously deployed the trading-bot dashboard, the
> healthcheck path was `/healthz`. The SaaS API uses `/health`
> (singular). Don't mix them up.

---

## Env vars

### Required for the API to do anything useful

| Var | Purpose |
|---|---|
| `TRADING_API_KEY` | Single free-tier API key the server accepts as Bearer. Set even if you only have premium users — the request that sets up Stripe still passes through this env-var auth path. |

### Required for Stripe billing (Phase 4.7+)

| Var | Purpose |
|---|---|
| `STRIPE_SECRET_KEY` (preferred) or `STRIPE_API_KEY` (legacy fallback) | Outbound Stripe REST auth. |
| `STRIPE_WEBHOOK_SECRET` | HMAC secret for `POST /webhook/stripe` signature verification. |
| `STRIPE_PREMIUM_PRICE_ID` (preferred) or `STRIPE_PRICE_ID_PREMIUM` (legacy fallback) | Premium price id used by `create_checkout_session`. Also the fallback price for both named plans below. |
| `STRIPE_PRO_PRICE_ID` | Price id for `POST /billing/checkout` with body `{"plan": "pro"}`. Falls back to `STRIPE_PREMIUM_PRICE_ID` when unset. |
| `STRIPE_ELITE_PRICE_ID` | Price id for `POST /billing/checkout` with body `{"plan": "elite"}`. Falls back to `STRIPE_PREMIUM_PRICE_ID` when unset. |

### Required for frontend self-serve key provisioning (Phase 11)

| Var | Purpose |
|---|---|
| `TRADING_PROVISION_SECRET` | Server-to-server shared secret for `POST /keys/provision` (sent as the `X-Provision-Secret` header by the frontend backend, never by browsers). Unset → the endpoint fails closed with 503; wrong/missing header → 403. Generate with `openssl rand -hex 32`. |

Both legacy and preferred names continue to work — see
`docs/stripe-zero-dollar-test.md` for the test-mode runbook that
uses a $0 monthly price for safe end-to-end checkout validation.

### Optional / behaviour-shaping

| Var | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | Platform-injected port. Honoured by `trading-bot-api`. |
| `HOST` | `0.0.0.0` | Bind host. |
| `TRADING_API_PREMIUM_KEYS` | (empty) | Comma-separated allow-list of paid keys (Phase 4.5). Phase 12: resolves to plan `pro`. |
| `TRADING_API_ELITE_KEYS` | (empty) | Phase 12 — comma-separated allow-list of `elite`-plan keys. A key in both lists is elite. |
| `TRADING_API_RATE_LIMIT_PER_MINUTE` | `60` | Base per-minute rate limit; Phase 12 uses it as the shared fallback for all three tier limits. |
| `TRADING_API_RATE_LIMIT_PER_MINUTE_FREE` | `60` | Phase 12 — free-tier per-minute limit. Falls back to `TRADING_API_RATE_LIMIT_PER_MINUTE`, then 60. |
| `TRADING_API_RATE_LIMIT_PER_MINUTE_PRO` | `120` | Phase 12 — pro-tier per-minute limit. Falls back to `TRADING_API_RATE_LIMIT_PER_MINUTE`, then 120. |
| `TRADING_API_RATE_LIMIT_PER_MINUTE_ELITE` | `300` | Phase 12 — elite-tier per-minute limit. Falls back to `TRADING_API_RATE_LIMIT_PER_MINUTE`, then 300. |
| `TRADING_API_USAGE_LOG_PATH` | `data/api_usage.jsonl` | Per-key usage log path (Phase 4.6). |
| `TRADING_API_AUDIT_LOG_PATH` | `data/api_access_audit.jsonl` | Audit log path (Phase 4.4). |
| `TRADING_API_REPORTS_DIR` | `reports` | Where the daily validation reports live. |
| `TRADING_API_MANIFEST_PATH` | `data/alpha_experiments.jsonl` | Experiment manifest (Phase 3.6). |
| `TRADING_STRIPE_PREMIUM_CACHE_PATH` | `data/stripe_premium_keys.json` | Webhook-driven entitlement cache. Phase 12 v2 format: `{"version": 2, "hashes": {"<hash>": {"plan": "pro"\|"elite"}}}`. The legacy flat-list format (a JSON array of hashes) is still read transparently — every legacy entry loads as plan `pro` — and the file is rewritten in v2 form on the next entitlement change. No manual migration is needed. |
| `TRADING_FREE_MAX_REQUESTS_PER_DAY` | `50` | Phase 5.4 free-tier daily request cap. |
| `TRADING_FREE_MAX_REPORT_CALLS` | `10` | Phase 5.4 free-tier report-calls cap. |
| `TRADING_UPGRADE_BANNER_COPY` | (default copy) | Phase 5.7 dashboard banner override. |
| `TRADING_LIMIT_HIT_COPY` | (default copy) | Phase 5.7 429 detail override. |
| `TRADING_REPORT_LIMIT_COPY` | (default copy) | Phase 5.7 403 detail override. |
| `SENTRY_DSN` | (unset) | Optional Sentry error tracking. When set, `trading-bot-api` (and the bot CLI) initialise sentry-sdk at boot with `traces_sample_rate=0.05`, `environment` from `RAILWAY_ENVIRONMENT_NAME` (default `local`), and `release` from `RAILWAY_GIT_COMMIT_SHA` when Railway provides it. FastAPI is auto-instrumented. Unset → strict no-op (sentry-sdk is never even imported). |

### Tier entitlement matrix (Phase 12)

API keys resolve to one of three tiers — `free`, `pro`, `elite`
(`GET /billing/status` reports the resolved tier; the legacy
`premium` boolean stays true for both paid plans):

| Entitlement | free | pro | elite |
|---|---|---|---|
| Rate limit (per minute) | 60 | 120 | 300 |
| `/reports/{date}` window | 3 days | 30 days | unlimited |
| `/experiments/*` cap | 3 | 25 | unlimited |
| Insights | truncated | full | full + `elite` block |
| Daily free-tier usage caps | enforced | exempt | exempt |

Plan switching (`pro` ↔ `elite`) goes through the Stripe billing
portal ("Manage subscription") — `POST /billing/checkout` refuses to
create a second subscription for an already-paid key (409).

---

## Volume mount

The Phase 4-5 logs (`api_usage.jsonl`, `api_audit.jsonl`,
`api_growth.jsonl`, `api_conversions.jsonl`,
`api_upgrade_events.jsonl`) and the Stripe premium-key cache
(`stripe_premium_keys.json`) write to `data/` by default. On
Railway you'll typically attach a persistent volume at
`/app/data`.

**Permission note.** When a Railway volume is mounted at
`/app/data`, its existing files are usually owned by `root`
(UID 0) but the container runs as `botuser` (UID 1000 in the
project's Dockerfile). Two consequences:

1. **Existing files are read-only to the app process** until they
   were created by `botuser`. The first new write in each file
   will succeed (botuser owns the new file); subsequent writes to
   pre-existing root-owned files will fail with `PermissionError`.
2. **Every disk write in the SaaS API is wrapped in
   `try/except`** and logged at `DEBUG` — a permission-denied
   write is non-fatal. The user-facing request still succeeds.

If you see `chmod: Operation not permitted` warnings during
deploy, they're cosmetic — usually from a `chmod -R 777
/app/data` step in an entrypoint script trying to fix the
permission mismatch but failing because the calling user doesn't
own the volume's existing files. The container starts and serves
traffic regardless.

To eliminate the warnings entirely, either:

* Run the container as `root` (drop `USER botuser` in the
  Dockerfile) — simple, but loses the non-root-user safety
  property.
* OR re-create the volume with the right ownership via a
  one-time `railway volume migrate` workflow (operator-only).

The default project Dockerfile keeps `USER botuser` because the
volume warnings are cosmetic and the app handles permission
errors gracefully.

---

## Boundary invariants (preserved)

`trading_bot.api.serve` imports nothing from:

* `trading_bot.core` — alpha scoring, decision logging
* `trading_bot.execution` — broker / paper broker
* `trading_bot.portfolio` — position management
* `trading_bot.risk` — circuit breaker, position sizing
* `trading_bot.scanners`, `trading_bot.strategies` — scanning + signal generation
* `trading_bot.main` — the trading bot CLI entry
* `trading_bot.dashboard` — the trading-bot dashboard

It imports `trading_bot.api.server` lazily inside `main()` (only
on the production code path, not during `--dry-run` or argparse
parsing). The non-reload code path keeps the import inside
`main()` so the module-load cost of the heavy SaaS middleware
stack only happens once per worker.

---

## Smoke checks

Local:

```bash
# 1. CI-friendly: print resolved bind, exit 0, no uvicorn import.
trading-bot-api --dry-run

# 2. Start uvicorn on a random high port and curl /health.
PORT=18181 trading-bot-api &
curl -i http://127.0.0.1:18181/health

# 3. Run the full smoke + boundary tests.
pytest tests/test_api_serve.py tests/test_startup_health.py -v
```

Post-deploy:

```bash
# Public liveness — must always be 200.
curl -i https://<your-railway-host>/health

# Auth-gated endpoint — proves the auth + middleware stack is wired.
curl -i https://<your-railway-host>/reports/latest \
    -H "Authorization: Bearer $TRADING_API_KEY"

# Stripe webhook — must exist (POST). A bare GET should 405.
curl -i https://<your-railway-host>/webhook/stripe
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Healthcheck fails with `Application failed to respond` 502 | `PORT` env mismatch — app bound 8080, platform routed to a different port. | Use `trading-bot-api` (honours `$PORT`). Never hardcode `--port` in the startCommand. |
| `chmod: Operation not permitted` warnings at boot | Entrypoint script trying to chmod a root-owned volume from a non-root user. | Cosmetic. Either drop the chmod step, run as root, or re-init the volume with the right owner. |
| `503` from `/billing/checkout` (or your local equivalent) immediately after deploy | `STRIPE_SECRET_KEY` (or legacy `STRIPE_API_KEY`) missing on the platform. | Set both env vars; redeploy. |
| `403 Invalid API key` on every request | The `Authorization: Bearer …` value doesn't match `TRADING_API_KEY` and isn't in `TRADING_API_PREMIUM_KEYS`. | Use the key the operator issued via `python -m trading_bot.api.keys issue` and add it to the right env-var. |
| `webhook signature failed` in logs | `STRIPE_WEBHOOK_SECRET` doesn't match the secret Stripe used to sign the call. | Test-mode and live mode have different webhook secrets — make sure they match the mode of `STRIPE_SECRET_KEY`. |
