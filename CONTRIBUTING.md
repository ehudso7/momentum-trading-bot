# Contributing to Momentum Trading Bot

Thank you for your interest in contributing. This document provides guidelines for contributing to this project.

## Reporting Issues

- Use [GitHub Issues](https://github.com/ehudso7/momentum-trading-bot/issues) to report bugs or request features.
- Include steps to reproduce, expected behavior, and actual behavior.
- For bugs, include your Python version and OS.

## Development Setup

```bash
git clone https://github.com/ehudso7/momentum-trading-bot.git
cd momentum-trading-bot
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running Tests

```bash
pytest tests/ -v                          # All tests
pytest tests/test_risk.py -v              # Risk management (critical)
pytest tests/ -v --cov=trading_bot        # With coverage
```

**All PRs must pass `tests/test_risk.py` -- this is non-negotiable.**

## Code Style

- **Type hints** on all public method signatures.
- **Docstrings** on all public classes and methods.
- **structlog** for all logging (not `print()` or `logging` directly).
- **Pydantic** for all configuration and validation.
- **pytest** for all tests; use `pytest-mock` for mocking.

## Safety Rules (Non-Negotiable)

Before submitting a PR, verify you have **NOT**:

- Removed or weakened any risk limit (position sizing, stop losses, circuit breakers)
- Changed the default run mode from `paper`
- Increased `risk_per_trade_pct` upper bound beyond 3%
- Removed the live mode confirmation prompt
- Committed API keys or secrets

## Pull Request Process

1. Fork the repository and create a feature branch.
2. Make your changes with clear, focused commits.
3. Ensure all tests pass: `pytest tests/ -v`
4. Ensure risk tests pass: `pytest tests/test_risk.py -v`
5. Open a PR with a description of what changed and why.

## Architecture Notes

- **Sync polling loop** (not async) -- keep it simple.
- **Constructor dependency injection** -- every component must be independently testable.
- **Config validation** via Pydantic with hard min/max bounds on all risk parameters.
- See `CLAUDE.md` for full architecture details.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
