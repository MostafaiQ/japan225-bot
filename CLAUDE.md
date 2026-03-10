# Japan 225 Bot — Claude Code Instructions

## START HERE (every session, every dashboard query)

1. Read `MEMORY.md` first — full architecture, file map, key constants, known bugs.
2. For the specific module you need: read `.claude/digests/<module>.digest.md` only.
3. **Never read raw .py source files** unless the digest is missing or you are making a code change.
4. For live bot status: read `storage/data/bot_state.json` — do not call APIs or read monitor.py.
5. For trade history: `sqlite3 storage/data/trading.db "SELECT ..."` — do not read database.py.
6. **Auto-run session-resumption agent** at the start of every new session (produces context brief).
7. **Auto-run post-trade-analyst agent** whenever a trade close is detected in logs or DB.
8. **Auto-run cost-watchdog agent** in background when the user asks about costs or at session end.

## Digest Index (read the digest, not the source)
| Digest | Covers |
|--------|--------|
| settings | All constants, model names, thresholds |
| monitor | Main loop, scanning/monitoring cycles, startup sync |
| database | SQLite schema, all Storage methods |
| indicators | analyze_timeframe(), detect_setup(), all setup types |
| session | get_current_session(), blackout rules |
| momentum | MomentumTracker, adverse tier logic |
| confidence | compute_confidence(), 9-criteria weighted scoring |
| ig_client | IG REST API, connect, prices, open/modify/close |
| risk_manager | validate_trade(), 12 checks, get_safe_lot_size() |
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

## Available Skills (15)
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
| `/restart` | **full deploy cycle**: test → commit → push → restart service (auto-aborts on test failure) |
| `/log-triage` | parse pasted errors/tracebacks, find source, classify root cause, fix directly |
| `/indicator-check` | current technicals table: EMA9/50/200, RSI, BB, VWAP, session, setup |
| `/confidence-debug` | reconstruct C1-C12 scoring for any scan — shows drags and boosts |
| `/db` | quick SQLite queries: `/db trades`, `/db scans`, `/db positions`, `/db <sql>` |

## Available Agents (7)
Agents live in `~/.claude/agents/`. Invoke via Agent tool with the agent name.

| Agent | Model | Auto-trigger | Purpose |
|-------|-------|-------------|---------|
| `high-chancellor` | Sonnet | break-glass only | supreme AI overseer for critical issues |
| `market-analyst` | default | manual | market analysis and indicator explanation |
| `trade-debugger` | default | manual | trade postmortem and failure analysis |
| `post-trade-analyst` | Haiku | after trade close | updates Brier scores + prompt_learnings automatically |
| `deploy-guardian` | Haiku | before systemctl restart | blocks deploy if tests/settings/syntax fail |
| `session-resumption` | Haiku | session start | produces "here's where you are" context brief |
| `cost-watchdog` | Haiku | background/on-demand | monitors API costs, warns on anomalies |

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

## Autonomous Protocols (auto-enforced — no user prompting needed)

### Deploy Protocol
- NEVER `systemctl restart` without running deploy-guardian agent first (or at minimum pytest)
- After restart, ALWAYS verify with `systemctl is-active` + last 5 journal lines
- If service fails to start, immediately check journal — do NOT retry blindly
- Use `/restart` skill for the full automated cycle

### Hot Files Warning
- `monitor.py`, `analyzer.py`, `indicators.py` are the blast radius center (~60% of all file operations)
- Any edit to these 3 files → run FULL test suite (not just the changed test)
- Use GitNexus impact analysis before touching these files

### AI Pipeline Guards
- Sonnet is primary. Opus is conditional (60-86% confidence only)
- NEVER bypass confidence threshold gates — this has caused real money losses
- All prompt changes to analyzer.py MUST be followed by `/prompt-audit`

### Rate Limit Mitigation
- RTK is active via PreToolUse hook — all CLI output is token-optimized (60-90% savings)
- Prefer targeted pytest (`pytest tests/test_specific.py`) over full suite during iteration
- Always use `-x` flag (stop on first failure) during development
- If rate-limited: wait, don't spam — the model and pricing tier don't change

### Session Auto-Actions
- On session start: run session-resumption agent (context reconstruction)
- After any trade close detected: run post-trade-analyst agent (Brier + lessons)
- Before any deploy: run deploy-guardian agent (validation gate)
- Periodically or on cost questions: run cost-watchdog agent

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

<!-- gitnexus:start -->
# GitNexus MCP

This project is indexed by GitNexus as **japan225-bot** (1290 symbols, 4000 relationships, 109 execution flows).

## Always Start Here

1. **Read `gitnexus://repo/{name}/context`** — codebase overview + check index freshness
2. **Match your task to a skill below** and **read that skill file**
3. **Follow the skill's workflow and checklist**

> If step 1 warns the index is stale, run `npx gitnexus analyze` in the terminal first.

## Skills

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
