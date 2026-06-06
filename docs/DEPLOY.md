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

### API Key Setup

1. Set `TRADING_API_KEY` on Railway
2. In the frontend, go to Profile → API Key and paste your key
3. Premium signals will unlock with a premium-tier key

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