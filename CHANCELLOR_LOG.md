# CHANCELLOR LOG — Japan 225 Trading Bot
## High Chancellor Intelligence Record
*Every entry is self-contained and implementable without follow-up questions.*

---

## [2026-02-28 23:30] — STRATEGY: Signal Frequency Expansion Verdict + Phased Implementation Plan

**Severity**: CRITICAL (live deployment blocked until frequency and SL gap resolved)
**Module(s)**: `core/indicators.py`, `core/confidence.py`, `config/settings.py`, `backtest.py`
**Specialist Input**: 4 expert agents reviewed (TA Strategy, Parameter Optimization, 5M Integration, Session/Data). All source code read directly to verify against expert claims.

### Problem / Observation

Post-HC-redesign backtest (60 days): 6 setups, 60% WR, PF=1.35, +$147.23 total P&L.
Win rate and profit factor are sound. Signal frequency (0.1/day) is not.
User requires minimum 1-3 trades/day for capital deployment.

4 expert agents proposed 20+ changes. This log entry is the HC synthesis and verdict.

### Open Gap: SL Discrepancy (most urgent, fix before any backtest runs)

WFO validated DEFAULT_SL_DISTANCE=150 (PF=3.67) over DEFAULT_SL_DISTANCE=200 (PF=2.56).
Live code: `DEFAULT_SL_DISTANCE=200` (settings.py).
detect_setup() hardcodes `entry - 200` (line 329) instead of using the constant.
Any backtest run before fixing this is validating the wrong SL. Fix first.

**Fix in config/settings.py:**
  DEFAULT_SL_DISTANCE = 200  →  DEFAULT_SL_DISTANCE = 150

**Fix in core/indicators.py, detect_setup() (all 4 SL assignments):**
  sl = entry - 200  →  sl = entry - DEFAULT_SL_DISTANCE
  sl = entry + 200  →  sl = entry + DEFAULT_SL_DISTANCE
  (also: tp = entry + 400 and entry - 400 → use DEFAULT_TP_DISTANCE constant)
  Add to imports: from config.settings import DEFAULT_SL_DISTANCE, DEFAULT_TP_DISTANCE

Also fix Finding 5 from previous HC session (still unresolved):
  In detect_setup() SHORT EMA50 rejection (line ~406):
  at_ema50_from_below = price <= ema50_15m + 5  →  price <= ema50_15m + 2

### Expert Verdicts Summary

ACCEPTED changes:
- SL=150 (Phase 0 — immediate, WFO-validated)
- RSI_ENTRY_HIGH_BOUNCE: relax from 48 to 55 ONLY after 29-second backtest confirms PF>=1.2, WR>=40%
- Replace bounce_starting (price > prev_close) with lower_wick >= 20pts (stronger confirmation)
  lower_wick = min(current_open, current_close) - current_low  — uses existing tf_15m output fields
- Add bb_lower_bounce_long setup: abs(price - bb_lower) <= 100, RSI 20-40, lower_wick >= 10
- NKD=F as backtest data source for London/NY sessions (^N225 stays for Tokyo)
- Session expansion sequence: London/NY Overlap first (13:30-16:00 UTC), then London, then NY

REJECTED changes:
- Remove bounce_starting gate entirely — removes only real-time reversal confirmation
- C4 loosening to price<=bb_mid+50 — reintroduces entering-mid-fall original flaw
- PAPER_TRADING_SESSION_GATE=False without NKD=F backtest validation per session
- VWAP setups (vwap_reclaim_long, vwap_rejection_short) before session-reset infrastructure built

DEFERRED to Phase 3:
- 5M confirmation layer (Expert 3) — doesn't increase frequency; win rate already 60%
- VWAP setups — needs session-reset boundary in analyze_timeframe() first
- EMA50 bounce/rejection — not yet validated

### Implementation Order

**Phase 0 — Immediate (no backtest needed)**
1. DEFAULT_SL_DISTANCE = 150 in settings.py
2. detect_setup(): all 4 SL/TP assignments use constants, not hardcoded 200/400
3. SHORT EMA50 tolerance: +5 → +2
4. Run: python -m pytest tests/ — must still show 233 passing

