# momentum-trading-bot — Architecture

<!-- governance: v1.0.0 | generated 2026-07-12 -->

## System overview (verified)

Automated momentum day-trading bot for US equities executing via Alpaca (paper default, live mode behind an explicit confirmation), plus a Stripe-billed trading-signal SaaS layer behind API keys, a Next.js frontend (Vercel) and an Expo mobile app. FastAPI dashboard; deployed on Railway.

Stack: Python 3.10+ (trading_bot), FastAPI + uvicorn, pandas/numpy, alpaca-py, polygon-api-client, yfinance, Pydantic 2, Stripe (SaaS billing), structlog + Sentry, Next.js (frontend/), Expo React Native (mobile/momentum-trading), Supabase (supabase/migrations), Docker / Railway

## Components

Enumerate real top-level apps/packages/modules and what each does, with paths.

_TBD (maintainer/agent: fill from the actual tree; every entry must cite a path)._

## Data flow

_TBD (maintainer)._

## Persistence & migrations

This repository has database migrations. Migration changes require the
db-migration-review skill/checklist and must never run automatically in governance PRs.
_TBD (maintainer): datastore(s), schema ownership, migration procedure._

## External dependencies & integrations

_TBD (maintainer): third-party APIs, webhooks, queues — cite config/code paths._

## Architecture Decision Records

Significant decisions live in `docs/adr/`. Start from `docs/adr/0000-template.md`.
