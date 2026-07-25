# Private Paper Launch — Checklist

Authoritative contract: `docs/PRIVATE_PAPER_LAUNCH.md`. If this checklist ever
disagrees with that document, the contract wins and this file is the bug.

This release is a **single-owner, paper-trading** deployment. Public signup,
billing, share links, growth projections, demo signals, and mobile order routing
are disabled. Do not re-enable them from this checklist — that is the deferred
public-product gate, which requires legal review first.

## 1. Automated gates (CI enforces all of these)

Run from the repo root. Every one must pass before deploying.

```bash
.venv/bin/python -m pytest tests/ -q          # 2,773 tests
.venv/bin/python -m pip_audit                 # zero known vulnerabilities

cd frontend
npm ci
npm audit --omit=dev --audit-level=high       # must exit 0
npm run lint
TRADING_PRIVATE_MODE=true \
  TRADING_PRIVATE_OWNER_EMAILS=owner@example.invalid npm run build
```

`.github/workflows/ci.yml` runs this same set on every push and pull request.

- [ ] Backend suite passes
- [ ] `pip-audit` clean
- [ ] `npm audit --omit=dev --audit-level=high` exits 0
- [ ] Frontend lint clean
- [ ] Frontend production build passes

## 2. Private access configuration

The frontend fails **private by default** in production: if
`TRADING_PRIVATE_MODE` is unset, `NODE_ENV=production` still forces private mode
(`frontend/src/lib/access-policy.ts`). Public exposure is never accidental.

Set in Vercel (production):

- [ ] `TRADING_PRIVATE_MODE=true`
- [ ] `TRADING_PRIVATE_OWNER_EMAILS` — the single owner address, lowercase
- [ ] `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY`
- [ ] `NEXT_PUBLIC_DASHBOARD_URL` — the Railway bot service domain
- [ ] `TRADING_DASHBOARD_API_KEY` — shared secret, must equal the value on the
      `momentum-bot-core` Railway service
- [ ] `TRADING_BACKEND_API_KEY` — must equal `TRADING_API_KEY` on the
      `momentum-trading-bot` (analytics) Railway service

The last two are easy to miss: without them the owner signs in successfully and
the dashboard renders, but every data call returns
`503 "Dashboard private key is not configured."` from
`frontend/src/app/api/backend/[...path]/route.ts`. Set them on both sides or
neither — a mismatch fails as 401 from the bot instead.

> **Vercel env vars on this project are `sensitive` type — write-only.** Neither
> `vercel env pull` nor the REST API with `decrypt=true` can read them back; both
> return an empty string for values that are definitely set. Do not treat an
> empty readback as proof a variable is unset. Verify behaviorally instead
> (see §4), and note that env var changes need a redeploy to take effect.

With private mode on and the allow-list empty, owner routes return 503 rather
than opening up. That is intentional; fix the allow-list, never the check.

Only `/login`, `/privacy`, `/terms`, and `/auth/callback` stay reachable
unauthenticated. Everything else requires an allow-listed owner session.

## 3. Backend (Railway)

- [ ] `SERVICE_ROLE=bot` service is running (see `docs/LAUNCH_RUNBOOK.md` §1)
- [ ] `TRADING_RUN_MODE=paper`
- [ ] `ALPACA_API_KEY` / `ALPACA_API_SECRET` set (paper credentials)
- [ ] `POLYGON_API_KEY` set
- [ ] `TRADING_LOG_JSON=true`
- [ ] Volume mounted at `/app/data` so the journal survives restarts
- [ ] Container runs as a non-root user

## 4. Production smoke tests

Run against the deployed URLs after deploying. All five must hold.

- [ ] An unauthenticated request to a private route is rejected (401/403), not served
- [ ] An allow-listed owner session reaches the dashboard
- [ ] The core bot reports `paper` — `curl https://<bot-domain>/api/status`
- [ ] Scanner rows are current real market data, not fixtures
- [ ] No report is labeled `demo`

### The 401-vs-503 probe

Because env vars cannot be read back, this is the reliable way to prove the
owner allow-list actually took effect:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<app-domain>/api/backend/status
```

- **401** `{"error":"login_required"}` — correct. The request cleared the
  allow-list check and failed only on the missing session.
- **503** `{"error":"The private owner allow-list is not configured."}` —
  `TRADING_PRIVATE_OWNER_EMAILS` is empty or missing. You would be locked out.

The same distinction works against the bot service directly:

```bash
curl -s https://<bot-domain>/api/status                       # expect 401
curl -s -H "Authorization: Bearer $KEY" https://<bot-domain>/api/status   # expect 200
```

A `503 "private dashboard authentication is not configured"` there means
`TRADING_DASHBOARD_API_KEY` is unset on the bot service, **or** it was set with
`--skip-deploys` and the container has not restarted yet.

`/health` may stay public for hosting infrastructure. Trading state, reports,
positions, trades, and operational dashboards must not.

## 5. Before deploying

- [ ] Record the rollback target: `git rev-parse HEAD` of the currently deployed
      production commit, written down before the new deploy starts

## Not in this release

- **Live money.** Blocked by the evidence gate in the contract and enforced in
  `trading_bot/main.py::_assert_live_evidence_gate`, which raises before an
  `AlpacaBroker` is constructed in live mode. Progress is visible on the
  dashboard's live-readiness card and at `/api/live-readiness`.
- **Native mobile.** Excluded — see `mobile/momentum-trading/PRIVATE_BETA_STATUS.md`.
- **Payments and public signup.** Deferred behind the public-product gate.