**Phase 1 — Parameter tuning + new setup (each step: run backtest, accept if PF>=1.2 and WR>=40%)**
1a. Run backtest at RSI_ENTRY_HIGH_BOUNCE=55 (one change, isolated)
1b. Replace bounce_starting with lower_wick >= 20pts in detect_setup() (one change, isolated)
1c. Add bb_lower_bounce_long to detect_setup() — new setup before existing LONG setups
    Conditions: bb_lower is not None and abs(price-bb_lower)<=100
                RSI 20-40
                daily_bullish=True
                lower_wick = min(open_15m, price) - tf_15m["low"] >= 10
                SL = entry - DEFAULT_SL_DISTANCE, TP = entry + DEFAULT_TP_DISTANCE
Target: ~0.4-0.6 signals/day in Tokyo session after Phase 1.

**Phase 2 — Session expansion (NKD=F data required)**
2a. Add NKD=F download to backtest data loader (yfinance ticker "NKD=F")
2b. Merge strategy: ^N225 for Tokyo (00:00-06:00 UTC), NKD=F for other hours
2c. Run backtest on all sessions — analyze per-session PF and WR
2d. Enable London/NY Overlap (13:30-16:00 UTC) only if per-session PF>=1.2
2e. Paper trade 15+ trades in new session before enabling next
2f. Set PAPER_TRADING_SESSION_GATE=False after Tokyo + at least one other session validated
Target: 1-2 signals/day.

**Phase 3 — Quality improvement (after frequency target met)**
3a. 5M confirmation layer
3b. VWAP setups (after session-reset boundary built)
3c. SL tightening for 5M-confirmed entries (120pts)

### Red Flags — Dangerous Combinations

- Removing bounce gate AND relaxing RSI in same backtest run: confounds causality
- C4 loosening (price <= bb_mid+50): NEVER — reintroduces original zero-edge flaw
- Enabling multiple sessions simultaneously: one at a time
- VWAP setups before session-reset: wrong VWAP values across session boundaries
- Any live deployment while live SL=200 but validation was done at SL=150

### Updated HC NO-GO Conditions for Live Deployment

Before enabling any new session:
  - Per-session NKD=F backtest: PF >= 1.2, WR >= 40%
  - Minimum 15 paper trades in that session
  - No more than 3 consecutive losses in session before review

Before declaring live-ready:
  - Total paper trades: minimum 30 across all enabled sessions
  - Overall WR >= 40%, PF >= 1.3, avg duration >= 30 minutes
  - DEFAULT_SL_DISTANCE=150 in live config (MEMORY.md must reflect current value)
  - All Phase 0 fixes applied and verified

Hard NO-GO:
  - VWAP setups without session-reset infrastructure
  - 5M integration without complete 5M pipeline
  - C4 loosened to price <= bb_mid + 50 for any reason

### Verification Steps
After Phase 0 fixes:
  python -m pytest tests/ → must show 233 passing
  grep "DEFAULT_SL_DISTANCE" config/settings.py → must show 150
  grep "entry - DEFAULT_SL_DISTANCE" core/indicators.py → must appear in detect_setup()

After Phase 1 each step:
  python backtest.py → check total setups count, WR, PF vs baseline (6 setups, 60% WR, PF=1.35)
  Acceptable: more setups with WR >= 40% and PF >= 1.2

---

## [2026-02-28 22:00] — PASSIVE AUDIT: Session end review (pre-screen fix, telegram rewrite, dashboard cost/sync)

**Severity**: HIGH (one item) / MEDIUM (three items) / LOW (six items)
**Module(s)**: `monitor.py`, `notifications/telegram_bot.py`, `core/indicators.py`, `dashboard/routers/chat.py`, `dashboard/services/claude_client.py`
**Specialist Input**: Direct code read — no sub-agents delegated (passive audit, no code changes)

### Work reviewed
1. monitor.py — pre-screen bug fix: 15M + Daily fetched in parallel, daily reused at confidence stage
2. telegram_bot.py — full rewrite: ReplyKeyboardMarkup, _nav_kb(), _dispatch_menu(), HTML helpers, edge cases
3. dashboard/routers/chat.py — GET /api/chat/costs + GET/POST /api/chat/history for cross-device sync
4. dashboard/services/claude_client.py — _log_cost() per iteration writing chat_costs.json, token-efficient-tools beta
5. core/indicators.py — bidirectional detect_setup(), EMA200 fallback logic verified

