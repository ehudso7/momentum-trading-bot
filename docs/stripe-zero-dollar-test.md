# Stripe Zero-Dollar Test Mode Runbook

This runbook walks an operator through end-to-end validation of the
Stripe billing pipeline — checkout → webhook → premium entitlement →
cancellation/downgrade — without ever charging a real card. It uses
a Stripe test-mode secret and a $0 monthly recurring price.

The whole flow is operator-run from a shell. There are no public
sign-up endpoints, no forms, and no automatic mutation of Railway
variables.

---

## What you'll need

* A Stripe **test** secret key (`sk_test_…`). Get one from
  <https://dashboard.stripe.com/test/apikeys>.
* The `railway` CLI logged into the production project.
* A test-mode **webhook signing secret** for the Railway-hosted
  endpoint. Created at
  <https://dashboard.stripe.com/test/webhooks> by adding the
  endpoint `https://<your-railway-host>/webhook/stripe` and
  selecting at minimum these events:
  * `checkout.session.completed`
  * `customer.subscription.created`
  * `customer.subscription.deleted`
  * `invoice.payment_failed`
* A test API key issued from `python -m trading_bot.api.keys issue`
  (Phase 6.0/6.1) — the key the user will pass to the API.

---

## Step a — Generate or reuse the $0 test price

```bash
export STRIPE_SECRET_KEY=sk_test_...

python scripts/stripe_zero_dollar_test_checkout.py \
    --print-railway-commands
```

The script idempotently creates (or reuses) a Stripe product called
`Momentum Trading Premium Test` and a recurring `$0 / month` price.
On success it prints, for example:

```
Stripe test product + price ready:
  product_id   : prod_TestAbc123
  price_id     : price_TestXyz789
  product_name : Momentum Trading Premium Test
  currency     : usd
  interval     : month
  product_reused: False
  price_reused : False

# Run from a shell with the railway CLI logged in:
# (replace <your-sk_test_...> with your actual test secret)

railway variables --set STRIPE_SECRET_KEY=<your-sk_test_...>
railway variables --set STRIPE_API_KEY=<your-sk_test_...>
railway variables --set STRIPE_PREMIUM_PRICE_ID=price_TestXyz789
railway variables --set STRIPE_PRICE_ID_PREMIUM=price_TestXyz789

# Don't forget the webhook secret (test-mode endpoint):
# railway variables --set STRIPE_WEBHOOK_SECRET=whsec_...
```

**Safety guards built into the script:**

* Refuses to run against `sk_live_…` unless you also pass
  `--allow-live`. Even with that flag, the price it creates is
  still $0 — so worst case is operator confusion, not a real
  charge.
* Refuses unrecognised secret prefixes (`pk_…`, typos, etc.).
* The Stripe secret is **never** echoed in the script output —
  the printed `railway variables --set …` snippets use the
  placeholder `<your-sk_test_...>` so the snippet is safe to
  share in tickets / pull-request bodies / chat.
* Re-running the script is a no-op if the test product + $0
  monthly price already exist (`product_reused: True`,
  `price_reused: True`).

---

## Step b — Set Railway test-mode env vars

Run the four `railway variables --set` commands the script printed,
substituting `<your-sk_test_...>` with the real test secret. Add
the webhook secret too:

```bash
railway variables --set STRIPE_SECRET_KEY=sk_test_...real...
railway variables --set STRIPE_API_KEY=sk_test_...real...
railway variables --set STRIPE_PREMIUM_PRICE_ID=price_TestXyz789
railway variables --set STRIPE_PRICE_ID_PREMIUM=price_TestXyz789
railway variables --set STRIPE_WEBHOOK_SECRET=whsec_...test...
```

**Why both env-var names?** The server reads either, with the
`STRIPE_SECRET_KEY` / `STRIPE_PREMIUM_PRICE_ID` names taking
precedence. Setting both keeps any deployed code path that still
references the legacy names working.

---

## Step c — Deploy

`railway up` (or whatever your normal deploy step is). Wait for
the new release to roll over. Confirm the env vars are present
on the running container:

```bash
railway variables --kv | grep STRIPE
```

