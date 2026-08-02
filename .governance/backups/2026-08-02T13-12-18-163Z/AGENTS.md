<!-- EH-GOV:BEGIN GENERATED source=ehudso7/projects-governance version=1.1.0 — do not edit inside this block; edits will be overwritten by `ehgov sync`. Project-owned content belongs in the PROJECT block. -->
# AGENTS.md — Momentum Trading Bot

Cross-agent constitution for `momentum-trading-bot`. Applies to every AI coding agent operating in this repository (Claude Code, Codex, and future runtimes). Runtime-specific files (`CLAUDE.md`, etc.) defer to this one.

Governance: `ehudso7/projects-governance` v1.1.0 · profile `regulated-system` · criticality `critical`

## Constitution

1. **Inspect before changing.** Understand the code you are about to modify. Do not guess project commands, paths, or conventions — verify them.
2. **Classify every task** as `trivial`, `linear`, `multi-step`, `graph-required`, or `high-risk-controlled`, and use the matching workflow graph for non-trivial work (see `GRAPH.md`).
3. **No monolithic execution** of graph-required work: follow the graph's nodes, gates, and failure paths.
4. **Honest state reporting.** `planned`, `implemented`, `tested`, and `verified` are distinct states. Claiming `verified` requires recorded command output with passing exit codes.
5. **Evidence.** Non-trivial work produces an execution record and evidence bundle under `.governance/evidence/` (contracts in the governance source `schemas/`).
6. **Human approval is mandatory for:** `production-deployment`, `destructive-migration`, `secrets`, `billing`, `authentication`, `authorization`.
7. **Sensitive paths** (below) carry extra policy weight; treat changes there as high-risk.
8. **Never** commit secrets, fabricate test results, discard unrelated work, or bypass a failing gate by weakening it.

## Sensitive paths

- `.github/workflows/**`
- `**/migrations/**`
- `infra/**`
- `**/*auth*`
- `**/*billing*`
- `**/*permission*`
- `**/*secret*`

## Workflow graph defaults

| Task kind | Graph |
|---|---|
| feature | `feature-development` |
| bug | `bug-resolution` |
| architecture | `architecture-review` |
| release | `production-release` |
| incident | `incident-response` |
| audit | `repository-audit` |
| security | `security-review` |
| dependency | `dependency-upgrade` |

## Governance tooling

- `ehgov inspect` — project governance status
- `ehgov validate` — validate this repository against profile `regulated-system`
- `ehgov sync --check` — check generated sections for drift against the pinned governance version
<!-- EH-GOV:END GENERATED -->

<!-- EH-GOV:BEGIN PROJECT -->
# AGENTS.md

## Cursor Cloud specific instructions

### Overview

This is a Python-based automated momentum day-trading bot. It has a single service: the trading bot process which includes a FastAPI dashboard (port 8080). No database or external containers are required. See `CLAUDE.md` for full architecture and `README.md` for setup/run/test commands.

### Running tests

```bash
pytest tests/ -v
```

All 496 tests should pass. No linter is configured for this project.

### Running the application

```bash
trading-bot --mode paper --dashboard-port 8080
```

The dashboard is at `http://localhost:8080`. API docs at `http://localhost:8080/api/docs`. The bot connects to Alpaca paper trading (via `ALPACA_API_KEY`/`ALPACA_API_SECRET` env vars). Without valid API keys, it falls back to the local `PaperBroker`.

### Gotchas

- The `.env` file must exist at the project root (copy from `trading_bot/config/.env.example`). The update script creates it if missing.
- The `trading-bot` CLI is installed to `~/.local/bin`; ensure `PATH` includes it.
- Market is closed on weekends; the bot will report "Weekend" status but still runs the polling loop and dashboard.
- `AppConfig` includes a `strip_env_whitespace` model validator that strips trailing whitespace/tabs from environment variable values before Pydantic validation. This handles environments where secrets may be injected with extra whitespace.
<!-- EH-GOV:END PROJECT -->
