# momentum-trading-bot — Security Policy

<!-- governance: v1.0.0 | generated 2026-07-12 -->

## Reporting a vulnerability

Report privately to the repository owner (do not open a public issue). Include
reproduction steps. You should receive an acknowledgement within 72 hours.

## Handling rules (enforced by policy and CI)

- No secrets in the repository: no API keys, tokens, passwords, or `.env*` files.
  CI runs a secret scan; findings block merge.
- All changes reach `main` via PR with required checks green.
- Dependencies are not upgraded casually; upgrades follow the dependency-upgrade
  review checklist (see governance skills).
- Authentication code paths require human review on every change; never disable
  auth checks in tests by editing product code.
- Payment code: never commit or log keys/payloads; live keys never appear in CI.

## Personal / regulated data

This product processes personal or regulated data. Rules:

- No real user data in code, fixtures, tests, logs, or issue text.
- Data collection/storage/sharing changes require a privacy review before merge.
- Data inventory and retention: _TBD (maintainer)._


## Known gaps

Record honestly; do not delete entries without fixing them.

- _TBD (maintainer): e.g. branch protection status, dependency audit cadence._