---

### FINDING 1 — MEDIUM: Naive datetime in _handle_position_closed causes future TypeError risk
**Module**: `monitor.py` lines ~764, ~877, ~167
**Problem**: `_on_trade_confirm` writes `"opened_at": datetime.now().isoformat()` (naive). `_handle_position_closed` subtracts `datetime.now() - datetime.fromisoformat(opened_at)`. Both are naive today, so no crash. But the startup_sync recovery path also writes `datetime.now().isoformat()` (naive). The moment any caller normalises to UTC-aware timestamps, the subtraction raises `TypeError: can't subtract offset-naive and offset-aware datetimes`.

**Recommended Fix**:
```python
# monitor.py ~877, _on_trade_confirm trade dict:
"opened_at": datetime.now(timezone.utc).isoformat(),

# monitor.py ~167, startup_sync recovery:
"opened_at": pos.get("created", datetime.now(timezone.utc).isoformat()),

# monitor.py ~764, _handle_position_closed duration calc:
open_dt = datetime.fromisoformat(opened_at)
if open_dt.tzinfo is None:
    open_dt = open_dt.replace(tzinfo=timezone.utc)
duration = int((datetime.now(timezone.utc) - open_dt).total_seconds() / 60)
```
**Verification**: Run existing 233 tests. Add a test that writes an aware opened_at and calls _handle_position_closed.

---

### FINDING 2 — HIGH: ig.close_position() blocks event loop in async Telegram handlers
**Module**: `notifications/telegram_bot.py` lines ~516, ~661, ~736
**Problem**: Three places call `self.ig.close_position(...)` synchronously inside `async def` handlers — `_cmd_kill`, the `menu_kill` branch of `_dispatch_menu`, and the `close_position:` callback branch of `_handle_callback`. This is a blocking IG REST API call (1–30s depending on IG latency) executed on the asyncio event loop thread, freezing all other coroutines — including the monitoring cycle. Under normal load this is a 1–3s freeze. Under IG stress this can block for 30s+, meaning the monitoring cycle cannot fire and position updates are missed.

**Recommended Fix** (apply to all three locations):
```python
# Replace:
result = self.ig.close_position(pos["deal_id"], pos["direction"], pos["lots"])

# With:
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(
    None,
    lambda: self.ig.close_position(pos["deal_id"], pos["direction"], pos["lots"])
)
```
All three call sites are inside `async def` methods so `await` is legal. No other change needed.
**Verification**: Run tests. Manually check that `_cmd_kill` is still `async def` (it is).

---

### FINDING 3 — MEDIUM: detect_setup SHORT EMA50 rejection allows price above EMA50
**Module**: `core/indicators.py` line ~400
**Problem**:
```python
at_ema50_from_below = price <= ema50_15m + 5 and dist_ema50 <= 30
```
Allows price to be 5 points ABOVE EMA50 while still triggering a SHORT "rejection" setup. Intent
(per comment) is "price came up to test EMA50 from below and is getting rejected." But `+5` allows
price above the barrier it's supposedly being rejected from. At Nikkei ~38,000, 5pts is negligible
(0.013%), so real-world impact is minimal. However the code intent and condition are misaligned.

**Recommended Fix**:
```python
# Remove the +5 allowance — if price must be "testing from below", it must be at or below EMA50
at_ema50_from_below = price <= ema50_15m and dist_ema50 <= 30
```
Or if tick-noise tolerance is wanted, name the constant:
```python
EMA50_SHORT_BUFFER_PTS = 2  # at module level
at_ema50_from_below = price <= ema50_15m + EMA50_SHORT_BUFFER_PTS and dist_ema50 <= 30
```
**Verification**: Re-run indicator unit tests. Confirm SHORT tests still pass with corrected condition.

---

