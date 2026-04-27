# DEPLOYMENT.md

Operator reference for deploying the read-only SaaS API and the
operator-only key issuance / revocation surface to production
(Railway is the reference target; the same guidance applies to any
PaaS that supports a persistent volume).

This document complements [`CORE_CONTROL.md`](CORE_CONTROL.md), which
specifies what each env var **does**. This document specifies how to
**deploy** the API such that:

* keys issued via `python -m trading_bot.api.keys issue` survive
  restarts and re-deploys;
* revocations land instantly and survive restarts;
* the manifest never leaks raw secrets — neither to git nor to logs;
* Stripe billing keeps working alongside the manifest.


## Production env vars (Phase 6.4)

The two env vars that drive Phase 6.2 / 6.3 are operator-set and
**must** point at a persistent location in production. The defaults
are relative paths that disappear on every container restart, which
is fine for local development but breaks issuance in production.

| Env var | Default (dev) | Recommended (Railway) | Purpose |
|---|---|---|---|
| `TRADING_API_KEYS_MANIFEST_PATH` | `data/api_keys_manifest.jsonl` | `/data/api_keys_manifest.jsonl` | Append-only JSONL of issued keys (hash + tier + ref_code + checkout_session_id). Read by both the auth path and the inspection CLI. |
| `TRADING_API_KEYS_REVOKED_PATH`  | `data/api_keys_revoked.jsonl`  | `/data/api_keys_revoked.jsonl`  | Append-only JSONL of revocation events (hash + timestamp + optional reason). Read by both the auth path and the inspection CLI. |

The same files also need to be:

* **gitignored** — `/data/` is already in `.gitignore` so a manifest
  written by a developer locally cannot be committed by accident.
* **mounted** on a Railway persistent volume — see option B below.

### Other API env vars (already documented in CORE_CONTROL.md)

| Env var | Required for | Notes |
|---|---|---|
| `TRADING_API_KEY` | optional | Single-tenant legacy free-tier key. Phase 6.2 made this optional — the deployment is still configured if a manifest exists. |
| `TRADING_API_PREMIUM_KEYS` | optional | Comma-separated env premium keys. Operator override. |
| `STRIPE_API_KEY` | premium subscriptions | Presence enables Stripe-primary classification. Legacy alias of `STRIPE_SECRET_KEY` for the Phase 7.3 checkout endpoint. |
| `STRIPE_SECRET_KEY` | `POST /billing/checkout` | Stripe REST auth. Preferred over the legacy `STRIPE_API_KEY` for new deployments. (Phase 7.3) |
| `STRIPE_PREMIUM_PRICE_ID` | `POST /billing/checkout` | Stripe Price ID of the premium subscription. Falls back to `STRIPE_PRICE_ID_PREMIUM`. (Phase 7.3) |
| `TRADING_PUBLIC_BASE_URL` | `POST /billing/checkout` | Absolute URL prefix for Stripe success/cancel redirects (e.g. `https://your-host.example.com`). (Phase 7.3) |
| `STRIPE_CHECKOUT_SUCCESS_PATH` | optional | Override the success-redirect path. Default: `/dashboard?checkout=success`. (Phase 7.3) |
| `STRIPE_CHECKOUT_CANCEL_PATH` | optional | Override the cancel-redirect path. Default: `/dashboard?checkout=cancel`. (Phase 7.3) |
| `STRIPE_WEBHOOK_SECRET` | premium subscriptions | Required for `POST /webhook/stripe`. |
| `TRADING_STRIPE_PREMIUM_CACHE_PATH` | premium subscriptions | Recommended Railway path: `/data/stripe_premium_keys.json` (same volume as the manifests). |
| `TRADING_USAGE_ENFORCEMENT_ENABLED` | optional | Phase 8.1 — flip the tier-aware daily-cap layer off with `false`/`0`/`no`/`off`. Default ON. |
| `TRADING_FREE_DAILY_REQUEST_LIMIT` | optional | Phase 8.1 — per-key per-UTC-day cap for free tier. Default `50`. Invalid values fall back to `50`. |
| `TRADING_PREMIUM_DAILY_REQUEST_LIMIT` | optional | Phase 8.1 — per-key per-UTC-day cap for premium tier. Default `1000`. Invalid values fall back to `1000`. |
| `TRADING_USAGE_LIMIT_EXEMPT_PATHS` | optional | Phase 8.1 — comma-separated extra exempt paths joined with the default exempt set (`/`, `/health`, the Phase 7.1 icons, `/webhook/stripe`). |


