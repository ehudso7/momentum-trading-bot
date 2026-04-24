# Core Conversion Control

This repository is being converted from a momentum day-trading bot into a private Core engine.

## Non-Negotiable Direction

The product sequence is:

```text
Core = weapon
SaaS = product
Crypto = leverage
```

This repository is responsible for the **Core** only. It must not become a public SaaS product, a user-facing auto-trading platform, or a crypto/token product.

## Core Definition

The Core is a private research, validation, risk, signal, and execution system that can discover, test, score, and safely execute market opportunities.

The Core must be:

1. **Edge-first** — data and evidence drive strategies, not vibes or hard-coded setups alone.
2. **Risk-governed** — every possible trade must pass through sizing, exposure, drawdown, loss-streak, and circuit-breaker controls.
3. **Broker-abstracted** — Alpaca remains the first adapter, not the permanent ceiling.
4. **Research-compatible** — every live/paper decision must be reproducible in backtests and replay.
5. **Observable** — decisions, rejections, fills, exits, errors, and performance must be logged with enough detail to diagnose edge decay.
6. **Private by design** — no SaaS customer should ever receive direct access to the true Core execution logic.

## What This Repo Is Today

Current state:

- Multi-source momentum scanner
- VWAP/EMA/ORB/red-to-green/breakout strategy engine
- Alpaca execution adapter
- Paper broker
- Backtest mode
- Risk controls
- Circuit breaker
- CSV trade journal
- FastAPI dashboard
- Docker/Railway deploy support

That makes it a strong **Core v0 lab**, not yet a weapon-grade Core.

## Conversion Phases

### Phase 1 — Core Skeleton + Decision Boundary

Add a clean Core layer without changing existing trading behavior.

Deliverables:

- `trading_bot/core/` package
- Signal decision data contracts
- Feature snapshot data contracts
- Core engine orchestrator shell
- Clear adapter boundary between existing scanner/strategy/risk/execution modules and future Core logic
- Tests proving imports, contracts, and no behavior breakage

Exit gate:

- Existing tests still pass
- New Core tests pass
- Existing CLI still works
- No live-trading behavior changes

### Phase 2 — Feature Store + Decision Ledger

Convert the bot into a data machine.

Deliverables:

- Structured feature snapshots for every candidate
- Structured signal decision records for every accepted/rejected setup
- Persistent decision ledger
- CSV compatibility preserved initially
- Optional Postgres/TimescaleDB design documented but not required for first cut

Exit gate:

- Every candidate has a reproducible decision trail
- Rejected signals are first-class records, not afterthoughts
- Backtest, paper, and live all emit comparable records

### Phase 3 — Alpha Scoring Layer

Add scoring above the current strategy layer.

Deliverables:

- `AlphaScorer` interface
- Rule-based baseline scorer
- Model-ready feature vector output
- Shadow-mode scoring first; no execution effect until validated

Exit gate:

- The scorer can rank candidate quality without changing trades
- Shadow scores can be compared against actual outcomes

### Phase 4 — Execution Hardening

Improve the Core from research-grade to money-grade.

Deliverables:

- Event-driven internal loop where practical
- Broker abstraction hardening
- Idempotent order intents
- Fill reconciliation
- Latency and slippage measurement
- Kill switch and emergency flatten path

Exit gate:

- Paper/live behavior is resilient under API errors, stale data, partial fills, and reconnects

### Phase 5 — Private Core / Public Product Boundary

Prepare SaaS extraction without exposing the weapon.

Deliverables:

- Public-safe analytics API spec
- Internal-only Core API spec
- Data redaction rules
- SaaS feature boundary document

Exit gate:

- SaaS can expose analytics, journaling, backtesting, alerts, and risk tools
- SaaS cannot expose private execution alpha or proprietary scoring weights

## Things We Will Not Do Yet

- Do not optimize for HFT before edge is proven.
- Do not expose auto-trading to customers.
- Do not add crypto/token logic.
- Do not replace the existing bot in one giant rewrite.
- Do not remove risk controls to increase trade frequency.

## Definition of Done for Phase 1

Phase 1 is complete when this repo has a safe Core skeleton that can be built on without breaking the current working bot.

Required checks:

```bash
python -m pytest tests/ -v
python -m pytest tests/test_core_contracts.py -v
python -m trading_bot.main --help
```