### FINDING 4 — MEDIUM: No message length validation on POST /api/chat
**Module**: `dashboard/routers/chat.py` line ~71
**Problem**: `message: str` has no length cap. A user could send a 100,000-char message. Anthropic
would bill for full input tokens or reject with a 400 that surfaces as a 500 error. History list
has no server-side size cap either (frontend caps at 20 entries but server doesn't enforce it).
**Recommended Fix** (Pydantic v2 syntax):
```python
from pydantic import BaseModel, field_validator

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []

    @field_validator("message")
    @classmethod
    def message_not_too_long(cls, v):
        v = v.strip()
        if len(v) > 8000:
            raise ValueError("message exceeds 8000 char limit")
        return v

    @field_validator("history")
    @classmethod
    def history_cap(cls, v):
        return v[-20:]  # server-side MAX_TURNS*2 enforcement
```

---

### FINDING 5 — MEDIUM: chat_history.json write is not atomic — multi-device race condition
**Module**: `dashboard/routers/chat.py` lines ~26–31
**Problem**:
```python
_HISTORY_PATH.write_text(json.dumps({...}))
```
Not atomic. Two devices writing simultaneously can corrupt the file. Also lacks tmp+replace
pattern used by monitor._write_state(). If process is killed mid-write, file becomes corrupt JSON.
**Recommended Fix**:
```python
def _write_history(messages: list) -> str:
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    data = json.dumps({"messages": messages[-40:], "updated_at": ts})
    tmp = _HISTORY_PATH.with_suffix(".tmp")
    tmp.write_text(data)
    tmp.replace(_HISTORY_PATH)  # atomic rename on Linux
    return ts
```

---

### FINDING 6 — LOW: New anthropic.Anthropic() client per chat() call destroys connection pool
**Module**: `dashboard/services/claude_client.py` line ~324
**Problem**: `client = anthropic.Anthropic(...)` inside `chat()` recreates the httpx connection
pool on every request. Adds ~50–200ms per call (TLS renegotiation). No functional impact.
**Recommended Fix**: Hoist to module level:
```python
_client = anthropic.Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    default_headers={"anthropic-beta": "token-efficient-tools-2025-02-19"},
)
```
Reference `_client` inside `chat()`. Note: API key must be loaded before the module is imported
(it is, via python-dotenv in uvicorn startup).

---

### FINDING 7 — LOW: _dispatch_menu duplicates all command handler text logic (~130 lines)
**Module**: `notifications/telegram_bot.py`
**Problem**: Commands like `_cmd_balance` build a text string and send it. `_dispatch_menu`'s
`menu_balance` branch does the exact same thing with identical data-fetching and formatting code.
Any UI change must be made twice. The existing `_status_text()` helper correctly extracts the
text-building logic — this pattern should be extended.
**Recommended Fix**: Extract private text-builder methods:
- `_balance_text() -> str`
- `_journal_text() -> str`
- `_today_text() -> str`
- `_stats_text() -> str`
- `_cost_text() -> str`

Then both `_cmd_X` and `_dispatch_menu`'s `menu_X` branch call `self._X_text()`.
Reduces ~700 lines to ~450 lines. Zero functional change.

---

### FINDING 8 — LOW: Force scan does not interrupt the current sleep cycle
**Module**: `monitor.py` line ~907, `notifications/telegram_bot.py` _cmd_force
**Problem**: `/force` sends "Running immediately…" but the bot won't actually scan until the
current sleep expires (up to 5 minutes). The dashboard force-scan trigger has the same gap.
**Recommended Fix**: Add `self._wake_event = asyncio.Event()` in `__init__`. Signal it in
`_on_force_scan()`. Replace sleep with:
```python
try:
    await asyncio.wait_for(self._wake_event.wait(), timeout=interval)
    self._wake_event.clear()
except asyncio.TimeoutError:
    pass
```
Low urgency — 5 min max wait is tolerable for this use case.

---

### FINDING 9 — LOW: chat_costs.json write not atomic (corrupt-on-crash risk)
**Module**: `dashboard/services/claude_client.py` lines ~62–63
**Problem**: `_COSTS_PATH.write_text(json.dumps(entries[-500:]))` is not atomic. Crash mid-write
corrupts the file. On next read, `json.loads` throws, the except catches it, and the current cost
entry is silently lost. `_log_cost` already handles corruption gracefully with `except Exception:
pass` but the write itself could be made atomic.
**Recommended Fix**: Same tmp+replace pattern:
```python
tmp = _COSTS_PATH.with_suffix(".tmp")
tmp.write_text(json.dumps(entries[-500:]))
tmp.replace(_COSTS_PATH)
```

---

### Priority Table for Next Session

| Priority | Finding | File | Lines | Effort |
|----------|---------|------|-------|--------|
| HIGH | 2: blocking ig.close_position() in async handlers | telegram_bot.py | ~516, 661, 736 | 3 one-liners |
| MEDIUM | 1: naive datetime risk | monitor.py | ~167, 764, 877 | 5 lines |
| MEDIUM | 3: SHORT EMA50 +5 buffer misaligned | indicators.py | ~400 | 1 line |
| MEDIUM | 4: no message length validation | routers/chat.py | ~71 | 10 lines |
| MEDIUM | 5: chat_history write not atomic | routers/chat.py | ~26 | 3 lines |
| LOW | 6: Anthropic client recreated per call | claude_client.py | ~324 | 2 lines |
| LOW | 7: _dispatch_menu DRY violation | telegram_bot.py | ~541–672 | refactor |
| LOW | 8: force scan doesn't interrupt sleep | monitor.py | ~907 | asyncio.Event |
| LOW | 9: chat_costs write not atomic | claude_client.py | ~62 | 3 lines |

**CRITICAL NOTHING FOUND** — the pre-screen fix, telegram rewrite, and dashboard features are
structurally sound. Finding 2 (event loop block on kill) is the only issue with live-trading
safety implications and should be fixed first.

---

## [2026-02-28 14:00] — ARCHITECTURE REVIEW: Dashboard Pre-Implementation Audit

**Severity**: HIGH (multiple issues, one CRITICAL)
**Module(s)**: Proposed `dashboard/` (not yet created), `config/settings.py`, `storage/database.py`, `.env`
**Specialist Input**: High Chancellor direct analysis

### Problem / Observation

Full architectural review of the proposed web dashboard before implementation. Findings below are
organized by concern area. The implementation plan is sound in broad strokes but has several
real problems that would bite in production.

---

### CRITICAL FINDING: GitHub PAT embedded in git remote URL

The git remote is configured as:
```
https://MostafaiQ:[REDACTED_PAT]@github.com/...
```

The dashboard's `git push` subprocess will inherit this. This token appears in:
- `git remote -v` output (readable by any process running as ubuntu)
- Process list (`ps aux`) during git operations
- Any logging that captures subprocess commands

**Fix**: Remove the embedded PAT from the remote URL. Use `git credential store` or SSH keys
instead. See the recommended fix section in the final spec entry below.

---

### FINDING: SQLite WAL mode is not enabled — concurrent access risk

`monitor.py` writes to `trading.db` continuously (every 60s in monitoring mode, every 5 min in
scanning mode). The dashboard reads the same file. SQLite's default journal mode is DELETE, which
takes a write lock that blocks all readers. With WAL mode enabled, readers never block writers
and writers never block readers (on separate pages). Without it, dashboard reads during an active
monitoring cycle will either block or return `database is locked` errors.

**Fix**: Enable WAL mode in `Storage.__init__()`:
```python
def _init_db(self):
    with self._conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL
        conn.executescript(...)
```
The dashboard should open its read connections with `check_same_thread=False` and
`timeout=5` to gracefully handle contention.

---

### FINDING: Config override approach requires bot restart for every change

The proposed `dashboard_overrides.json` approach means every parameter change requires restarting
`monitor.py`. If a position is open when restart happens, the startup_sync() will recover it
correctly, but there's a ~30s window (RestartSec=30) where the bot is not monitoring. During
that window the broker's hard stops protect the position, but no soft logic (trailing stop,
breakeven progression) runs.

