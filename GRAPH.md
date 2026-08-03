<!-- EH-GOV:BEGIN GENERATED source=ehudso7/project-governance version=2.3.0 — do not edit inside this block; edits will be overwritten by `malik sync`. Project-owned content belongs in the PROJECT block. -->
# GRAPH.md — Workflow graphs for Momentum Trading Bot

Non-trivial work in this repository follows graph-based workflows defined by the governance source (`ehudso7/project-governance` v2.3.0).

## Task classification

| Class | Meaning | Graph required? |
|---|---|---|
| `trivial` | Single obvious change, no design decisions (typo, comment, constant) | No |
| `linear` | Short known sequence, no branching risk | No |
| `multi-step` | Several dependent steps, at least one verification gate | Recommended |
| `graph-required` | Real feature/bug/architecture work with quality gates and evidence | Yes |
| `high-risk-controlled` | Production releases, incidents, migrations, security-sensitive changes | Yes, with approvals |

Do not force trivial tasks into graph ceremony; do not smuggle graph-required work through as "trivial".

## Default graphs for this project

| Task kind | Graph |
|---|---|
| feature | `feature-development` |
| bug | `bug-resolution` |
| architecture | `architecture-review` |
| release | `production-release` |
| incident | `incident-response` |
| audit | `repository-audit` |
| security | `security-review` |
| diligence | `acquisition-diligence` |
| dependency | `dependency-upgrade` |

## Using a graph

1. Classify the task; pick the graph from the table (or justify a different one).
2. Start an execution record in `.governance/evidence/` (schema: `execution-record.schema.json`).
3. Execute node by node; record node completion, retries, and failures.
4. Stop at approval nodes until a human approves.
5. Finish only at a terminal node; produce the evidence bundle.

Render any graph as a diagram: `malik graph render <graph-id>`.
<!-- EH-GOV:END GENERATED -->

<!-- EH-GOV:BEGIN PROJECT -->
<!-- Project-specific additions go here. `ehgov sync` never modifies this block. -->
<!-- EH-GOV:END PROJECT -->