## Recommended Railway layout

```
+-------------------+      +-----------------------+
|  Railway service  |      |  Persistent volume    |
|  (Dockerfile)     +----->+  mounted at /data     |
+-------------------+      |                       |
                           |  api_keys_manifest.jsonl
                           |  api_keys_revoked.jsonl
                           |  stripe_premium_keys.json
                           |  api_growth.jsonl       (Phase 5.1)
                           |  api_usage.jsonl        (Phase 4.6)
                           |  api_access_audit.jsonl (Phase 4.4)
                           |  api_conversions.jsonl  (Phase 4.9)
                           +-----------------------+
```

Set these env vars in the Railway dashboard so every operator file
lives on the persistent volume:

```
TRADING_API_KEYS_MANIFEST_PATH=/data/api_keys_manifest.jsonl
TRADING_API_KEYS_REVOKED_PATH=/data/api_keys_revoked.jsonl
TRADING_STRIPE_PREMIUM_CACHE_PATH=/data/stripe_premium_keys.json
TRADING_API_GROWTH_LOG_PATH=/data/api_growth.jsonl
TRADING_API_USAGE_LOG_PATH=/data/api_usage.jsonl
TRADING_API_AUDIT_LOG_PATH=/data/api_access_audit.jsonl
TRADING_API_CONVERSION_LOG_PATH=/data/api_conversions.jsonl
TRADING_API_UPGRADE_EVENTS_LOG_PATH=/data/api_upgrade_events.jsonl
TRADING_LOG_JSON=true
```


## Issuing, listing, and revoking keys against production

All Phase 6 commands accept `--manifest-path` and (where it makes
sense) `--revoked-path`, so an operator can target the production
files explicitly without exporting env vars.

### Issue a key against the production manifest

```bash
# Inside a Railway shell (recommended — see Option A below):
python -m trading_bot.api.keys issue \
    --tier free \
    --label "alice@example.com" \
    --ref "twitter-launch_2026" \
    --manifest-path /data/api_keys_manifest.jsonl
```

The raw `api_key` is printed to stdout exactly once — capture it
out-of-band and hand-deliver it to the customer. The manifest stores
only `key_hash`, `label_hash`, `tier`, `created_at`, `ref_code`,
`checkout_session_id`. The raw key is never persisted.

### List keys (active by default)

```bash
python -m trading_bot.api.keys list \
    --manifest-path /data/api_keys_manifest.jsonl \
    --revoked-path  /data/api_keys_revoked.jsonl
```

Filters and JSON output:

```bash
python -m trading_bot.api.keys list --tier premium \
    --manifest-path /data/api_keys_manifest.jsonl

python -m trading_bot.api.keys list --ref twitter-launch_2026 \
    --manifest-path /data/api_keys_manifest.jsonl

python -m trading_bot.api.keys list --include-revoked --json \
    --manifest-path /data/api_keys_manifest.jsonl \
    --revoked-path  /data/api_keys_revoked.jsonl
```

### Inspect a single key

```bash
python -m trading_bot.api.keys show \
    --key-hash <hash from issuance> \
    --manifest-path /data/api_keys_manifest.jsonl \
    --revoked-path  /data/api_keys_revoked.jsonl
```

### Aggregate counts

```bash
python -m trading_bot.api.keys stats \
    --manifest-path /data/api_keys_manifest.jsonl \
    --revoked-path  /data/api_keys_revoked.jsonl
```

### Revoke a key

By hash (preferred — the hash is in the issuance manifest and in
your operator notes):

```bash
python -m trading_bot.api.keys revoke \
    --key-hash <hash from issuance> \
    --reason "user-requested rotation" \
    --revoked-path /data/api_keys_revoked.jsonl
```