**Better approach**: Use a `dashboard_overrides.json` file that `monitor.py` re-reads on each
main cycle, not just at startup. Add a `_reload_overrides()` call at the top of `_main_cycle()`.
Only the safe parameters (confidence thresholds, scan intervals, cooldown minutes) reload hot.
Trading-critical parameters (SL/TP distances, margin limits) still require restart because they
affect in-flight trade logic.

---

### FINDING: Bot restart with open position — no user warning

The dashboard's "Restart Bot" button has no guard against open positions. Restarting with an open
position means 30 seconds of unmonitored time. startup_sync() handles recovery correctly, but
the user may not realize this.

**Fix**: The `/api/bot/restart` endpoint must check `position_state.has_open` before allowing
restart and return a warning response requiring explicit acknowledgment.

---

### FINDING: git push automation has no safety checks

The proposed implementation runs `git add -A && git commit && git push` directly. Problems:
1. `git add -A` will stage `storage/data/trading.db` (a binary that changes every cycle),
   `.env` (secrets), `__pycache__/`, and any other dirty files. The `.gitignore` may not cover all
   of these, and the dashboard would be committing live DB state and secrets.
2. No check that the diff is actually a Python file modification (not accidental deletion of a
   module).
3. No check that the push succeeds before reporting success to the user.
4. No rollback if the bot restart after push fails.

