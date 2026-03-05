---
description: "Scan codebase and estimate what it would cost a real team to build vs AI"
allowed-tools: Bash(find:*), Bash(wc:*), Bash(grep:*), Bash(git:*), Bash(cat:*), Bash(ls:*), Bash(head:*), Bash(tail:*), Bash(sort:*), Bash(uniq:*), Bash(awk:*), Bash(sed:*), Read, Glob, Grep
---

Scan this codebase and generate a detailed cost estimate report comparing what this project would cost to build with a real team vs what it cost using AI.

## Step 1 — Scan the codebase

Run these commands to gather raw data:

```
# Total file counts by extension (exclude noise dirs)
find . -type f \
  -not -path './.git/*' \
  -not -path './node_modules/*' \
  -not -path './venv/*' \
  -not -path './__pycache__/*' \
  -not -path './storage/data/*' \
  -not -path './.claude/worktrees/*' \
  | sed 's/.*\.//' | sort | uniq -c | sort -rn | head -30

# Lines of code (source files only)
find . -type f \( -name '*.py' -o -name '*.js' -o -name '*.ts' -o -name '*.tsx' -o -name '*.html' -o -name '*.css' -o -name '*.json' -o -name '*.md' -o -name '*.yml' -o -name '*.yaml' \) \
  -not -path './.git/*' \
  -not -path './node_modules/*' \
  -not -path './venv/*' \
  -not -path './__pycache__/*' \
  -not -path './storage/data/*' \
  | xargs wc -l 2>/dev/null | tail -1

# Python files breakdown
find . -name '*.py' -not -path './venv/*' -not -path './__pycache__/*' | wc -l

# Test files
find . -name 'test_*.py' -o -name '*_test.py' | wc -l

# Config / env vars
grep -r "^[A-Z_]\+\s*=" config/settings.py 2>/dev/null | wc -l

# External API integrations (imports / calls)
grep -rh "import\|requests\|httpx\|aiohttp\|anthropic\|lightstreamer\|telegram" \
  --include='*.py' -not -path './venv/*' | grep -v '#' | sort -u | head -40

# DB models / tables
grep -rh "CREATE TABLE\|self\.conn\|cursor\|execute(" --include='*.py' -not -path './venv/*' | wc -l

# Unique environment variables
grep -rh "os\.environ\|os\.getenv\|getenv" --include='*.py' -not -path './venv/*' \
  | grep -oP '[A-Z_]{3,}' | sort -u

# Git log — commit count and date range
git log --oneline 2>/dev/null | wc -l
git log --format="%ad" --date=short 2>/dev/null | tail -1   # first commit
git log --format="%ad" --date=short 2>/dev/null | head -1   # last commit

# Git log — estimate AI session hours from commit timestamps
git log --format="%ad" --date=format:"%Y-%m-%d %H:%M" 2>/dev/null | head -50

# Services / systemd units
find . -name '*.service' -o -name '*.ini' | grep -v venv | head -10

# Test count
grep -r "^def test_\|^    def test_" tests/ 2>/dev/null | wc -l

# Dashboard routes (FastAPI endpoints)
grep -rh "@app\.\|@router\." --include='*.py' dashboard/ 2>/dev/null | wc -l

# Telegram commands
grep -rh "CommandHandler\|MessageHandler\|@bot\|async def.*command\|/[a-z]" \
  --include='*.py' notifications/ 2>/dev/null | grep -v '#' | wc -l
```

## Step 2 — Analyse the data

From the scan output, identify:
- **Languages & frameworks**: Python, FastAPI, asyncio, trading-ig, Lightstreamer, python-telegram-bot, Anthropic SDK, SQLite, Claude Code CLI
- **Architecture layers**: AI pipeline, broker API, real-time streaming, Telegram bot, web dashboard, risk engine, backtester, test suite
- **Complexity signals**: number of modules, external integrations, async patterns, AI prompt engineering depth, config constants

## Step 3 — Estimate team composition

Map the detected tech to roles and estimate hours. Use these Gulf/Kuwait freelance rates (2025-2026):

| Role | Hourly Rate |
|------|-------------|
| Junior Developer | $15–$22/hr |
| Mid Developer | $22–$40/hr |
| Senior Developer | $40–$60/hr |
| Senior Architect / AI Engineer | $60–$90/hr |
| UI/UX Designer | $18–$35/hr |
| QA Engineer | $15–$28/hr |
| DevOps Engineer | $30–$52/hr |
| Project Manager | $30–$55/hr |