By raw key (only when the operator has the raw key on hand — it is
hashed in-process and discarded; the raw value never reaches disk):

```bash
python -m trading_bot.api.keys revoke \
    --api-key "<the raw key>" \
    --reason "leaked on twitter" \
    --revoked-path /data/api_keys_revoked.jsonl
```

The next request bearing that key returns `403 Invalid API key` —
the auth path's `key_store` cache picks up the new revocation row
on the next request via mtime-keyed hot reload (no server restart).


## Deployment options

Three production patterns. Pick exactly one — mixing them silently
desynchronises your manifest across environments.

### Option A — issue from a Railway shell against the mounted volume *(recommended)*

The cleanest pattern. Operator opens an interactive shell on the
running Railway service and runs `python -m trading_bot.api.keys
issue` against `/data/api_keys_manifest.jsonl` (the same path the
auth path is reading). Newly issued keys authenticate on the next
request without any restart.

Pros:
* No file copying. The manifest lives in exactly one place.
* No git involvement. The manifest never enters the repository.
* Survives restarts (volume is persistent).
* `revoke` lands instantly via mtime hot-reload.

Cons:
* Requires shell access to the Railway service.

```
# Railway → service → "Shell" tab (or `railway run bash` locally).
python -m trading_bot.api.keys issue \
    --tier free \
    --label "alice@example.com" \
    --manifest-path /data/api_keys_manifest.jsonl
```

### Option B — mount a persistent volume and let the API write the manifest

Identical to A in every operational respect; the only difference is
that you *first* declare the persistent volume in the Railway
service config (Settings → Volumes → Add volume → Mount path:
`/data`), then set the env vars from the table above. Once the
volume exists, **option A is how you actually issue keys** — option B
is the one-time infra step that makes A work.

This is the option that produces a durable, restart-survivable
deployment. Without a persistent volume, Railway's container
filesystem is destroyed on every redeploy and every issued key
silently disappears.

### Option C — bake the manifest into the deploy artifact *(NOT RECOMMENDED)*

Adding `data/api_keys_manifest.jsonl` to the git repo so it ships
with the Docker image. Strongly discouraged because:

* The manifest's `label_hash` is a hash, but it's still operator
  metadata that benefits from staying out of source control.
* Every issuance becomes a deploy.
* Revocations also become deploys — and a deploy that ships a
  revocation log overrides any newer rows that exist on the
  Railway volume from a prior shell-based revoke. Easy to lose
  revocations this way.
* `/data/` is already in `.gitignore` for a reason.

If you must use Option C (e.g. the platform genuinely doesn't
support persistent volumes), make absolutely sure the same
manifest is the source of truth across every environment, and that
revocations are also routed through the same git workflow.


## ⚠️ Local manifest does NOT authenticate against Railway

A common mistake: an operator runs

```bash
# On their laptop:
python -m trading_bot.api.keys issue --tier free --label alice
```

then `curl`s the production endpoint with the new key and gets
`403`. **This is expected.** The key was written to the operator's
*local* `data/api_keys_manifest.jsonl`, which Railway has never
seen.

Two recovery paths:

1. **Re-issue against the production manifest** (Option A above).
   Discard the locally-issued key — it never authenticated and was
   never delivered to a customer, so no rotation is required.
2. **Hand-replicate the row.** Append the same JSONL row to the
   Railway-volume manifest. Only do this if you've already shipped
   the locally-issued raw key to a customer; otherwise re-issuing is
   simpler and avoids drift.

`docs/CORE_CONTROL.md` § Phase 6.2 covers what the server does on
each `403` / `503` distinction — see that document for the precise
auth precedence ordering.


## Privacy posture (production deployment)

Production deployments inherit every Phase 6.0 / 6.2 / 6.3 privacy
invariant verbatim. To restate them in deployment terms:

* The raw `api_key` is never persisted. It is printed to stdout once
  by `keys issue`, hashed once by `keys revoke --api-key`, and
  hashed once per request by the auth path. None of those code
  paths writes the raw value to disk.
