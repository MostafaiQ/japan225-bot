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
| analyzer | AIAnalyzer, Haiku/Sonnet/Opus pipeline, tool use schema |
| context_writer | write_context(), market_snapshot/recent_activity/macro/live_edge |
| telegram_bot | Commands, buttons, alert flow |
| dashboard | FastAPI routes, systemd units, ngrok |
| claude_client | Dashboard chat, history summarizer, usage tracker |

## Response Style (dashboard chat)
- Status queries: 2–4 sentences max. No headers.
- Code questions: answer directly, reference file:line if relevant.
- Only expand with detail if user explicitly asks.
- No markdown headers in chat responses — renders as raw # symbols in dashboard.
- Never reproduce large file contents unless explicitly asked.

## Available Skills (invoke for common tasks)
When user asks to review recent trades → use `/trade-review` workflow:
  1. `sqlite3 storage/data/trading.db "SELECT trade_number,direction,setup_type,session,confidence,pnl,result FROM trades ORDER BY id DESC LIMIT 10"`
  2. Format as compact table. Identify worst trade. Read its ai_analysis field. Explain what AI missed.

When user asks about strategy performance → use `/strategy-health` workflow:
  1. Query trades grouped by setup_type and session (last 20 closed trades).
  2. Compare WR to backtest baseline: bb_mid_bounce=47%, bb_lower_bounce=45%, Tokyo=49%, London=44%, NY=48%.
  3. Flag anything >10% below baseline. Suggest if confidence threshold needs adjustment.

When user asks about API costs → use `/cost-report` workflow:
  1. `sqlite3 storage/data/trading.db "SELECT SUM(api_cost) FROM scans"` and same for trades.
  2. Compute cost-per-evaluation and Sonnet vs Opus split from scan records.
  3. Report: total spent, per-trade cost, whether Opus is adding value (WR on Opus-confirmed vs Sonnet-only trades).

When user asks to check deployment health → use `/deploy-check` workflow:
  1. `systemctl is-active japan225-bot japan225-dashboard japan225-ngrok`
  2. `tail -20 logs/monitor.log` for recent errors.
  3. Check git status for uncommitted changes. Check if MEMORY.md was updated after last code change.

When user asks about prompt performance → use `/prompt-audit` workflow:
  1. Read `storage/data/prompt_learnings.json` if it exists.
  2. Query last 5 losing trades and their ai_analysis field.
  3. Find patterns: what did Sonnet/Opus say that was wrong? What context was missing?

When user asks for a market briefing → use `/session-brief` workflow:
  1. Read `storage/context/market_snapshot.md` and `storage/context/macro.md`.
  2. Query recent scans: `sqlite3 storage/data/trading.db "SELECT action_taken, confidence, direction FROM scans WHERE timestamp > datetime('now', '-8 hours') ORDER BY id DESC LIMIT 10"`
  3. Summarize: active session, key indicators, recent scan outcomes, macro events.

When user asks about AI calibration → use `/brier-check` workflow:
  1. Read `storage/data/brier_scores.json`.
  2. Report: mean Brier score, breakdown by setup type and session.
  3. Interpret: <0.15 = well calibrated, 0.15-0.25 = moderate, >0.25 = overconfident/underconfident.

When user asks to import backtest data → use `/backtest-import` workflow:
  1. Read the CSV file provided by the user.
  2. Parse columns: timestamp, direction, setup_type, entry, exit, pnl.
  3. Update MEMORY.md Backtest Status section with new results.

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
ai/analyzer.py          Haiku→Sonnet→Opus pipeline, tool use schema
storage/data/           trading.db | bot_state.json | prompt_learnings.json
storage/data/           chat_usage.json | dashboard_overrides.json
.claude/digests/        14 digest files — always prefer over raw source
```
