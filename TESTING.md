# momentum-trading-bot — Testing

<!-- governance: v1.0.0 | generated 2026-07-12 -->

## Verified test commands

- `pytest tests/ -v`
- `pytest tests/ --cov=trading_bot`

(Verified from: pyproject.toml ([project.optional-dependencies].dev, [tool.pytest.ini_options]) + README.md + CLAUDE.md/AGENTS.md, read from local shallow clone of main @ 77c8492 on 2026-07-12. build/lint/typecheck verified absent: pure-Python package, no build step; AGENTS.md states 'No linter is configured' and no ruff/flake8/black/mypy config exists. No .github/workflows at all..)

## Requirements

- New features ship with tests; bug fixes ship with a regression test where feasible.
- Tests must not be deleted, skipped, or weakened to make a change pass.
- CI must run the commands above on every PR.

## Test layout

_TBD (maintainer/agent: describe where tests live and how to run a single test)._

## Coverage expectations

_TBD (maintainer)._