* The raw `label` (e.g. customer email) is never persisted. Only
  `SHA-256(label)[:32]` lands on disk.
* The Stripe Checkout `url` is never persisted (only the short-lived
  `checkout_session_id`).
* No customer email, IP, name, or payment field is ever stored.
* The inspection CLI (`list` / `show` / `stats`) projects rows
  through a fixed allow-list; even if a future schema bump quietly
  adds a new field, it will not surface in operator output.

The above are pinned by `tests/test_keys.py` and
`tests/test_api_server.py`. If a future change appears to weaken
any of them, those tests will fail before the change can ship.


## Launch Day Checklist (Phase 7.2)

Single-page runbook for the moment you flip on real-traffic. Every
step has a corresponding test that fails if the contract regresses.

### 1 — Production start command is locked

`railway.toml` must look exactly like this:

```toml
[build]
builder = "DOCKERFILE"
dockerfilePath = "Dockerfile"

[deploy]
startCommand = "/bin/sh -c \"chmod -R 777 /app/data || true && uvicorn trading_bot.api.server:app --host 0.0.0.0 --port 8080\""
healthcheckPath = "/health"
healthcheckTimeout = 5
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

The leading `chmod` ensures the persistent volume mounted at
`/app/data` is writable by the unprivileged uvicorn process. Pinned
by `tests/test_launch_check.py::TestRailwayTomlLockdown`.

### 2 — Railway env vars all point at the persistent volume

In Railway → service → Variables:

```
TRADING_API_KEYS_MANIFEST_PATH=/app/data/api_keys_manifest.jsonl
TRADING_API_KEYS_REVOKED_PATH=/app/data/api_keys_revoked.jsonl
TRADING_STRIPE_PREMIUM_CACHE_PATH=/app/data/stripe_premium_keys.json
TRADING_API_USAGE_LOG_PATH=/app/data/api_usage.jsonl
TRADING_API_AUDIT_LOG_PATH=/app/data/api_access_audit.jsonl
TRADING_API_GROWTH_LOG_PATH=/app/data/api_growth.jsonl
TRADING_API_CONVERSION_LOG_PATH=/app/data/api_conversions.jsonl
TRADING_API_UPGRADE_EVENTS_LOG_PATH=/app/data/api_upgrade_events.jsonl
STRIPE_API_KEY=sk_live_…           # only when ready to accept payments
STRIPE_WEBHOOK_SECRET=whsec_…       # only when Stripe is wired
TRADING_LOG_JSON=true

# Phase 8.1 — tier-aware usage enforcement (defaults shown; all
# four are optional). Tighten before launching, loosen as the
# product matures.
TRADING_USAGE_ENFORCEMENT_ENABLED=true
TRADING_FREE_DAILY_REQUEST_LIMIT=50
TRADING_PREMIUM_DAILY_REQUEST_LIMIT=1000
# TRADING_USAGE_LIMIT_EXEMPT_PATHS=/some/extra/path
```

#### Phase 8.1 metering behaviour at a glance

A free user under the cap sees their headroom on every response:

```
$ curl -i https://your-host.example.com/reports/latest \
       -H "Authorization: Bearer <free-key>"
HTTP/1.1 200 OK
X-Usage-Limit: 50
X-Usage-Remaining: 47
X-Usage-Tier: free
…
```

A free user that hits the cap is rejected with a documented body
plus `Retry-After`:

```
$ curl -i https://your-host.example.com/reports/latest \
       -H "Authorization: Bearer <free-key>"
HTTP/1.1 429 Too Many Requests
X-Usage-Limit: 50
X-Usage-Remaining: 0
X-Usage-Tier: free
Retry-After: 27384
Content-Type: application/json

