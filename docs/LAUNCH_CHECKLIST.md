# SaaS Launch Checklist

This is the operator-facing checklist for taking the trading-signal
SaaS from "deployed and healthy" to "ready to onboard real users."

The checklist is intentionally manual — every step must be confirmed
by a human before flipping `STRIPE_*` env vars from test to live mode.

---

## Phase 0 — Inventory

- [ ] Branch `claude/trading-saas-launch-K9Lag` is merged or cherry-picked
      into the deploy target.
- [ ] `python -m pytest tests/` passes locally.
- [ ] `pip install -e ".[dev]"` succeeds in the target Python env.

---

## Phase 1 — Required environment

These env vars must be set on the Railway service before launch.

| Env var                              | Purpose                                |
|--------------------------------------|----------------------------------------|
| `STRIPE_SECRET_KEY`                  | Stripe API secret (test or live)       |
| `STRIPE_PREMIUM_PRICE_ID`            | Stripe price id of the premium plan    |
| `STRIPE_WEBHOOK_SECRET`              | Webhook signing secret (whsec_...)     |
| `TRADING_PUBLIC_BASE_URL`            | Public origin (e.g. https://api.host)  |
| `TRADING_API_KEYS_MANIFEST_PATH`     | (recommended) `/app/data/api_keys_manifest.jsonl` |
| `TRADING_API_KEYS_REVOKED_PATH`      | (recommended) `/app/data/api_keys_revoked.jsonl`  |
| `TRADING_STRIPE_PREMIUM_CACHE_PATH`  | `/app/data/stripe_premium_keys.json`   |
| `TRADING_STRIPE_WEBHOOK_EVENTS_PATH` | `/app/data/stripe_webhook_events.jsonl`|
| `TRADING_SAAS_REPORTS_DIR`           | `/app/data/saas_reports`               |
| `TRADING_RUN_MODE`                   | `paper` for default, `live` only when authorized |

Optional (data provider — at least one):

| Env var              | Purpose                                |
|----------------------|----------------------------------------|
| `POLYGON_API_KEY`    | Use Polygon for daily bars (preferred) |
| `ALPACA_API_KEY` + `ALPACA_API_SECRET` | Use Alpaca for daily bars |
| `TRADING_SAAS_DATA_MODE=demo` | Use deterministic demo fixtures (always labels reports `mode: demo`) |

Run the safe verification script:

```bash
python -m scripts.billing_verification
# or, for CI gating:
python -m scripts.billing_verification --strict
```

The script never prints raw secrets. It will reject a mixed
test+live environment as a hard FAIL.

- [ ] `billing_verification` reports PASS for `required_env`.
- [ ] `billing_verification` reports PASS for `stripe_mode_consistency`.
- [ ] `billing_verification` reports PASS for `premium_cache_path`.
- [ ] `billing_verification` reports PASS for `webhook_events_path`.

---

## Phase 2 — Stripe test-mode end-to-end

Refer to `docs/stripe-zero-dollar-test.md` for the full runbook. The
short loop:

1. Set Stripe **test** env vars on Railway.
2. `python scripts/stripe_zero_dollar_test_checkout.py` (creates a
   $0 test price).
3. Deploy the service.
4. `python -m trading_bot.api.keys issue --tier free --label tester`
   on the Railway shell. Capture the raw key once.
5. `curl -H "Authorization: Bearer <KEY>" https://<host>/billing/checkout`
   to receive a Stripe Checkout URL.
6. Complete the test checkout in a browser.
7. Confirm:
   - [ ] `/signals/history` returns 200 for the upgraded key.
   - [ ] `/signals/latest` returns the full report (no `premium.has_full_access: false`).
   - [ ] `python -m trading_bot.api.keys premium-check --key-hash <H>`
         prints `is_premium=yes`.
   - [ ] `python -m trading_bot.api.keys webhook-events --limit 5`
         shows the `customer.subscription.created` row.
8. Cancel the test subscription in the Stripe dashboard.
9. Confirm:
   - [ ] `premium-check` now prints `is_premium=no`.
   - [ ] `/signals/history` returns 403 with the upgrade payload.

---

## Phase 3 — Generate the first signal report

```bash
# Demo data (safe to run anywhere):
TRADING_SAAS_DATA_MODE=demo python -m trading_bot.saas generate

# Polygon-backed (when Polygon is configured):
python -m trading_bot.saas generate

# Custom universe:
python -m trading_bot.saas generate --universe AAPL,MSFT,NVDA,TSLA
```

- [ ] `python -m trading_bot.saas list` shows at least one date.
- [ ] `curl https://<host>/signals/latest` returns 200 (preview).
- [ ] `curl -H 'Authorization: Bearer <KEY>' https://<host>/signals/latest`
      returns 200 with the expected tier projection.

---

## Phase 4 — Public surface smoke

```bash
# Liveness
curl -fsS https://<host>/health

# Public preview JSON
curl -fsS -H "Accept: application/json" https://<host>/

# Free preview
curl -fsS https://<host>/signals/latest

# Premium archive (requires premium key)
curl -fsS -H "Authorization: Bearer <KEY>" https://<host>/signals/history

# Launch dashboard
curl -fsS https://<host>/launch | head -n 5
```

- [ ] All five commands return 2xx.
- [ ] `/launch` HTML carries the `Not financial advice` disclaimer.

---

## Phase 5 — Live-mode handoff (only when authorized)

1. Re-run `billing_verification` against live env. Confirm
   `stripe_secret_mode: live`.
2. Confirm the live webhook endpoint is registered with the same
   signing secret as `STRIPE_WEBHOOK_SECRET`.
3. Run a real (non-zero) test purchase with a card you control,
   confirm:
   - [ ] Webhook event appears in `webhook-events` log.
   - [ ] Premium cache shows the upgraded hash.
   - [ ] Cancellation downgrades within minutes.

---

## Phase 6 — Rollback plan

If anything goes wrong post-launch:

| Symptom                     | Action                                              |
|-----------------------------|------------------------------------------------------|
| Webhook signature failures  | Re-issue webhook secret in Stripe → set on Railway  |
| Mass-premium-grant by mistake | `python -m trading_bot.api.keys premium-remove --key-hash <H>` per affected hash |
| Stripe outage               | Service stays up; new checkouts return 502/503; existing premium remains in cache |
| Bad signal report           | Delete the offending file under `$TRADING_SAAS_REPORTS_DIR`; previous date becomes "latest" automatically |
| Mixed test/live env         | `billing_verification --strict` exits 1; rotate keys to a single mode |
| Compromised key             | `python -m trading_bot.api.keys revoke --key-hash <H> --reason compromised` |

Never rotate `STRIPE_WEBHOOK_SECRET` and `STRIPE_SECRET_KEY` in
opposite directions — verify with `billing_verification` after every
env change.

---

## Disclaimer

This product produces transparent rule-based signal recommendations
for research and education only. It is not financial advice. Demo
mode uses deterministic synthetic fixtures and is always labeled
`mode: demo`. No trades are executed via this surface.
