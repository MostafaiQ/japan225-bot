# CHANCELLOR LOG — Japan 225 Trading Bot
## High Chancellor Intelligence Record
*Every entry is self-contained and implementable without follow-up questions.*

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