{"detail":"usage limit reached — upgrade for higher limits"}
```

#### Phase 8.2 feature differentiation at a glance

| Feature | Free | Premium |
|---|---|---|
| `GET /reports/latest` | curated subset (`report_type`, `report_date`, `scorer_fingerprint`, `totals`, `promotion_readiness`) plus an `upgrade` envelope. Premium-only deep stats (tier / reason / regime / decile / shadow-filter / guardrails / sources) are dropped. | full sanitised report. |
| `GET /reports/history` | `403 {"detail": "premium feature — upgrade required"}` with `X-Usage-Tier: free` | `{"count": N, "dates": ["YYYY-MM-DD", ...]}` |
| `GET /dashboard` | banner + projected report (deep-stat sections hidden) | full HTML dashboard |

Worked example — free response to `GET /reports/latest`:

```json
{
  "report_type": "daily_alpha_validation",
  "report_date": "2026-04-25",
  "scorer_fingerprint": "f0b2…",
  "totals": {"alpha_rows": 100, "buy_rows": 25, "skip_rows": 75},
  "promotion_readiness": {"ready": true, "consecutive_passing_days": 21},
  "tier": "free",
  "upgrade": {
    "detail": "premium feature — upgrade required",
    "hint": "upgrade for full access"
  }
}
```

Worked example — free `GET /reports/history` (premium-only):

```
$ curl -i https://your-host.example.com/reports/history \
       -H "Authorization: Bearer <free-key>"
HTTP/1.1 403 Forbidden
X-Usage-Tier: free
X-Usage-Limit: 50
X-Usage-Remaining: 46
Content-Type: application/json

{"detail":"premium feature — upgrade required"}
```

Note that the 403 still consumes the caller's daily usage quota
(Phase 8.1) — gated requests count.

#### Phase 8.3 upgrade-pressure payload

When `STRIPE_SECRET_KEY` + `STRIPE_PREMIUM_PRICE_ID` +
`TRADING_PUBLIC_BASE_URL` are configured (the same env vars that
power `POST /billing/checkout`), every free-tier rejection or
curated response carries a Phase 8.3 ``upgrade`` payload with a
freshly-minted Stripe Checkout URL the client can redirect the
user to immediately:

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

Three `reason` values surface:

| Where | `reason` | `required` |
|---|---|---|
| 429 from Phase 8.1 daily-cap exhaustion | `usage_limit` | `true` |
| 403 from Phase 8.2 premium-only feature | `feature_locked` | `true` |
| 200 from Phase 8.2 free `/reports/latest` curated body | `limited_access` | `false` |

Premium callers never trigger a Stripe call on these paths, so
their responses remain identical to the pre-Phase-8.3 shape.

If Stripe is unreachable or `TRADING_PUBLIC_BASE_URL` is unset,
the payload is dropped silently — the base response (429 / 403 /
200) is unchanged. Operators can disable the 429 trigger point by
flipping `TRADING_USAGE_ENFORCEMENT_ENABLED=false`, or disable
all three Phase 8.3 payloads by unsetting `TRADING_PUBLIC_BASE_URL`.

Performance note: every free-tier 429 / 403 / curated-200 makes
ONE outbound Stripe POST. The Stripe Session itself is
short-lived (Stripe expires it after 24h by default) and is NOT
persisted server-side — checkout_url leak-guard is pinned by
`tests/test_api_server.py::TestPhase83CheckoutUrlNotPersisted`.

A premium user (promoted via Stripe Checkout / webhook) gets the
higher cap automatically — no env edits, no restart:

```
$ curl -i https://your-host.example.com/reports/latest \
       -H "Authorization: Bearer <same-key-after-upgrade>"
HTTP/1.1 200 OK
X-Usage-Limit: 1000
X-Usage-Remaining: 939
X-Usage-Tier: premium
```

To temporarily disable the entire metering layer (e.g., during an
incident or migration):

```
$ railway variables --set TRADING_USAGE_ENFORCEMENT_ENABLED=false
$ railway redeploy
```

### 3 — Issue the real user keys (Railway shell only)

```bash
# Inside `railway ssh` or the Railway dashboard "Shell" tab:
python -m trading_bot.api.keys issue \
    --tier free \
    --label "alice@example.com" \
    --manifest-path /app/data/api_keys_manifest.jsonl
