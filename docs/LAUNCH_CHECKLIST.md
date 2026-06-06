# MomentumForge Enhanced — Launch Checklist

## Pre-Launch

### Backend (Railway)
- [ ] Railway backend is live and `/health` returns 200
- [ ] `TRADING_API_KEY` is set on Railway
- [ ] Stripe env vars configured (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PREMIUM_PRICE_ID`)
- [ ] At least one signal report generated (`python -m trading_bot.saas generate`)
- [ ] CORS allows your Vercel domain (if applicable)

### Frontend (Vercel)
- [ ] `NEXT_PUBLIC_BACKEND_URL` points to Railway app URL
- [ ] `NEXT_PUBLIC_DASHBOARD_URL` set if trading dashboard is on a separate service
- [ ] Supabase project created and migration applied (`supabase/migrations/001_trades.sql`)
- [ ] `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY` configured
- [ ] Stripe webhook endpoint configured: `https://your-app.vercel.app/api/stripe/webhook`
- [ ] `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` set in Vercel env vars

### Local Verification
- [ ] `cd frontend && npm install && npm run dev` — dashboard loads at localhost:3000
- [ ] Scanner shows signals from Railway (or graceful error if no reports)
- [ ] Login/signup works via Supabase
- [ ] PDF export downloads a report
- [ ] Billing page loads pricing tiers
- [ ] `npm run build` passes without errors

## Launch Day

1. Deploy frontend to Vercel (`vercel --prod`)
2. Verify production URL loads dashboard
3. Test auth flow end-to-end
4. Test Stripe checkout with a test card
5. Confirm webhook receives `checkout.session.completed`
6. Share demo link for marketing

## Post-Launch

- [ ] Monitor Railway logs for API errors
- [ ] Monitor Vercel analytics
- [ ] Set up Supabase RLS audit
- [ ] Configure custom domain
- [ ] Add PWA icons (replace placeholder SVG)