**Fix**: Scope `git add` to only the specific file(s) in the diff. Validate the file is under
`/home/ubuntu/japan225-bot/` (prevent path traversal). Never stage `.env`, `*.db`, or
`__pycache__`. Check git push exit code before reporting success.

---

### FINDING: Claude chat token budget is uncapped

All 12 digests total ~22KB (~5,500 tokens). MEMORY.md adds more. If the system prompt includes
all digests for every chat turn, and conversation history is unbounded, a long conversation will
hit context limits and costs will grow with every turn. With claude-sonnet-4-6 at $3/$15 per
million tokens, a 20-turn debugging session with full context could cost $2-5 in a single
session.

**Fix**: See the tiered context injection strategy in the spec below.

---

### Recommended Fix / Final Spec

See the detailed implementation spec in the response to the user. All architectural decisions
are documented there.

### Verification Steps
- After implementing WAL mode: run `monitor.py` and the dashboard simultaneously, perform a
  dashboard read during a monitor write cycle, confirm no `database is locked` error.
- After securing git remote: `git remote -v` should show no credentials in the URL.
- After implementing position guard: attempt bot restart via dashboard with a simulated open
  position in the DB; confirm the warning response fires.

---

## [2026-02-28] — STRATEGY: New Setup Types Assessment — Signal Frequency Expansion Phase A

**Severity**: HIGH (live deployment blocked; frequency 0.33/day vs target 1-3/day)
**Module(s)**: `core/indicators.py`, `core/confidence.py`, `config/settings.py`
**Specialist Input**: HC direct analysis of live code + backtest results. No sub-agents needed (full context available).
**Backtest baseline**: 14 setups / 42 days = 0.33/day. 11 trades executed (dedup). OOS PF=1.54 (strategy generalises).

### Problem / Observation

Post-redesign backtest shows acceptable quality (OOS PF=1.54, WR=50%) but insufficient frequency.
The system is SEMI-AUTOMATED: detect_setup() pre-screens, Sonnet+Opus are the real quality gate,
human presses CONFIRM/REJECT. detect_setup() must generate 1-3 candidates/day for AI review.

Currently: 1 enabled LONG setup type (bollinger_mid_bounce), 2 SHORT setup types (disabled by
daily_bullish requirement in current market — ^N225 above EMA200 = daily_bullish=True at all times).
RSI gate (35-48) and lower_wick >= 20pts (not yet implemented — still `price > prev_close`) are
the primary frequency constraints.

London 0% WR on 7 trades: statistically thin sample. AI filtering is the correct mechanism
to reject bad-context London entries. Do NOT exclude London by code.

### Root Cause Analysis

Frequency cannot reach 1-3/day from one setup type in one session (Tokyo). Two levers are needed:
1. More setup types / relaxed parameters in detect_setup() (Phase A)
2. Session expansion to London + NY with NKD=F data (Phase B / Phase 2)

Phase A alone reaches an estimated 0.48/day (Tokyo only). Phase B is required for 1-3/day target.

### Phase 0 Status (verify before any code change)

