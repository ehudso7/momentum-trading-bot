# Future Scope - Deferred Improvements

Items below are valuable enhancements deferred because they require new API keys,
significant architectural changes, or external service integrations. Revisit when ready
to expand the system's capabilities.

---

## Requires New API Keys / Paid Services

### 1. Real-Time WebSocket Streaming (Polygon/Alpaca)
- **What:** Replace 60s polling with sub-second WebSocket price feeds
- **Why:** Enables tighter trailing stops, faster entry fills, and true Level 2 data
- **Requires:** Polygon WebSocket plan or Alpaca data subscription
- **Complexity:** High - requires async event loop, message queue, backpressure handling

### 2. SMS/Push Alerting (Twilio / Pushover / Telegram)
- **What:** Send alerts on trades, circuit breaker trips, and daily summary
- **Why:** Immediate awareness when away from dashboard
- **Requires:** Twilio API key, Pushover token, or Telegram bot token
- **Complexity:** Low - simple HTTP POST on key events

### 3. Advanced News Sentiment (OpenAI / Anthropic API)
- **What:** LLM-powered catalyst classification beyond keyword matching
- **Why:** Better distinguish earnings, FDA approvals, pump-and-dump, dilution from catalysts
- **Requires:** LLM API key (OpenAI, Anthropic, etc.)
- **Complexity:** Medium - prompt engineering, latency budget, cost management

### 4. Options Flow Data (Unusual Whales / Tradier)
- **What:** Monitor unusual options activity as a momentum confirmation signal
- **Why:** Large call sweeps often precede momentum moves on low-floats
- **Requires:** Unusual Whales API or Tradier options data subscription
- **Complexity:** Medium - new data source integration, signal correlation

### 5. Short Interest Data (Ortex / FINRA)
- **What:** Include short interest and cost-to-borrow in scanner scoring
- **Why:** High short interest + momentum = potential squeeze (additional edge)
- **Requires:** Ortex API subscription or FINRA data feed
- **Complexity:** Medium - data normalization, caching, scoring integration

### 6. Multi-Broker Support (Interactive Brokers / Tradier)
- **What:** Add IBKR TWS/Gateway and Tradier as execution providers
- **Why:** Better margin rates, options execution, international markets
- **Requires:** IBKR API gateway setup, Tradier API key
- **Complexity:** High - different order models, TWS connection management

---

## No API Keys Required (Architectural Improvements)

### 7. Async Architecture Migration
- **What:** Migrate from sync polling to async (asyncio + aiohttp)
- **Why:** Prerequisite for WebSocket streaming; better resource utilization
- **Impact:** Foundational change affecting all modules
- **Complexity:** High - full rewrite of main loop, broker clients, data layer

### 8. Database Persistence (SQLite/PostgreSQL)
- **What:** Replace CSV journal + in-memory state with proper database
- **Why:** Crash recovery, historical analysis, multi-day tracking, query capability
- **Options:** SQLite for single-instance, PostgreSQL for multi-instance
- **Complexity:** Medium - schema design, migration from CSV, ORM (SQLAlchemy)

### 9. Multi-Strategy Framework
- **What:** Run multiple strategies simultaneously with independent risk budgets
- **Why:** Diversification, A/B testing strategies, regime-specific strategies
- **Design:** Strategy registry, per-strategy allocation, combined circuit breaker
- **Complexity:** High - portfolio allocation, strategy isolation, conflict resolution

### 10. Machine Learning Regime Detection
- **What:** Use scikit-learn/statsmodels for market regime classification
- **Why:** Adapt strategy parameters to market conditions (trending vs. choppy)
- **Options:** Hidden Markov Models, rolling volatility clustering, feature engineering
- **Complexity:** Medium - feature engineering, model training, online inference

### 11. Kubernetes Deployment
- **What:** Helm chart with HPA, PDB, ConfigMap/Secret management
- **Why:** Production-grade orchestration, auto-scaling, rolling updates
- **Prerequisites:** Health probes (done), graceful shutdown (done)
- **Complexity:** Medium - Helm templates, resource limits, monitoring stack

### 12. Prometheus + Grafana Observability
- **What:** Expose /metrics endpoint (prometheus_client), Grafana dashboards
- **Why:** Professional monitoring, alerting rules, historical metrics, SLO tracking
- **Metrics:** Tick latency, fill latency, P&L, drawdown, error rates, memory
- **Complexity:** Low-Medium - prometheus_client integration, Grafana JSON dashboards

### 13. Pre-Market Scanner Enhancement
- **What:** Run scanner at 7:00 AM ET to build watchlist before open
- **Why:** Better preparation, pre-computed float/catalyst data, ranked watchlist
- **Design:** Separate pre-market scan job, cached results for 9:30 AM use
- **Complexity:** Low - scheduler addition, cache layer

### 14. Backtester Enhancements
- **What:** Walk-forward optimization, Monte Carlo simulation, transaction cost modeling
- **Why:** More realistic performance estimates, parameter robustness testing
- **Design:** Sliding train/test windows, randomized trade sampling, cost curves
- **Complexity:** Medium - statistical framework, parallel execution

### 15. Rate Limiter Middleware
- **What:** Token bucket rate limiter for all external API calls
- **Why:** Prevent hitting Polygon/Alpaca rate limits during high-activity periods
- **Design:** Decorator-based, per-endpoint limits, backpressure signaling
- **Complexity:** Low - token bucket implementation, decorator pattern

### 16. Configuration Hot-Reload
- **What:** Watch config.yaml for changes and apply without restart
- **Why:** Adjust risk params, scanner filters, and strategy params mid-session
- **Design:** File watcher (watchdog), diff-based reload, validation before apply
- **Complexity:** Medium - thread-safe config swap, validation, rollback on error

### 17. Trade Replay / Simulation Mode
- **What:** Replay historical market data tick-by-tick through the full pipeline
- **Why:** Debug strategy behavior on specific days, verify fixes against past failures
- **Design:** Recorded market data files, time-controlled playback, deterministic fills
- **Complexity:** Medium - data recording, playback engine, time simulation

### 18. REST API for External Control
- **What:** Add POST endpoints for manual overrides (force close, pause, resume)
- **Why:** Operational control without restarting the bot
- **Endpoints:** POST /api/pause, POST /api/resume, POST /api/close-all, POST /api/config
- **Complexity:** Low - FastAPI routes, auth middleware, state mutations

---

## Priority Ranking

| Priority | Item | Impact | Effort |
|----------|------|--------|--------|
| P0 | #12 Prometheus/Grafana | High | Low |
| P0 | #15 Rate Limiter | High | Low |
| P0 | #2 Alerting (Twilio/Telegram) | High | Low |
| P1 | #8 Database Persistence | High | Medium |
| P1 | #13 Pre-Market Scanner | Medium | Low |
| P1 | #18 REST API Control | Medium | Low |
| P1 | #16 Config Hot-Reload | Medium | Medium |
| P2 | #1 WebSocket Streaming | High | High |
| P2 | #7 Async Migration | High | High |
| P2 | #10 ML Regime Detection | Medium | Medium |
| P2 | #14 Backtester Enhancements | Medium | Medium |
| P3 | #3 LLM News Sentiment | Medium | Medium |
| P3 | #9 Multi-Strategy | High | High |
| P3 | #4 Options Flow | Low | Medium |
| P3 | #5 Short Interest | Low | Medium |
| P3 | #6 Multi-Broker | Medium | High |
| P3 | #11 Kubernetes | Medium | Medium |
| P3 | #17 Trade Replay | Low | Medium |
