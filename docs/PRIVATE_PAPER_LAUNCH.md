# Private Paper Launch Contract

Status: implementation baseline for the private operator release.

## Purpose

The first launch is a single-owner, paper-trading application for measuring
whether the rules-based momentum strategy has a repeatable edge. It is not a
promise of income, an investment-advisory service, or a public trading product.

The operator's desired $5,000-$10,000 daily outcome is treated as an aspiration,
not an engineering acceptance criterion. The acceptance criterion is a positive,
risk-adjusted paper-trading record with bounded losses and complete operational
evidence.

## Non-negotiable operating mode

- One allow-listed Supabase owner account may access the web control room.
- The browser never receives Railway API keys or dashboard bearer tokens.
- The bot runs in `paper` mode, long-only, during regular US equity sessions.
- Public signup, billing, share links, growth projections, demo signals, and
  mobile order-routing surfaces are disabled for the private release.
- Dashboard scanner rows come from the running bot's real market-data scanner.
  A scanner rank is never presented as a win probability.
- The analytics service generates current reports only from an explicitly
  configured real provider. Demo, stale, and total-provider-failure reports are
  rejected instead of being served.
- Health probes may remain public for hosting infrastructure. Trading state,
  reports, positions, trades, and operational dashboards require authentication.

## Live-money evidence gate

Live mode remains disabled unless every condition below passes:

1. The operator explicitly enables live activation in configuration.
2. At least 100 closed paper trades exist in the journal.
3. Those trades span at least 20 distinct trading days.
4. Realized expectancy per trade is greater than zero.
5. Realized profit factor is at least 1.25.
6. Peak-to-trough drawdown is no greater than 5%.
7. Alpaca is explicitly configured for non-paper operation with valid keys.
8. The operator completes the existing interactive live-risk acknowledgement.

Passing the gate means only that the software has met its configured evidence
threshold. It does not imply future profitability or eliminate trading risk.

## Private launch acceptance gates

- All backend tests pass.
- Risk and live-readiness tests pass independently.
- Frontend lint and production build pass.
- Native mobile is either fully real-data and validated or explicitly excluded
  from distribution. For this release it is excluded.
- Dependency audits contain no known high or critical production vulnerability,
  or a documented upstream exception with compensating controls.
- Containers run the service as a non-root user after preparing the mounted data
  directory with least-privilege permissions.
- Production smoke tests prove: unauthenticated private routes are rejected,
  owner routes work, the core bot reports `paper`, scanner data is current, and
  no report is labeled `demo`.
- A rollback target is recorded before any production deployment.

## Public-product gate (deferred)

No public release or monetization begins until legal counsel has reviewed the
product classification, claims, disclosures, market-data redistribution rights,
privacy terms, subscription flows, and applicable financial-services rules.
Public performance claims must be generated from reproducible evidence and must
not imply guaranteed outcomes.

## Dependency audit status

As of 2026-07-25, `npm audit --omit=dev --audit-level=high` reports **zero**
vulnerabilities, and `pip-audit` reports zero for the Python service. The
production gate is met with no exception outstanding.

The previously documented PostCSS exception is resolved and has been removed. It
had also become inaccurate: the advisories were reclassified from moderate to
high, which made it a release blocker under the rule below rather than an
acceptable exception.

Next.js 16.2.11 still pins `postcss@8.4.31` and resolves `sharp@^0.34.5`, both
below their patched floors, so `frontend/package.json` carries an `overrides`
block that raises them and three other transitive packages to patched versions:

| Package | Was | Forced to | Advisory |
|---|---|---|---|
| `postcss` | 8.4.31 | ^8.5.23 | GHSA-qx2v-qp2m-jg93, GHSA-6g55-p6wh-862q, GHSA-r28c-9q8g-f849 |
| `sharp` | 0.34.5 | ^0.35.3 | GHSA-f88m-g3jw-g9cj (libvips CVE-2026-33327/33328/35590/35591) |
| `fast-uri` | 3.1.3 | ^3.1.4 | GHSA-v2hh-gcrm-f6hx |
| `dompurify` | 3.4.11 | ^3.4.12 | GHSA-c2j3-45gr-mqc4 |
| `js-yaml` | 4.2.0 | ^4.3.0 | GHSA-52cp-r559-cp3m |

Every forced version is semver-compatible with the range its parent declares, so
no dependency is being pushed across a major boundary. Remove each override once
the upstream parent ships a release that resolves the patched version on its own.

### Known development-only finding (not a release blocker)

`npm audit` without `--omit=dev` reports `brace-expansion` (GHSA-mh99-v99m-4gvg,
unbounded-expansion DoS) reachable only through ESLint's own glob matching.
The advisory range is `<=5.0.7` and spans major versions, so no 1.x release can
satisfy it; the fix requires ESLint 10, which currently crashes the
`eslint-plugin-react` bundled inside `eslint-config-next`
(`contextOrFilename.getFilename is not a function`). This code is lint tooling.
It is not imported by the application, not present in the production bundle, and
not reachable by any request. A `brace-expansion@^1.0.0` override is in place and
already clears the second advisory (GHSA-3jxr-9vmj-r5cp) on that package.

High and critical **production** audit findings remain a release blocker.
Recheck this section on every dependency update, and drop the ESLint note as
soon as `eslint-config-next` ships plugins compatible with ESLint 10.