As of 2026-02-28, ALL Phase 0 items are COMPLETE:
- DEFAULT_SL_DISTANCE=150 in settings.py (WFO-validated, PF=3.67)
- detect_setup() uses DEFAULT_SL_DISTANCE constant — confirmed in live code (line 366)
- SHORT EMA50 tolerance: price <= ema50_15m + 2 — confirmed in live code (line 445)
- 233 tests passing

### Recommended Fix — Phase A (implement in strict sequence, one backtest per step)

**STEP A1 — Lower wick gate upgrade (quality, slight frequency reduction)**

Replace `price > prev_close` bounce gate with `lower_wick >= 20pts` in the bollinger_mid_bounce block.
In `core/indicators.py`, detect_setup(), LONG Setup 1 block:

```python
# REMOVE these lines:
prev_close = tf_15m.get("prev_close")
bounce_starting = prev_close is not None and price > prev_close

# ADD these lines:
candle_open = tf_15m.get("open")
candle_low  = tf_15m.get("low")
if candle_open is not None and candle_low is not None:
    lower_wick = min(candle_open, price) - candle_low
    bounce_confirmed = lower_wick >= 20  # genuine rejection of lower prices (pin bar / hammer)
else:
    bounce_confirmed = False

# CHANGE the entry condition:
# OLD: if near_mid_pts and rsi_ok_long and above_ema50 and bounce_starting:
# NEW:
if near_mid_pts and rsi_ok_long and above_ema50 and bounce_confirmed:
```

Acceptance criterion: frequency must stay >= 8 setups / 42 days (0.19/day). If it drops below,
reduce wick threshold to 15pts and re-run backtest.

Note: `open` and `low` are already output by analyze_timeframe() (lines 179, 181 in indicators.py).
No changes to analyze_timeframe() needed.

---

**STEP A2 — RSI gate widening 35-48 → 35-55 (frequency increase, conditional on backtest)**

Run backtest at RSI_ENTRY_HIGH_BOUNCE=55. Accept ONLY if PF >= 1.2 AND WR >= 40%.
Do NOT run simultaneously with A1 — test each change independently.

In `config/settings.py`:
```python
RSI_ENTRY_HIGH_BOUNCE = 55   # was 48. Only after backtest validation.
```

`core/indicators.py` line 359 already uses this constant: `35 <= rsi_15m <= RSI_ENTRY_HIGH_BOUNCE`.
No change needed in indicators.py. Verify confidence.py also reads from settings.RSI_ENTRY_HIGH_BOUNCE
(not a hardcoded 48).

---

**STEP A3 — Add bollinger_lower_bounce LONG setup (new setup type, high conviction)**

After A1 and A2 are settled, add this new setup. Insert in `core/indicators.py`, detect_setup(),
LONG SETUPS block, AFTER the `return result` of bollinger_mid_bounce and BEFORE the
`if ENABLE_EMA50_BOUNCE_SETUP` block (line 387 area):

```python
# --- LONG Setup 3: Bollinger Lower Band Bounce ---
# Deep oversold at 2-std-dev band. Higher conviction than BB mid.
# No above_ema50 gate — at the lower band, price may be below EMA50 (expected and acceptable).
# The AI will evaluate EMA50 position as part of its quality assessment.
if bb_lower and rsi_15m:
    near_lower_pts = abs(price - bb_lower) <= 80   # must actually be at the band
    rsi_ok_lower = 20 <= rsi_15m <= 40             # deep oversold — tighter than BB mid
    candle_open = tf_15m.get("open")
    candle_low  = tf_15m.get("low")
    if candle_open is not None and candle_low is not None:
        lower_wick_pts = min(candle_open, price) - candle_low
        rejection_confirmed = lower_wick_pts >= 15  # looser than BB mid (15 not 20 — band is harder to reach)
    else:
        rejection_confirmed = False

    if near_lower_pts and rsi_ok_lower and rejection_confirmed:
        entry = price
        sl = entry - DEFAULT_SL_DISTANCE           # 150pts
        tp = entry + DEFAULT_TP_DISTANCE           # 400pts → R:R = 2.67:1
        macro_note = (
            f" 4H RSI {rsi_4h:.1f} — MACRO OVERSOLD, multi-TF confluence."
            if rsi_4h and rsi_4h < 40 else ""
        )
        result.update({
            "found": True,
            "type": "bollinger_lower_bounce",
            "direction": "LONG",
            "entry": round(entry, 1),
            "sl": round(sl, 1),
            "tp": round(tp, 1),
            "reasoning": (
                f"LONG: BB lower band bounce on 15M. "
                f"Price {abs(price - bb_lower):.0f}pts from lower band ({bb_lower:.0f}). "
                f"RSI {rsi_15m:.1f} deeply oversold. "
                f"Lower wick {lower_wick_pts:.0f}pts rejection confirmed. "
                f"Daily bullish.{macro_note}"
            ),
        })
        return result
```

