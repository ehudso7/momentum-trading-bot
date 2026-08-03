<!-- EH-GOV:BEGIN GENERATED source=ehudso7/project-governance version=2.3.0 — do not edit inside this block; edits will be overwritten by `malik sync`. Project-owned content belongs in the PROJECT block. -->
# EVIDENCE.md — Momentum Trading Bot

Evidence contracts for this project. Governance: `ehudso7/project-governance` v2.3.0.

## What evidence is (and is not)

Evidence bundles make work auditable: what was intended, what ran, what changed, what was verified. They support assurance and review. **They do not by themselves create legal indemnity or transfer liability.**

## Where evidence lives

- Directory: `.governance/evidence/`
- Retention: 365 days minimum
- Execution record required: true

## Contracts (JSON Schemas in the governance source `schemas/`)

| Artifact | Schema | Produced by |
|---|---|---|
| Execution record | `execution-record.schema.json` | The executing agent/runtime, per graph run |
| Evidence bundle | `evidence.schema.json` | The executing agent, at run completion |
| Expected outcome | `expected-outcome.schema.json` | Author of the change, before/during the run |
| Observed outcome | `observed-outcome.schema.json` | Whoever measures reality afterward |
| Verification result | `verification-result.schema.json` | UIE (never the executing agent itself) |

Validate any artifact: `malik evidence validate <file>`.

## Honesty rules

- `completion_status: verified` requires recorded passing commands or test results; the validator rejects bundles that claim verification without them.
- Failures, skipped nodes, and unresolved risks are recorded, not omitted.
- `content_hash` provides tamper-evidence for finished bundles.
<!-- EH-GOV:END GENERATED -->

<!-- EH-GOV:BEGIN PROJECT -->
<!-- Project-specific additions go here. `ehgov sync` never modifies this block. -->
<!-- EH-GOV:END PROJECT -->
