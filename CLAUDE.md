# Japan 225 Bot — Claude Code Instructions

## START HERE (every session, every dashboard query)

1. Read `MEMORY.md` first — full architecture, file map, key constants, known bugs.
2. For the specific module you need: read `.claude/digests/<module>.digest.md` only.
3. **Never read raw .py source files** unless the digest is missing or you are making a code change.
4. For live bot status: read `storage/data/bot_state.json` — do not call APIs or read monitor.py.
5. For trade history: `sqlite3 storage/data/trading.db "SELECT ..."` — do not read database.py.

## Digest Index (read the digest, not the source)
| Digest | Covers |
|--------|--------|
| settings | All constants, model names, thresholds |
| monitor | Main loop, scanning/monitoring cycles, startup sync |
| database | SQLite schema, all Storage methods |
| indicators | analyze_timeframe(), detect_setup(), all setup types |
| session | get_current_session(), blackout rules |
| momentum | MomentumTracker, adverse tier logic |
| confidence | compute_confidence(), 11-criteria scoring |
| ig_client | IG REST API, connect, prices, open/modify/close |
| risk_manager | validate_trade(), 11 checks, get_safe_lot_size() |
| exit_manager | evaluate_position(), ExitPhase, trailing stop |
| analyzer | AIAnalyzer, Sonnet/Opus 2-tier pipeline, JSON schema |
| context_writer | write_context(), market_snapshot/recent_activity/macro/live_edge |
| telegram_bot | Commands, buttons, alert flow |
| dashboard | FastAPI routes, systemd units, ngrok |
| claude_client | Dashboard chat, history summarizer, usage tracker |
| scan_analyzer | Cron-based missed-move tracker, rejection analysis |

## Response Style (dashboard chat)
- Status queries: 2–4 sentences max. No headers.
- Code questions: answer directly, reference file:line if relevant.
- Only expand with detail if user explicitly asks.
- No markdown headers in chat responses — renders as raw # symbols in dashboard.
- Never reproduce large file contents unless explicitly asked.

## Available Skills
Invoke with `/skill-name`. Full workflows live in `~/.claude/skills/`.

| Skill | Trigger |
|-------|---------|
| `/trade-review` | review recent trades, worst trade AI analysis |
| `/strategy-health` | WR vs backtest baseline, confidence threshold check |
| `/cost-report` | API spend, per-trade cost, Sonnet vs Opus split |
| `/deploy-check` | service health, log errors, git status |
| `/prompt-audit` | prompt_learnings.json, losing trade AI analysis |
| `/session-brief` | market briefing, recent scans, macro events |
| `/brier-check` | AI calibration, Brier score breakdown |
| `/backtest-import` | import CSV backtest results to MEMORY.md |
| `/gha` | analyze failing GitHub Actions runs |
| `/recall` | search conversation history |

## Handling Operational Questions (dashboard chat)
User is a trader, not a developer. When they ask operational questions:
- "What's wrong?" / "Is the bot working?" → check `systemctl is-active japan225-bot japan225-dashboard japan225-ngrok` then read last 20 lines of `storage/data/bot_state.json`
- "Why no trades?" → check bot_state.json session, is_no_trade_day(), then recent scans in DB
- "What happened to cost?" / "API usage?" → read `storage/data/chat_costs.json` for chat costs + `sqlite3 storage/data/trading.db "SELECT SUM(api_cost) FROM scans WHERE timestamp LIKE '$(date +%Y-%m-%d)%'"` for scan costs
- "Is IG down?" / "API issues?" → check journalctl for IG errors, use WebSearch for "IG Index API status" if needed
- "Why off-hours?" → explain that off_hours = no active trading session (Tokyo 00-06, London 08-16, NY 16-21 UTC). Normal for weekends.
- "What do the logs mean?" → read `/api/logs?type=scan` result from recent journalctl or explain the outcome codes in recent_scans
- Always give a DIRECT answer. Never say "probably" or "might". Check the actual data first.
- If you need to run a command and it might affect the bot, warn the user first.

## Development Discipline (hard gates — never skip)
1. **Plan before code** — Before writing any code, describe the approach and wait for user approval.
2. **Clarify ambiguity** — If requirements are ambiguous, ask clarifying questions before writing any code.
3. **Edge cases after code** — After finishing any code, list the edge cases and suggest test cases to cover them.
4. **Small changesets** — If a task requires changes to more than 3 files, stop and break it into smaller tasks first.
5. **Bug = test first** — When there's a bug, start by writing a test that reproduces it, then fix it until the test passes.
6. **Learn from corrections** — Every time the user corrects me, reflect on what went wrong and write a plan (in MEMORY.md session notes) to never repeat that mistake.

## Standing Rules
- Minimal diffs. Never delete+rewrite unchanged lines.
- After any code change: update MEMORY.md first, then the relevant digest.
- Never commit .env or *.db files.
- If no digest exists for a file you changed, create one.
- `POSITIONS_API_ERROR` is a sentinel — check with `is`, not `not`.
- `open_trade_atomic()` is the only safe way to log a trade open.

## Key File Locations
```
monitor.py              Main process (systemd: japan225-bot)
config/settings.py      ALL constants — never scatter config
ai/analyzer.py          Sonnet→Opus 2-tier pipeline, JSON schema
storage/data/           trading.db | bot_state.json | prompt_learnings.json
storage/data/           chat_usage.json | dashboard_overrides.json
.claude/digests/        14 digest files — always prefer over raw source
```
