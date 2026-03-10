# Contributing

Thanks for your interest in contributing to the Japan 225 Trading Bot! This guide will help you get started.

## Getting Started

1. **Fork the repo** and clone your fork
2. **Set up the development environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Run the tests** (no API credentials needed):
   ```bash
   python3 -m pytest tests/ -v
   ```

## Development Workflow

1. Create a branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. Make your changes
3. Run the full test suite:
   ```bash
   python3 -m pytest tests/ -x -q
   ```
4. Commit with a clear message describing the change
5. Push and open a pull request

## What to Contribute

### Good first issues
- Improve test coverage for edge cases
- Fix typos or clarify documentation
- Add type hints to functions that lack them

### Features
- **New setup types** -- add detection logic in `core/indicators.py` (`detect_setup()`)
- **New indicators** -- add calculation in `core/indicators.py` (`analyze_timeframe()`)
- **New confidence criteria** -- add to `core/confidence.py` (`compute_confidence()`)
- **Dashboard improvements** -- frontend is `docs/index.html`, API routes in `dashboard/routers/`
- **Backtesting** -- improvements to `backtest.py`
- **New broker integrations** -- implement the `IGClient` interface for other brokers

### Bug fixes
- Check the issue tracker for reported bugs
- If you find a bug, open an issue first to discuss before submitting a fix

## Code Style

- **Single source of truth for config** -- all constants go in `config/settings.py`, never scattered
- **Minimal diffs** -- only change what you need to. Don't reformat unchanged lines
- **No over-engineering** -- solve the current problem, not hypothetical future ones
- **Tests are required** -- add tests for new features. All 424+ tests must pass
- **No secrets in code** -- credentials go in `.env`, never committed

## Project Structure Guide

| Area | Where to look |
|------|--------------|
| All constants and thresholds | `config/settings.py` |
| Setup detection logic | `core/indicators.py` → `detect_setup()` |
| Confidence scoring | `core/confidence.py` → `compute_confidence()` |
| AI prompts | `ai/analyzer.py` → `build_system_prompt()`, `build_scan_prompt()` |
| Risk checks | `trading/risk_manager.py` → `validate_trade()` |
| Exit strategy | `trading/exit_manager.py` → `evaluate_position()` |
| Main loop | `monitor.py` → `_scanning_cycle()`, `_monitoring_cycle()` |
| Telegram commands | `notifications/telegram_bot.py` |
| Dashboard API | `dashboard/routers/` |
| Dashboard frontend | `docs/index.html` |

## Testing

Tests run without any API credentials using mock objects:

```bash
# All tests
python3 -m pytest tests/ -v

# Specific module
python3 -m pytest tests/test_indicators.py -v
python3 -m pytest tests/test_confidence.py -v
python3 -m pytest tests/test_risk_manager.py -v

# Stop on first failure
python3 -m pytest tests/ -x
```

If you add a new feature, add corresponding tests. Look at existing test files for patterns.

## Pull Request Guidelines

- **One feature per PR** -- keeps reviews manageable
- **Describe what and why** -- not just what changed, but why it's better
- **Include test results** -- mention that all tests pass
- **Don't break existing behavior** -- backward compatibility matters
- **Update docs if needed** -- README.md for user-facing changes, DEPLOY.md for infrastructure

## Architecture Notes

- **Single process** -- `monitor.py` runs everything (scanning, monitoring, Telegram)
- **No external databases** -- SQLite on the VM, WAL mode for crash safety
- **AI via CLI subprocess** -- Claude Code CLI with OAuth, not direct API calls
- **Bidirectional** -- the bot evaluates both LONG and SHORT every scan cycle
- **9-criteria weighted scoring** -- filters noise before expensive AI calls
- **Opus position evaluator** -- evaluates open positions every 2min, auto-closes if CLOSE_NOW >= 70% conf

## Questions?

Open an issue on GitHub. For bugs, include:
- What you expected to happen
- What actually happened
- Relevant log output
- Your Python version and OS