```

The raw key is printed once — capture it out-of-band and hand-deliver
it to the customer. Never paste it into chat, ticket systems,
screenshots, or any logging surface.

### 4 — Revoke any test keys you issued during dev

If you issued anything during testing, either revoke each hash:

```bash
python -m trading_bot.api.keys revoke \
    --key-hash <hash> \
    --reason "test key cleanup" \
    --revoked-path /app/data/api_keys_revoked.jsonl
```

…or batch-revoke a list:

```bash
python -m trading_bot.api.keys revoke-many \
    --key-hash <hash1> --key-hash <hash2> --key-hash <hash3> \
    --reason "test key cleanup" \
    --revoked-path /app/data/api_keys_revoked.jsonl
```

Confirm the cleanup with:

```bash
python -m trading_bot.api.keys list --include-revoked \
    --manifest-path /app/data/api_keys_manifest.jsonl \
    --revoked-path  /app/data/api_keys_revoked.jsonl
```

Every test hash should now appear with `revoked=yes`.

### 5 — Run launch_check on the Railway shell

```bash
python -m trading_bot.api.launch_check
```

Exits 0 + prints `READY` when every env var is set, every path is
writable, and nothing points at `/tmp` or repo-local `data/`. Exits
1 with `NOT READY` plus a line per failed check otherwise. Fix any
`[FAIL]` row and re-run before continuing.

### 6 — Run the production HTTP smoke test

From the same Railway shell:

```bash
python -m trading_bot.api.launch_check --smoke \
    --base-url https://your-host.example.com \
    --api-key <real-issued-key>
```

This combines step 5 with the Phase 6.5 HTTP smoke runner — public
landing, health, 401 / 403 / 200 protected paths, and `/dashboard`.
The supplied `--api-key` is hashed in-process; the raw value never
prints. Exits 0 only when every check passes.

### 7 — Wire Stripe AFTER auth passes

Only after step 6 reports `READY`:

* In the Stripe Dashboard, create the webhook endpoint pointing at
  `https://your-host.example.com/webhook/stripe`.
* Subscribe to `customer.subscription.created`,
  `customer.subscription.deleted`, `invoice.payment_failed`.
* Copy the signing secret into `STRIPE_WEBHOOK_SECRET` and redeploy.
* Smoke-test one real Checkout to confirm the webhook flips the
  customer's tier to premium. The webhook accepts BOTH identity
  shapes:
  - `metadata[api_key]` (Phase 4.7 / 4.8 — operator-CLI
    `keys issue --checkout` flow)
  - `metadata[key_hash]` (Phase 7.3 — `POST /billing/checkout` flow,
    no raw key on the wire)

  Either one is verified against the issuance manifest before the
  premium cache is mutated. Revoked hashes can never be re-promoted
  by a Stripe replay regardless of which identity field carries
  them. See `docs/CORE_CONTROL.md` § Phase 7.4 for the full table.

* If both `STRIPE_API_KEY` (legacy) and `STRIPE_SECRET_KEY` (Phase
  7.3 preferred) are set, they should hold the same Stripe-side
  credential — `is_stripe_configured()` accepts either. Operators
  rolling forward from a Phase 4.7 deployment may set just
  `STRIPE_SECRET_KEY` and remove the legacy alias.

### 8 — Hand-deliver keys securely

Channels (in descending order of preference):

1. The customer's authenticated account on a separate platform
   you already trust.
2. End-to-end encrypted messaging (Signal, iMessage, etc.).
3. A vault link that auto-destructs after one read.

Never send a raw key over plain email, plain SMS, public chat, or
any unencrypted channel.

### Rollback signals

If anything looks off after launch:

* `python -m trading_bot.api.keys list --include-revoked` quickly
  shows whether the manifest looks sane.
* `python -m trading_bot.api.keys stats --json` gives total /
  active / revoked counts at a glance.
* `python -m trading_bot.api.launch_check --smoke ...` is safe to
  re-run at any time — six lightweight HTTP requests against
  documented endpoints.
* To kill a leaked key immediately:
  `python -m trading_bot.api.keys revoke --api-key <leaked-raw> \
   --revoked-path /app/data/api_keys_revoked.jsonl --reason leaked`
  — the next request bearing that key 403s within the mtime
  reload window (sub-second in practice).