Acceptance criterion: any new signals generated with WR >= 40% in backtest.

---

**STEP A4 — Update confidence.py C2 and C3 for BB lower bounce**

In `core/confidence.py`, LONG branch of compute_confidence():

C2 (entry_level) — add near_lower as valid entry zone:
```python
bb_lower_15m = tf_15m.get("bollinger_lower")
near_lower = abs(price - bb_lower_15m) <= 80 if bb_lower_15m else False
c2 = near_mid or near_ema50 or near_lower  # add near_lower
```

C3 (rsi_15m LONG) — setup-aware gate to handle deep-oversold lower-band RSI (20-35):
```python
bb_lower_15m = tf_15m.get("bollinger_lower")
near_lower_zone = abs(price - bb_lower_15m) <= 80 if bb_lower_15m else False
if near_lower_zone:
    c3 = rsi_15m is not None and 20 <= rsi_15m <= 40  # deep oversold for lower band
else:
    c3 = rsi_15m is not None and LONG_RSI_LOW <= rsi_15m <= LONG_RSI_HIGH  # standard gate
```

Rationale: Without this change, a BB lower bounce with RSI=22 (strongest possible oversold) would
FAIL C3 (which requires RSI >= 35), reducing confidence score below the 50% AI escalation gate
and causing the strongest signals to be silently dropped.

Run all tests after this change: `python -m pytest tests/ -q`. All 233+ must pass.

---

### Impact if Phase A Not Implemented

Bot continues generating 0.33 signals/day in Tokyo only. At this rate:
- Paper trading target (30 trades) takes 90+ days to reach
- Live capital deployment remains blocked
- Session expansion cannot be validated without paper data
- User requirement of 1-3/day unmet, no path to live trading

### Verification Steps

A1: After wick gate change, run backtest. Confirm >= 8 setups in 42 days. If below, reduce to 15pts.
A2: After RSI_ENTRY_HIGH_BOUNCE=55, run backtest. Accept only if PF >= 1.2 AND WR >= 40%.
A3: After bollinger_lower_bounce, run backtest. Confirm new setup fires at least 2x in 42 days.
A4: After confidence.py changes, run `python -m pytest tests/ -q`. All 233+ tests must pass.
     Also manually check: with rsi_15m=25 and price near bb_lower, confirm C3 passes (was failing before).

### Setups to NEVER Add (hard rejections)

1. BB Upper Breakout (LONG momentum) — SL=150pts calibrated for pullback entries. Breakouts have
   different trade dynamics. Would require separate exit strategy. Do not implement.
2. VWAP setups (any) — VWAP has no session reset. Wrong values across session boundaries guaranteed.
   Build session-reset VWAP infrastructure first. This is a Phase 3 item.
3. Removing bounce gate entirely — confirmed as the key fix that separated 0.8% WR from 60% WR.
4. C4 loosened to price <= bb_mid + 50 — reintroduces "entering mid-fall" structural flaw.
5. EMA9 × EMA50 golden cross — deferred (requires prev_ema9/prev_ema50 in analyze_timeframe output).

### Session Expansion Reminder (Phase B, post Phase A)

Path to 1-3/day requires Phase B (London + NY). Sequence:
1. Download NKD=F 15M (90 days via yfinance "NKD=F")
2. Modify backtest to use ^N225 for Tokyo (00:00-06:00 UTC), NKD=F for London + NY
3. Run per-session backtest. Enable session only if PF >= 1.2 per session.
4. Paper trade 15+ trades per session. Overall 30 before live.
5. With all 3 sessions: estimated 0.48/day × 3 = 1.44/day — within target.

---
