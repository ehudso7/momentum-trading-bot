# MomentumForge Enhanced — Deployment Guide

## Architecture

| Component | Platform | URL |
|-----------|----------|-----|
| Frontend (Next.js 15) | Vercel | `https://your-app.vercel.app` |
| Backend API (Python/FastAPI) | Railway | `https://momentum-trading-bot-production.up.railway.app` |
| Auth + Trade Storage | Supabase | `https://your-project.supabase.co` |
| Payments | Stripe | Webhook → Vercel API route |

## Repo layout

This is a monorepo:

- `trading_bot/` — Python API (deployed to **Railway**)
- `frontend/` — Next.js dashboard (deployed to **Vercel**)
- `mobile/` — React Native app
- `supabase/migrations/` — Auth + trade sync schema

## 1. Local Development

```bash
cd frontend
cp .env.example .env.local
# Edit .env.local with your keys
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

### Required `.env.local` values

```env
NEXT_PUBLIC_BACKEND_URL=https://momentum-trading-bot-production.up.railway.app
NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJ...
```

Optional:
```env
NEXT_PUBLIC_DASHBOARD_URL=https://your-trading-bot-dashboard.railway.app
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
```

## 2. Supabase Setup

1. Create a project at [supabase.com](https://supabase.com)
2. Run the migration in SQL Editor:

```bash
# Or via Supabase CLI:
supabase db push
```

Migration file: `supabase/migrations/001_trades.sql`

3. Enable Email auth in Authentication → Providers
4. Copy Project URL and anon key to `.env.local`

## 3. Deploy Frontend to Vercel

```bash
cd frontend
npx vercel
```

Set environment variables in Vercel dashboard:

| Variable | Value |
|----------|-------|
| `NEXT_PUBLIC_BACKEND_URL` | Your Railway backend URL |
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Supabase anon key |
| `STRIPE_SECRET_KEY` | Stripe secret key |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |

Deploy to production:

```bash
npx vercel --prod
```

## 4. Backend (Railway) — Already Live

Your existing Railway backend from `ehudso7/momentum-trading-bot` should already be running. Verify:

```bash
curl https://momentum-trading-bot-production.up.railway.app/health
```

Expected response:
```json
{"status":"ok","service":"momentum-trading-bot-analytics","timestamp":"..."}
```

### Get Your API Key for MomentumForge AI (Do This Now)

**The absolute fastest way to a working key:**

```bash
# 1. Generate the key + get perfect copy-paste instructions
python scripts/momentumforge_api_key.py
```

The script prints:
- A brand new secure key
- The exact Railway Variables command / UI steps (quick `TRADING_API_KEY` method)
- The exact command to run inside a Railway Shell if you want the full production manifest system

**Manual quick path (no script):**

1. Generate any strong random string (or use the one printed by the script above).
2. In Railway → your service → **Variables**:
   - Name: `TRADING_API_KEY`
   - Value: `the-key-you-just-generated`
3. Redeploy.
4. In MomentumForge AI frontend:
   - Go to **Profile** (or `/profile`)
   - Paste the exact same key into the API Key box
   - Click **Save**
5. Click the **"Test connection"** button right there in the form.

After this the frontend can call your Railway signals, reports, and the live trading dashboard.

For multiple users / revocation / premium tiers later, re-run the script — it will show you the production manifest commands using your `/app/data` volume.

> The key is stored only in the browser (localStorage) and sent as `Authorization: Bearer ...`. Never commit it.

### PEAK EXPERIENCE: Watch Your Money Grow Exponentially (MomentumForge AI)

Once connected:

1. Run the core worker:
   ```bash
   trading-bot --mode paper --dashboard-port 8080
   ```

2. Run the peaked MomentumForge AI frontend (`cd frontend && npm run dev`).

3. In the AI dashboard you now have the **Growth Simulator** — the crown jewel.
   - Live exponential projections using real compound math (see `trading_bot/risk/compound.py`).
   - Tune win rate / R:R / trade frequency to see different futures.
   - 12-month trajectory chart.
   - "Time to 2x" calculator.

4. Run the ultimate launcher for the full peaked test flow:
   ```bash
   python3 scripts/peak_momentumforge.py
   ```

This gives you copy-paste commands for everything + explains how the AI layer + core bot work together so you can "set it and watch (paper) money grow".

The MomentumForge AI frontend is now the god-tier interface that does the intelligence, projections, and beautiful monitoring while the battle-tested core bot does the safe execution.

## 5. Stripe Webhook

1. In Stripe Dashboard → Developers → Webhooks
2. Add endpoint: `https://your-app.vercel.app/api/stripe/webhook`
3. Select events:
   - `checkout.session.completed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
4. Copy signing secret to `STRIPE_WEBHOOK_SECRET` in Vercel

> Note: The Railway backend also handles Stripe webhooks at `/webhook/stripe` for API key tier upgrades. The Vercel webhook is a supplementary stub for frontend-side subscription tracking.

## 6. PWA

The app includes:
- `public/manifest.json` — installable web app
- `public/sw.js` — offline cache for shell assets
- Service worker auto-registers in production

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Scanner shows 404 | Generate a signal report on Railway backend |
| CORS errors | Add your Vercel domain to `TRADING_API_ALLOWED_ORIGINS` on Railway |
| Auth redirect loop | Verify Supabase URL/key match and Site URL in Supabase settings |
| Checkout fails | Ensure API key is saved in Profile and Stripe is configured on Railway |
| Positions empty | Set `NEXT_PUBLIC_DASHBOARD_URL` to your trading bot dashboard service |