(Don't paste the output anywhere — it includes the secret values.)

---

## Step d — Issue a test user key + create checkout

Issue a fresh free-tier key for the test:

```bash
python -m trading_bot.api.keys issue \
    --tier free \
    --label "stripe-test-user" \
    --ref "stripe-test-mode"
```

Record the printed `api_key` — call it `$TEST_KEY`. The key is also
hashed into `data/api_keys_manifest.jsonl` (Phase 6.0/6.1) so you
can later confirm it was the one that converted.

Now ask the server for a Stripe Checkout URL using the operator
Checkout helper:

```bash
python -m trading_bot.api.billing checkout \
    --api-key "$TEST_KEY" \
    --success-url https://example.com/billing/success \
    --cancel-url  https://example.com/billing/cancel
```

You'll get back something like:

```
checkout_url:         https://checkout.stripe.com/c/pay/cs_test_...
checkout_session_id:  cs_test_...
api_key_hash:         <32-hex>
```

The raw `api_key` is **never** echoed by this command — only the
hash.

---

## Step e — Complete the test checkout

Open the printed `checkout_url` in a browser. Pay with the Stripe
test card:

| Field | Value |
|---|---|
| Card number | `4242 4242 4242 4242` |
| Expiry | any future date |
| CVC | any 3 digits |
| Postal code | any |

Stripe Checkout will charge $0 (the price is zero) and create the
subscription, then fire the webhook to your Railway endpoint.

---

## Step f — Confirm `x-usage-tier: premium`

From a shell with internet access (NOT the in-sandbox shell):

```bash
curl -i https://<your-railway-host>/reports/latest \
    -H "Authorization: Bearer $TEST_KEY"
```

Expected response headers (look for these):

* `200 OK` (or `404` if no daily report has been generated yet —
  the auth still succeeded)
* `x-usage-tier: premium` *(if the server emits this header in
  your deployment)*

If you see `x-usage-tier: free` or `403`, the webhook did not
flip your test key to premium. Common causes:

* The webhook secret on Railway doesn't match the one Stripe used
  to sign the call. The signature check fails-closed, so the
  premium-cache update never runs.
* The Checkout session was created without
  `metadata[api_key]=$TEST_KEY` — the webhook handler can't tell
  whose key to promote. (The operator-Checkout CLI sets this
  correctly; only manual dashboard-created subscriptions can
  miss it.)
* The Stripe webhook event view shows a non-2xx delivery — open
  it in the dashboard to see the response body.

---

## Step g — Cancel the subscription

In the Stripe dashboard's test-mode subscription view, click
**Cancel subscription** → **Immediately**. Stripe fires
`customer.subscription.deleted` to the webhook endpoint.

(Alternatively, use the Stripe CLI: `stripe subscriptions cancel
sub_… --test-clock-mode` is also fine.)

---

## Step h — Confirm `x-usage-tier: free`

Re-run the same curl:

```bash
curl -i https://<your-railway-host>/reports/latest \
    -H "Authorization: Bearer $TEST_KEY"
```

Expected:

* `x-usage-tier: free` *(if your deployment emits this header)*
* If your test key is also listed in `TRADING_API_KEY` (so it
  passes auth as a free key), you'll get a `200` with free-tier
  data. If it was promoted *only* by Stripe and the Stripe
  cache was its only path to auth, you may now see a `403
  Invalid API key` because the cache no longer contains it.

Both outcomes confirm the entitlement was revoked.

The webhook handler is idempotent on the Stripe `event.id` — if
Stripe re-delivers the same `customer.subscription.deleted` event
(e.g. because of a retry), the second delivery is a no-op. So a
subsequent admin re-grant won't be silently revoked by a stale
re-delivery.

---

## Step i — Restore live Stripe env vars

When you're done testing, swap the env vars back to your live
Stripe values:

```bash
railway variables --set STRIPE_SECRET_KEY=sk_live_...
railway variables --set STRIPE_API_KEY=sk_live_...
railway variables --set STRIPE_PREMIUM_PRICE_ID=price_LIVE_...
railway variables --set STRIPE_PRICE_ID_PREMIUM=price_LIVE_...
railway variables --set STRIPE_WEBHOOK_SECRET=whsec_live_...
```

Re-deploy. The test-mode product + $0 price you created in Stripe
remain in your test mode and don't affect live billing in any way.

---

## What this runbook does **not** do

* It doesn't store card data, PAN, CVV, customer name, or email.
  The webhook payload is parsed for `metadata.api_key` only —
  every other field is read as needed for dispatch but never
  persisted.
* It doesn't weaken any premium / security gate. The webhook
  signature is still verified (`STRIPE_WEBHOOK_SECRET`); free
  tier limits, per-key usage caps, and the SaaS-boundary rules
  are unaffected.
* It doesn't add a public sign-up endpoint or form. Every step
  is an operator-run shell command.
* It doesn't auto-mutate Railway variables from Python. The
  script only **prints** the commands; you copy-paste them.

---

## Dry-run checklist

Before doing this against a real Railway deploy, you can sanity-
check locally:

```bash
# 1. Provisioning script in dry-run mode (still hits Stripe test mode):
STRIPE_SECRET_KEY=sk_test_... \
    python scripts/stripe_zero_dollar_test_checkout.py \
        --print-railway-commands

# 2. Run the unit tests for the script and billing path:
pytest tests/test_stripe_zero_dollar_test_checkout.py tests/test_billing.py -v

# 3. Generate a test API key:
python -m trading_bot.api.keys issue --tier free --label dryrun --ref dryrun

# 4. Check the manifest:
python -m trading_bot.api.keys list --ref dryrun
```

If all four steps succeed locally, the Railway run-through above
is just the same commands against a deployed host.
