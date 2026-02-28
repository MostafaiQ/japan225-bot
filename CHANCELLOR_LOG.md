# CHANCELLOR LOG — Japan 225 Trading Bot
## High Chancellor Intelligence Record
*Every entry is self-contained and implementable without follow-up questions.*

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
