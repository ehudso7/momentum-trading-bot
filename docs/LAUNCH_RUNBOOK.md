# Launch Runbook — remaining operator actions (updated 2026-07-25)

Scope: this runbook serves the **private paper launch**. The authoritative
contract is `docs/PRIVATE_PAPER_LAUNCH.md`; the pre-deploy checklist is
`docs/LAUNCH_CHECKLIST.md`. Where any of them disagree, the contract wins.

Code-side gates are green as of 2026-07-25: 2,773 backend tests pass,
`pip-audit` is clean, `npm audit --omit=dev --audit-level=high` reports zero
vulnerabilities, and frontend lint and production build pass. The items below
are **operator-only actions** needing production credentials or persistence
approval — each is a copy-paste command. Run them from the repo root.

Section 2 (payments) is **deferred** for this release and retained only as
reference for the future public launch. Do not run it now.

## 1. Start the core trading bot as its own Railway service

The existing `momentum-trading-bot` Railway service runs only the SaaS
analytics API. The trading loop is a second service using the same repo
and Dockerfile, selected by `SERVICE_ROLE=bot` (see
`scripts/railway_start.sh`).

```bash
set -a && source .env && set +a
railway add --service momentum-bot-core \
  --repo ehudso7/momentum-trading-bot --branch main \
  --variables "SERVICE_ROLE=bot" \
  --variables "ALPACA_API_KEY=$ALPACA_API_KEY" \
  --variables "ALPACA_API_SECRET=$ALPACA_API_SECRET" \
  --variables "POLYGON_API_KEY=$POLYGON_API_KEY" \
  --variables "TRADING_RUN_MODE=paper" \
  --variables "TRADING_LOG_JSON=true" \
  --variables "DASHBOARD_CORS_ORIGINS=https://momentumforge-enhanced.vercel.app,http://localhost:3000"
railway volume add --service momentum-bot-core --mount-path /app/data
railway domain --service momentum-bot-core   # note the generated URL
```

Then point the frontend's live-dashboard proxy at it:

```bash
cd frontend && npx vercel env add NEXT_PUBLIC_DASHBOARD_URL production
# value: https://<momentum-bot-core domain>
```

**Alternative (or additional) — run it on this Mac.** A LaunchAgent is
already written at `~/Library/LaunchAgents/com.momentumforge.bot.plist`
(paper mode, dashboard on :8080, auto-restart, survives reboot):

```bash
# stop any session-scoped bot first so port 8080 is free
pkill -f "trading-bot --mode paper" || true
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.momentumforge.bot.plist
```

The Mac must be awake during market hours (`caffeinate -s` or
Energy Saver settings); the Railway service has no such caveat.

## 2. Payments go-live config — DEFERRED, do not run for this release

> **Deferred behind the public-product gate.** The private paper launch disables
> public signup and billing. Running this section would re-enable a monetized
> public surface before the legal review that the contract requires (product
> classification, disclosures, market-data redistribution rights, privacy terms,
> subscription rules). Keep it unset. This text is reference for a future
> public launch only.

The self-serve purchase flow (signup → auto API key → checkout →
webhook fulfillment → premium) is fully implemented and fail-closed
until these are set:

```bash
# One shared secret for key provisioning (backend + frontend must match)
PROV=$(openssl rand -hex 32)

railway variables --service momentum-trading-bot \
  --set "TRADING_PROVISION_SECRET=$PROV" \
  --set "TRADING_PUBLIC_BASE_URL=https://momentumforge-enhanced.vercel.app" \
  --set "ALPACA_API_KEY=$ALPACA_API_KEY"        # was missing on this service

cd frontend && npx vercel env add TRADING_PROVISION_SECRET production
# paste the same $PROV value
```

In the **Stripe dashboard**:
1. Create two recurring prices: Pro $29/mo and Elite $99/mo.
2. Set them on Railway:
   `railway variables --service momentum-trading-bot --set "STRIPE_PRO_PRICE_ID=price_..." --set "STRIPE_ELITE_PRICE_ID=price_..."`
   (Both fall back to `STRIPE_PREMIUM_PRICE_ID`, already set, until then.)
3. Confirm the webhook endpoint
   `https://momentum-trading-bot-production.up.railway.app/webhook/stripe`
   is registered for `checkout.session.completed`,
   `customer.subscription.updated`, `customer.subscription.deleted`,
   and that its signing secret matches Railway's `STRIPE_WEBHOOK_SECRET`.

Then redeploy the frontend (`git commit --allow-empty -m redeploy && git push`
or `npx vercel --prod`) so the new env vars take effect, and run one
$0-style test checkout (see `docs/stripe-zero-dollar-test.md`).

## 3. Going live (real money) — blocked by an evidence gate

Paper mode is the enforced default everywhere. Setting `TRADING_RUN_MODE=live`
is **not sufficient** and will not start the bot on its own.

Before an `AlpacaBroker` is constructed in live mode,
`trading_bot/main.py::_assert_live_evidence_gate` reads the trade journal and
raises `RuntimeError` unless every criterion in
`trading_bot/risk/live_readiness.py` passes:

| Criterion | Threshold |
|---|---|
| Closed paper trades | ≥ 100 |
| Distinct trading days | ≥ 20 |
| Expectancy per trade | > 0 |
| Profit factor | ≥ 1.25 |
| Peak-to-trough drawdown | ≤ 5% |

A `None` profit factor (for example, a small sample with no losing trades) does
**not** pass. Check current standing on the dashboard's live-readiness card or:

```bash
curl https://<bot-domain>/api/live-readiness
```

Only once that returns `"ready": true` do the operator steps apply:

1. Generate **live** Alpaca keys, set `TRADING_RUN_MODE=live` and
   `alpaca_paper=false` on the bot service only.
2. Complete the interactive live-risk acknowledgement prompt.
3. Risk limits (0.5%/trade, 1.5%/day, circuit breaker at 5% drawdown, 3:50pm
   hard exit) are non-negotiable and unchanged.

Passing the gate means the software met its configured evidence threshold. It
does not imply future profitability and does not remove trading risk.

## Verification checklist after §1

Private paper launch only — §2 is deferred, so it has nothing to verify.

- `curl https://<bot-domain>/health` → `{"status":"alive",...}`
- `curl https://<bot-domain>/api/status` → equity/circuit JSON, mode `paper`
- `curl https://<bot-domain>/api/live-readiness` → `"ready": false` with the
  remaining criteria listed (expected until the evidence gate is met)
- An unauthenticated request to a private frontend route is rejected, not served
- Signing in as the allow-listed owner reaches the dashboard
- Scanner rows show current real market data, and no report is labeled `demo`

The full pre-deploy list, including the CI gates and the rollback target, is in
`docs/LAUNCH_CHECKLIST.md`.