Monthly salaries for reference (KWD / USD):
- Junior: KWD 600–1,100 / $2,000–$3,500
- Mid: KWD 1,100–2,000 / $3,500–$6,500
- Senior: KWD 2,000–3,000 / $6,500–$10,000
- Architect/AI: KWD 3,000–4,500 / $10,000–$15,000
- Designer: KWD 900–1,700 / $3,000–$5,500
- QA: KWD 750–1,400 / $2,500–$4,500
- DevOps: KWD 1,500–2,600 / $5,000–$8,500
- PM: KWD 1,500–2,750 / $5,000–$9,000

For this project, estimate hours for each applicable role:
- **Senior Python / Backend Dev** (core trading engine, risk manager, exit manager, indicators, confidence scoring)
- **AI / Prompt Engineer** (Sonnet+Opus pipeline, system prompts, prompt learnings, Brier scoring)
- **Broker Integration Dev** (IG REST API, Lightstreamer streaming, candle caching, delta fetches)
- **DevOps / Cloud** (Oracle Cloud VM, systemd services, ngrok, deployment scripts)
- **Backend / API Dev** (FastAPI dashboard, SQLite schema, storage layer)
- **Frontend Dev** (dashboard UI, real-time updates, chart rendering)
- **QA / Test Engineer** (395 tests, test fixtures, coverage)
- **Telegram Bot Dev** (commands, inline buttons, alert flow)
- **Project Manager** (architecture decisions, planning, iteration management)

## Step 4 — Generate the report

Output a clean markdown report with these sections:

---

# Cost Estimate Report — Japan 225 Trading Bot

## Codebase Scan

Present a table or bullet list with:
- Total source files and LOC
- Languages detected
- Frameworks and libraries
- External integrations (IG API, Anthropic, Telegram, Lightstreamer, ngrok, Google News RSS, CNN Fear & Greed)
- DB tables / models
- Test count
- Environment variables
- Systemd services

## Team Required

| Role | Hours Estimated | Rate ($/hr) | Cost (USD) | Cost (KWD) |
|------|----------------|-------------|-----------|-----------|
| ... | ... | ... | ... | ... |
| **TOTAL** | | | | |

Use mid-range Gulf freelance rates. KWD conversion: 1 KWD ≈ 3.25 USD.

## Build Scenarios

| Metric | Solo Dev | Small Team (2–3) | Growth Team (4–6) | Agency |
|--------|----------|-----------------|-------------------|--------|
| Calendar Time | | | | |
| Total Human Hours | | | | |
| Total Cost (KWD) | | | | |
| Total Cost (USD) | | | | |

Solo = 1 senior dev does everything (slower, sequential).
Small = 2-3 mid/senior devs splitting work.
Growth = 4-6 specialists working in parallel.
Agency = full team + PM overhead + margin (1.3–1.5× multiplier).

## AI Equivalent

From git log, estimate the number of actual AI working sessions and approximate wall-clock hours of AI interaction. If git log shows commits spanning multiple days, estimate ~1–3 hours of active prompting per working day.

| Basis | Value |
|-------|-------|
| Git commits | |
| Project span (first → last commit) | |
| Estimated AI active hours | |
| Estimated Claude Pro cost | ~$200/mo (prorated) |

## Speed vs Human Developer

| Metric | Human (Solo Senior) | AI-Assisted |
|--------|---------------------|-------------|
| Estimated hours | | |
| Calendar time | | |
| Speed multiplier | | |

## Cost Comparison

| Item | Cost (KWD) | Cost (USD) |
|------|-----------|-----------|
| Human team (growth scenario) | | |
| AI tooling (Claude Pro, prorated) | ~KWD 60 | ~$200 |
| Net savings | | |
| ROI | | |

## The Headline

Write a single compelling paragraph summarising: how many AI hours were spent, what a human team would have charged for equivalent work (Kuwait/Gulf rates), the effective hourly value generated per AI hour, and what a typical Gulf growth team would bill for this scope.

## Assumptions

List all assumptions made (hourly rates, KWD/USD conversion, AI hours estimation method, team structure, etc.).

---

Be precise. Use actual numbers from the scan. Do not invent LOC or file counts — read them from the Bash output. Present KWD as primary currency with USD in parentheses throughout.
