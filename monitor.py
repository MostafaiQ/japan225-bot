"""
Japan 225 Trading Bot — VM Monitor (always-on orchestrator)

This is the main process running 24/7 on Oracle Cloud Free Tier.

Two modes:
  SCANNING MODE (no open position):
    - Every 5 minutes during active sessions (Tokyo, London, NY)
    - Every 30 minutes during off-hours (heartbeat only)
    - Local pre-screen → local confidence → AI escalation (if passes)
    - AI cooldown: 30 minutes between escalations
    - Waits for user confirmation via Telegram before executing

  MONITORING MODE (position open):
    - Every 60 seconds
    - Checks IG for position existence (with 2-consecutive-empty safety)
    - Tiered adverse move alerts (mild/moderate/severe)
    - 3-phase exit management (breakeven/runner)
    - Stale data detection
    - Event proximity alerts

Startup: always runs startup_sync() to reconcile DB state with IG reality.
"""
import asyncio
import json
import logging
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config.settings import (
    LOG_FORMAT, LOG_LEVEL, TRADING_MODE, CONTRACT_SIZE,
    MONITOR_INTERVAL_SECONDS, POSITION_CHECK_EVERY_N_CYCLES, OPUS_POSITION_EVAL_EVERY_N,
    SCAN_INTERVAL_SECONDS, OFFHOURS_INTERVAL_SECONDS,
    AI_COOLDOWN_MINUTES, HAIKU_MIN_SCORE, PRICE_DRIFT_ABORT_PTS, SAFETY_CONSECUTIVE_EMPTY,
    calculate_margin, calculate_profit, SPREAD_ESTIMATE, DEFAULT_SL_DISTANCE,
    MIN_CONFIDENCE, MIN_CONFIDENCE_SHORT, MIN_RR_RATIO, MAX_OPEN_POSITIONS,
    DAILY_EMA200_CANDLES, PRE_SCREEN_CANDLES, AI_ESCALATION_CANDLES,
    MINUTE_5_CANDLES, DISPLAY_TZ, display_now,
    EXTREME_DAY_RANGE_PTS,
    CONTRADICTORY_SIGNAL_MIN_SCORE, CONTRADICTORY_SIGNAL_MAX_GAP,
)
from core.ig_client import IGClient, POSITIONS_API_ERROR
from core.indicators import analyze_timeframe, detect_setup, compute_session_context
from core.session import get_current_session, is_no_trade_day
from core.momentum import MomentumTracker, TIER_SEVERE
from core.confidence import compute_confidence
from trading.exit_manager import ExitManager, ExitPhase
from trading.risk_manager import RiskManager
from storage.database import Storage
from notifications.telegram_bot import TelegramBot
from ai.analyzer import AIAnalyzer, WebResearcher, post_trade_analysis

logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
# Set all log timestamps to Kuwait time (UTC+3)
logging.Formatter.converter = lambda *args: display_now().timetuple()
logger = logging.getLogger("monitor")


def _secs_to_next_session() -> int:
    """Return seconds until the next trading session opens (UTC). Min 30s."""
    from config.settings import SESSION_HOURS_UTC
    now = datetime.now(timezone.utc)
    frac = now.hour + now.minute / 60.0 + now.second / 3600.0
    starts = sorted(v[0] for v in SESSION_HOURS_UTC.values())  # e.g. [0, 8, 16]
    for s in starts:
        if s > frac:
            return max(30, int((s - frac) * 3600))
    # All starts are earlier today — next is first session tomorrow
    return max(30, int((24 - frac + min(starts)) * 3600))


class TradingMonitor:
    """
    Main VM process. Runs the full scan + monitoring loop.
    """

    def __init__(self):
        self.storage = Storage()
        self.ig = IGClient()
        self.risk = RiskManager(self.storage)
        self.telegram = TelegramBot(self.storage, self.ig)
        self.exit_manager = ExitManager(self.ig, self.storage, self.telegram)
        self.analyzer = AIAnalyzer()
        self.researcher = WebResearcher()
        self.running = False
        self.scanning_paused = False  # /pause command toggles this
        # Per-position tracker dict: {deal_id: {momentum_tracker, price_buffer, opus_eval_counter, buffer_save_counter, empty_count, check_counter}}
        self._position_trackers: dict[str, dict] = {}
        # Legacy singleton aliases (used by _on_pos_check and other code paths)
        self.momentum_tracker: MomentumTracker = None
        self._position_empty_count = 0   # Consecutive empty responses from IG
        self._position_check_counter = 0  # Increments every 2s cycle; position existence checked every N cycles
        self._force_scan_event = asyncio.Event()
        self._pos_check_running = False
        self._trade_execution_lock = asyncio.Lock()  # C1 fix: prevent double position opening
        self._started_at = datetime.now(timezone.utc)
        self._last_scan_time: datetime | None = None
        self._next_scan_at: datetime | None = None
        self._current_session: str | None = None   # persists across write_state calls
        self._current_price: float | None = None
        self._last_scan_detail: dict = {}  # dashboard: shows last scan outcome
        self._dashboard_force_scan: bool = False  # Set by poll task, consumed by _scanning_cycle
        self._last_opus_decision: dict | None = None  # Track Opus direction consistency
        self._position_price_buffer: deque = deque(maxlen=10800)  # Legacy — first position's buffer
        self._streaming_reconnect_counter = 0    # Consecutive cycles without streaming price → triggers reconnect
        self._concurrent_scan_running: bool = False  # Guard: prevents overlapping background scans
        self._last_concurrent_scan_at: datetime | None = None  # Last time a concurrent scan ran
        # Paths for dashboard integration
        self._state_path   = Path(__file__).parent / "storage" / "data" / "bot_state.json"
        self._overrides_path = Path(__file__).parent / "storage" / "data" / "dashboard_overrides.json"
        self._trigger_path   = Path(__file__).parent / "storage" / "data" / "force_scan.trigger"
        self._clear_cd_path  = Path(__file__).parent / "storage" / "data" / "clear_cooldown.trigger"
        self._force_open_pending_path = Path(__file__).parent / "storage" / "data" / "force_open_pending.json"
        self._force_open_trigger_path = Path(__file__).parent / "storage" / "data" / "force_open.trigger"
        self._pos_check_trigger_path  = Path(__file__).parent / "storage" / "data" / "pos_check.trigger"
        self._price_buffer_cache_path = Path(__file__).parent / "storage" / "data" / "price_buffer_cache.json"
        self._buffer_save_counter = 0  # Save every N monitoring cycles
        self._last_state_write: float = 0.0  # Throttle _write_state to every 10s

    def _get_tracker(self, deal_id: str) -> dict:
        """Get or create per-position tracker state."""
        if deal_id not in self._position_trackers:
            self._position_trackers[deal_id] = {
                "momentum_tracker": None,
                "price_buffer": deque(maxlen=10800),
                "opus_eval_counter": 0,
                "buffer_save_counter": 0,
                "empty_count": 0,
                "check_counter": 0,
            }
        return self._position_trackers[deal_id]

    def _remove_tracker(self, deal_id: str):
        """Remove per-position tracker and clean up cache file."""
        self._position_trackers.pop(deal_id, None)
        cache = Path(__file__).parent / "storage" / "data" / f"price_buffer_{deal_id}.json"
        cache.unlink(missing_ok=True)

    # ============================================================
    # STARTUP
    # ============================================================

    async def start(self):
        """Entry point. Connects, syncs state, starts loop."""
        logger.info("=" * 60)
        logger.info("JAPAN 225 MONITOR STARTING")
        logger.info(f"Mode: {TRADING_MODE}")
        logger.info("=" * 60)

        # Initialize Telegram FIRST — must be available even when IG is down
        await self.telegram.initialize()
        self.telegram.on_trade_confirm = self._on_trade_confirm
        self.telegram.on_force_scan = self._on_force_scan
        self.telegram.on_pos_check = self._on_pos_check
        await self.telegram.start_polling()

        # Write initial bot_state.json so dashboard has fresh data immediately
        self._write_state(phase="STARTING", force=True)

        # Connect to IG — 3 fast retries, then retry every 5 min until IG recovers
        connected = False
        for attempt in range(3):
            if self.ig.connect():
                connected = True
                break
            logger.warning(f"IG connect attempt {attempt + 1}/3 failed, retrying in 10s...")
            await asyncio.sleep(10)

        if not connected:
            logger.critical("IG connection failed after 3 attempts. Waiting for IG to recover.")
            await self.telegram.send_alert(
                "⚠️ IG API unavailable (503/500 — likely weekend maintenance).\n"
                "Telegram is online. Bot will retry IG (backoff: 60s→120s→300s max).\n"
                "Use /status for updates."
            )
            _startup_backoff = 60
            while not connected:
                self._write_state(phase="IG_DISCONNECTED", force=True)
                logger.info(f"Retrying IG connection in {_startup_backoff}s...")
                await asyncio.sleep(_startup_backoff)
                _startup_backoff = min(_startup_backoff * 2, 300)
                if self.ig.connect():
                    connected = True
                    await self.telegram.send_alert("✅ IG reconnected. Bot resuming normal operation.")

        # Startup sync — reconcile DB state with IG reality
        await self.startup_sync()

        # Start Lightstreamer price tick streaming (transparent REST fallback if unavailable)
        self.ig.start_streaming()

        # Check for AI result that survived a bot restart
        await self._recover_pending_ai()

        self.running = True
        self._trigger_poll_task = asyncio.create_task(self._poll_trigger_file())

        try:
            while self.running:
                await self._main_cycle()
        except asyncio.CancelledError:
            logger.info("Monitor loop cancelled")
        finally:
            self._trigger_poll_task.cancel()
            try:
                await self._trigger_poll_task
            except (asyncio.CancelledError, Exception):
                pass
            await self._shutdown()

    # ============================================================
    # PENDING AI RECOVERY (after bot restart during analysis)
    # ============================================================

    async def _recover_pending_ai(self):
        """Check if any AI analyses survived the previous bot restart."""
        data_dir = Path(__file__).parent / "storage" / "data"
        try:
            for pending in sorted(data_dir.glob("ai_pending_*.txt")):
                age = datetime.now().timestamp() - pending.stat().st_mtime
                if age > 300:  # older than 5 minutes — stale
                    pending.unlink(missing_ok=True)
                    continue
                content = pending.read_text().strip()
                pending.unlink(missing_ok=True)
                if not content:
                    continue
                logger.info(f"Recovered pending AI result ({age:.0f}s old, {len(content)} chars)")
                preview = content[:600] + ("..." if len(content) > 600 else "")
                await self.telegram.send_alert(
                    f"Recovered AI analysis from before restart:\n\n{preview}"
                )
        except Exception as e:
            logger.warning(f"Failed to recover pending AI result: {e}")

    # ============================================================
    # STARTUP SYNC (crash recovery)
    # ============================================================

    async def startup_sync(self):
        """
        Reconcile local DB state with IG on every restart.
        Handles multiple positions — iterates all IG positions and all DB positions.
        """
        logger.info("Running startup sync...")
        ig_positions = self.ig.get_open_positions()
        db_positions = self.storage.get_all_position_states()

        if ig_positions is POSITIONS_API_ERROR:
            msg = "Startup sync: IG API unavailable. Cannot verify position state. Proceeding with DB state."
            logger.warning(msg)
            await self.telegram.send_alert(msg)
            # Still init trackers for any DB positions
            for db_pos in db_positions:
                deal_id = db_pos.get("deal_id")
                if deal_id:
                    direction = "LONG" if (db_pos.get("direction") or "BUY").upper() in ("BUY", "LONG") else "SHORT"
                    entry = float(db_pos.get("entry_price") or 0)
                    tracker = self._get_tracker(deal_id)
                    tracker["momentum_tracker"] = MomentumTracker(direction, entry)
            return

        ig_deal_ids = {p.get("deal_id") for p in ig_positions}
        db_deal_ids = {p.get("deal_id") for p in db_positions}

        # IG positions not in DB → recover
        for pos in ig_positions:
            ig_deal = pos.get("deal_id")
            if ig_deal not in db_deal_ids:
                logger.warning(f"RECOVERY: IG has position {ig_deal} not in DB. Syncing.")
                direction_raw = (pos.get("direction") or "BUY").upper()
                direction_log = "LONG" if direction_raw in ("BUY", "LONG") else "SHORT"
                self.storage.open_trade_atomic(
                    trade={
                        "deal_id": ig_deal,
                        "direction": direction_log,
                        "lots": pos.get("size"),
                        "entry_price": pos.get("level"),
                        "stop_loss": pos.get("stop_level"),
                        "take_profit": pos.get("limit_level"),
                        "balance_before": 0,
                        "confidence": 0,
                        "setup_type": "recovered",
                        "session": "unknown",
                        "ai_analysis": "Position recovered on startup — opened while bot was offline",
                    },
                    position={
                        "deal_id": ig_deal,
                        "direction": direction_log,
                        "lots": pos.get("size"),
                        "entry_price": pos.get("level"),
                        "stop_level": pos.get("stop_level"),
                        "limit_level": pos.get("limit_level"),
                        "opened_at": pos.get("created", datetime.now(timezone.utc).isoformat()),
                        "confidence": 0,
                    },
                )
                tracker = self._get_tracker(ig_deal)
                tracker["momentum_tracker"] = MomentumTracker(direction_log, float(pos.get("level") or 0))
                opened_at_str = pos.get("created", "")
                if opened_at_str:
                    await self._async_fetch_and_set_buffer_for(ig_deal, opened_at_str)
                await self.telegram.send_alert(
                    f"Bot restarted. Found open position on IG not in DB.\n"
                    f"Deal: {ig_deal} | {direction_log} @ {pos.get('level')}\n"
                    "Synced and resuming monitoring."
                )

        # DB positions not in IG → closed while offline
        for db_pos in db_positions:
            db_deal = db_pos.get("deal_id")
            if db_deal not in ig_deal_ids:
                logger.warning(f"RECOVERY: DB shows position {db_deal} but IG has none. Closed while offline.")
                self.storage.set_position_closed(db_deal)
                self.storage.log_trade_close(db_deal, {
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                    "result": "CLOSED_WHILE_OFFLINE",
                    "notes": "Position detected closed on bot restart",
                })
                await self.telegram.send_alert(
                    f"Bot restarted. Position {db_deal} was closed while offline.\n"
                    "Check IG for final P&L details."
                )

        # Matching positions — init trackers
        for db_pos in db_positions:
            db_deal = db_pos.get("deal_id")
            if db_deal in ig_deal_ids:
                direction = "LONG" if (db_pos.get("direction") or "BUY").upper() in ("BUY", "LONG") else "SHORT"
                entry = float(db_pos.get("entry_price") or 0)
                tracker = self._get_tracker(db_deal)
                tracker["momentum_tracker"] = MomentumTracker(direction, entry)
                logger.info(f"RECOVERY: Position {db_deal} intact on both. Resuming monitoring.")
                opened_at_str = db_pos.get("opened_at", "")
                if opened_at_str:
                    await self._async_fetch_and_set_buffer_for(db_deal, opened_at_str)

        # Sync legacy singleton for backward compat
        if self._position_trackers:
            first_deal = next(iter(self._position_trackers))
            self.momentum_tracker = self._position_trackers[first_deal]["momentum_tracker"]
            self._position_price_buffer = self._position_trackers[first_deal]["price_buffer"]

        if not ig_positions and not db_positions:
            logger.info("Clean start. No open positions.")
            await self.telegram.send_alert(
                f"Bot started. Scanning mode active.\n"
                f"Mode: {TRADING_MODE.upper()}"
            )
        elif ig_positions:
            dirs = [("LONG" if (p.get("direction") or "BUY").upper() in ("BUY", "LONG") else "SHORT") for p in ig_positions]
            await self.telegram.send_alert(
                f"Bot restarted. {len(ig_positions)} position(s) intact: {', '.join(dirs)}.\n"
                "Resuming monitoring."
            )

    # ============================================================
    # DASHBOARD INTEGRATION
    # ============================================================

    def _reload_overrides(self):
        """Hot-reload dashboard_overrides.json at the top of each main cycle."""
        try:
            if not self._overrides_path.exists():
                return
            data = json.loads(self._overrides_path.read_text())
            # Hot-reload keys only
            import config.settings as S
            if "MIN_CONFIDENCE" in data:
                S.MIN_CONFIDENCE = int(data["MIN_CONFIDENCE"])
            if "MIN_CONFIDENCE_SHORT" in data:
                S.MIN_CONFIDENCE_SHORT = int(data["MIN_CONFIDENCE_SHORT"])
            if "AI_COOLDOWN_MINUTES" in data:
                S.AI_COOLDOWN_MINUTES = int(data["AI_COOLDOWN_MINUTES"])
            if "SCAN_INTERVAL_SECONDS" in data:
                S.SCAN_INTERVAL_SECONDS = int(data["SCAN_INTERVAL_SECONDS"])
            if "scanning_paused" in data:
                self.scanning_paused = bool(data["scanning_paused"])
            if "MAX_MARGIN_PERCENT" in data:
                S.MAX_MARGIN_PERCENT = float(data["MAX_MARGIN_PERCENT"])
            if "MAX_PORTFOLIO_RISK_PERCENT" in data:
                S.MAX_PORTFOLIO_RISK_PERCENT = float(data["MAX_PORTFOLIO_RISK_PERCENT"])
            if "RISK_PERCENT" in data:
                S.RISK_PERCENT = float(data["RISK_PERCENT"])
            if "MAX_RISK_PERCENT" in data:
                S.MAX_RISK_PERCENT = float(data["MAX_RISK_PERCENT"])
        except Exception as e:
            logger.debug(f"_reload_overrides skipped: {e}")

    def _write_state(self, session_name: str | None = None, phase: str | None = None, force: bool = False):
        """Write bot_state.json for the dashboard to read. Throttled to every 10s unless force=True."""
        try:
            if session_name:
                self._current_session = session_name
            now_mono = time.monotonic()
            if not force and (now_mono - self._last_state_write) < 10.0:
                return
            self._last_state_write = now_mono
            uptime_secs = int((datetime.now(timezone.utc) - self._started_at).total_seconds())
            h, rem = divmod(uptime_secs, 3600)
            m = rem // 60
            state = {
                "session":        self._current_session or "—",
                "phase":          phase or (
                    "MONITORING" if self.storage.get_open_positions_count() > 0
                    else "COOLDOWN" if self.storage.is_ai_on_cooldown(AI_COOLDOWN_MINUTES)
                    else "SCANNING"
                ),
                "scanning_paused": self.scanning_paused,
                "last_scan":      self._last_scan_time.isoformat() if self._last_scan_time else None,
                "next_scan_at":   self._next_scan_at.isoformat() if self._next_scan_at else None,
                "current_price":  self._current_price,
                "uptime":         f"{h}h {m}m",
                "started_at":     self._started_at.isoformat(),
                "last_scan_detail": self._last_scan_detail,
            }
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(self._state_path)
        except Exception as e:
            logger.debug(f"_write_state failed: {e}")

    def _check_force_scan_trigger(self) -> bool:
        """Return True if dashboard requested a force scan (file or flag)."""
        try:
            if self._clear_cd_path.exists():
                self._clear_cd_path.unlink()
                self.storage.clear_ai_cooldown()
                logger.info("Dashboard clear-cooldown trigger: cooldown cleared.")
        except Exception:
            pass
        # Check file (direct, e.g. SIGUSR1 path) or flag (set by poll task)
        found = False
        try:
            if self._trigger_path.exists():
                self._trigger_path.unlink()
                found = True
        except Exception:
            pass
        if self._dashboard_force_scan:
            self._dashboard_force_scan = False
            found = True
        if found:
            logger.info("Dashboard force-scan trigger detected.")
        return found

    def _write_force_open_pending(self, setup: dict, session: dict, current_price: float):
        """Write pending force-open opportunity so dashboard can show Force Open button."""
        try:
            pending = {
                "setup_type": setup.get("type"),
                "direction": setup.get("direction"),
                "entry": setup.get("entry", current_price),
                "sl": setup.get("sl"),
                "tp": setup.get("tp"),
                "session": session.get("name", "unknown"),
                "confidence": 100,
                "reasoning": setup.get("reasoning", "")[:300],
                "ai_analysis": setup.get("reasoning", "")[:300],
                "timestamp": datetime.now().isoformat(),
            }
            self._force_open_pending_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._force_open_pending_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(pending))
            tmp.replace(self._force_open_pending_path)
            logger.info(f"Force-open pending: {setup.get('direction')} {setup.get('type')} @ {setup.get('entry')}")
        except Exception as e:
            logger.warning(f"_write_force_open_pending failed: {e}")

    async def _check_force_open_trigger(self):
        """Execute force-open trade if dashboard user clicked Force Open button."""
        try:
            if not self._force_open_trigger_path.exists():
                return
            data = json.loads(self._force_open_trigger_path.read_text())
            self._force_open_trigger_path.unlink(missing_ok=True)
            # Remove pending file too — trade is being executed
            self._force_open_pending_path.unlink(missing_ok=True)

            # Compute real lot size from current balance
            balance_info = await asyncio.get_event_loop().run_in_executor(None, self.ig.get_account_info)
            balance = balance_info.get("balance", 0) if balance_info else 0
            if balance > 0:
                entry = float(data.get("entry", 0))
                sl = float(data.get("sl", 0))
                sl_dist = abs(entry - sl) if sl else DEFAULT_SL_DISTANCE
                data["lots"] = self.risk.get_safe_lot_size(balance, entry, sl_distance=sl_dist)
            else:
                data["lots"] = 0.01

            logger.info(f"Force-open trigger received: {data.get('direction')} {data.get('setup_type')} lots={data.get('lots')}")
            await self.telegram.send_alert(
                f"USER FORCE-OPEN: {data.get('direction')} {data.get('setup_type')} @ {data.get('entry')} | lots={data.get('lots')}"
            )
            await self._on_trade_confirm(data)
        except Exception as e:
            logger.warning(f"_check_force_open_trigger failed: {e}")

    # ============================================================
    # MAIN CYCLE
    # ============================================================

    async def _main_cycle(self):
        """Dispatches to scanning or monitoring based on position state."""
        try:
            self._reload_overrides()
            await self._check_force_open_trigger()

            if not self.ig.ensure_connected():
                self._ig_fail_count = getattr(self, '_ig_fail_count', 0) + 1
                _backoff = min(60 * (2 ** (self._ig_fail_count - 1)), 300)
                logger.warning(f"IG reconnection failed (attempt {self._ig_fail_count}). Sleeping {_backoff}s.")
                if self._ig_fail_count == 1 or self._ig_fail_count % 5 == 0:
                    await self.telegram.send_alert(
                        f"⚠️ IG API unreachable (attempt {self._ig_fail_count}). "
                        f"Next retry in {_backoff}s."
                    )
                await asyncio.sleep(_backoff)
                return

            if getattr(self, '_ig_fail_count', 0) > 0:
                await self.telegram.send_alert("✅ IG API reconnected. Resuming normal operation.")
                self._ig_fail_count = 0

            all_positions = self.storage.get_all_position_states()
            open_count = len(all_positions)

            if open_count > 0:
                # Position existence check: single IG API call for all positions
                await self._check_all_positions_exist(all_positions)

                # Monitor each position
                for pos_state in all_positions:
                    await self._monitoring_cycle(pos_state)

                # If below position cap: also scan for new entries in background
                if open_count < MAX_OPEN_POSITIONS and not self._concurrent_scan_running:
                    now_utc = datetime.now(timezone.utc)
                    scan_due = (
                        self._last_concurrent_scan_at is None
                        or (now_utc - self._last_concurrent_scan_at).total_seconds() >= SCAN_INTERVAL_SECONDS
                    )
                    if scan_due:
                        self._concurrent_scan_running = True
                        asyncio.create_task(self._run_concurrent_scan())
                await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
            else:
                interval = await self._scanning_cycle()
                self._next_scan_at = datetime.now(timezone.utc) + timedelta(seconds=interval)
                self._write_state()
                try:
                    await asyncio.wait_for(self._force_scan_event.wait(), timeout=interval)
                    self._force_scan_event.clear()
                except asyncio.TimeoutError:
                    pass
                self._next_scan_at = None

        except Exception as e:
            logger.error(f"Main cycle error: {e}", exc_info=True)
            try:
                await self.telegram.send_alert(f"Monitor error: {str(e)[:200]}")
            except Exception:
                pass
            await asyncio.sleep(30)

    async def _check_all_positions_exist(self, all_positions: list[dict]):
        """Check position existence on IG for all tracked positions.
        Single API call, runs once per N monitoring cycles.
        """
        # Use the first position's tracker for the check counter
        if not all_positions:
            return
        first_deal = all_positions[0].get("deal_id", "")
        tracker = self._get_tracker(first_deal)
        tracker["check_counter"] += 1
        if tracker["check_counter"] < POSITION_CHECK_EVERY_N_CYCLES:
            return
        tracker["check_counter"] = 0

        live_positions = await asyncio.get_event_loop().run_in_executor(
            None, self.ig.get_open_positions
        )

        if live_positions is POSITIONS_API_ERROR:
            logger.warning("IG API error checking positions. Skipping cycle.")
            await self.telegram.send_alert(
                "WARNING: IG API error while checking positions. Will retry."
            )
            # Reset all empty counts
            for pos in all_positions:
                t = self._get_tracker(pos.get("deal_id", ""))
                t["empty_count"] = 0
            return

        live_deal_ids = {p.get("deal_id") for p in live_positions}

        for pos_state in all_positions:
            deal_id = pos_state.get("deal_id")
            t = self._get_tracker(deal_id)
            if deal_id not in live_deal_ids:
                t["empty_count"] += 1
                if t["empty_count"] < SAFETY_CONSECUTIVE_EMPTY:
                    logger.warning(
                        f"Position {deal_id} not found on IG ({t['empty_count']}/{SAFETY_CONSECUTIVE_EMPTY}). "
                        f"Waiting for confirmation."
                    )
                else:
                    t["empty_count"] = 0
                    await self._handle_position_closed(pos_state)
            else:
                t["empty_count"] = 0

    # ============================================================
    # SCANNING MODE
    # ============================================================

    @staticmethod
    def _5m_aligns_with_15m(setup_5m: dict, tf_15m: dict) -> bool:
        """Check that 5M setup aligns with 15M structure (lightweight guard).

        LONG:  15M RSI < 65 (not overbought) AND price within 300pts of 15M BB mid or lower
        SHORT: 15M RSI > 35 (not oversold)  AND price within 300pts of 15M BB upper
        If 15M data missing → pass through (safe default).
        """
        if not tf_15m:
            return True

        rsi_15m = tf_15m.get("rsi")
        direction = setup_5m.get("direction", "")
        price = setup_5m.get("entry") or tf_15m.get("price")

        if direction == "LONG":
            if rsi_15m is not None and rsi_15m > 65:
                return False
            bb_mid = tf_15m.get("bollinger_mid")
            bb_lower = tf_15m.get("bollinger_lower")
            ref = bb_lower if bb_lower else bb_mid
            if ref and price and abs(price - ref) > 300:
                return False
        elif direction == "SHORT":
            if rsi_15m is not None and rsi_15m < 35:
                return False
            bb_upper = tf_15m.get("bollinger_upper")
            if bb_upper and price and abs(price - bb_upper) > 300:
                return False

        return True

    async def _scanning_cycle(self) -> int:
        """
        Entry scanning cycle. Returns the sleep interval to use.

        Returns: seconds to sleep before next cycle.
        """
        session = get_current_session()
        logger.info(f"Scan cycle | Session: {session['name']} | Active: {session['active']}")
        self._last_scan_time = datetime.now(timezone.utc)
        self._write_state(session_name=session["name"])

        # --- Force scan from dashboard ---
        force_scan = self._check_force_scan_trigger()

        # --- Weekend / No-trade day check ---
        no_trade, reason = is_no_trade_day()
        if no_trade:
            logger.info(f"No-trade: {reason}")
            return OFFHOURS_INTERVAL_SECONDS

        # --- Off-hours: heartbeat only (unless force scan requested) ---
        if not session["active"] and not force_scan:
            secs = _secs_to_next_session()
            sleep_for = max(30, min(secs, OFFHOURS_INTERVAL_SECONDS))
            logger.debug(f"Off-hours — next session in {secs//60}m {secs%60}s. Sleeping {sleep_for}s.")
            return sleep_for

        # --- System paused (force scan overrides pause too) ---
        account = self.storage.get_account_state()
        if (not account.get("system_active", True) or self.scanning_paused) and not force_scan:
            logger.info("Scanning paused.")
            return SCAN_INTERVAL_SECONDS

        # --- Fetch current price (1 API call) ---
        market = await asyncio.get_event_loop().run_in_executor(
            None, self.ig.get_market_info
        )
        if not market:
            logger.warning("Failed to get market info")
            return SCAN_INTERVAL_SECONDS

        if market.get("market_status") != "TRADEABLE":
            logger.info(f"Market not tradeable: {market.get('market_status')}")
            return SCAN_INTERVAL_SECONDS

        current_price = market.get("bid", 0)
        self._current_price = current_price

        # --- Fetch candles ---
        # Cold start (full fetch, many pages): ALL sequential to stay within 28 req/min
        # Warm (delta, 1 page each): 5M+15M parallel, Daily time-gated (usually skipped)
        cold_start = not self.ig._cache_full_fetch_done.get("MINUTE_15", False)
        loop = asyncio.get_event_loop()
        if cold_start:
            # Sequential: 5M (~5s) → 15M (~11s) → 4H → Daily (~13s). Token bucket handles pacing.
            candles_5m = await loop.run_in_executor(
                None, lambda: self.ig.get_prices("MINUTE_5", MINUTE_5_CANDLES)
            )
            candles_15m = await loop.run_in_executor(
                None, lambda: self.ig.get_prices("MINUTE_15", PRE_SCREEN_CANDLES)
            )
            candles_4h = await loop.run_in_executor(
                None, lambda: self.ig.get_prices("HOUR_4", AI_ESCALATION_CANDLES)
            )
            candles_daily = await loop.run_in_executor(
                None, lambda: self.ig.get_prices("DAY", DAILY_EMA200_CANDLES)
            )
        else:
            # Delta fetches: 1 page each, safe to parallel. Daily usually time-gated.
            candles_15m, candles_5m, candles_4h = await asyncio.gather(
                loop.run_in_executor(
                    None, lambda: self.ig.get_prices("MINUTE_15", PRE_SCREEN_CANDLES)
                ),
                loop.run_in_executor(
                    None, lambda: self.ig.get_prices("MINUTE_5", MINUTE_5_CANDLES)
                ),
                loop.run_in_executor(
                    None, lambda: self.ig.get_prices("HOUR_4", AI_ESCALATION_CANDLES)
                ),
            )
            candles_daily = await loop.run_in_executor(
                None, lambda: self.ig.get_prices("DAY", DAILY_EMA200_CANDLES)
            )
        if not candles_15m:
            logger.warning("Failed to fetch 15M candles")
            return SCAN_INTERVAL_SECONDS

        tf_15m = analyze_timeframe(candles_15m)
        tf_daily = analyze_timeframe(candles_daily) if candles_daily else {}
        tf_5m = analyze_timeframe(candles_5m) if candles_5m else {}
        tf_4h = analyze_timeframe(candles_4h) if candles_4h else {}

        # Crash day detection logging
        daily_range = tf_daily.get("high", 0) - tf_daily.get("low", 0) if tf_daily else 0
        if daily_range > EXTREME_DAY_RANGE_PTS:
            logger.warning(f"EXTREME DAY: intraday range={daily_range:.0f}pts")
        if not candles_4h:
            logger.warning("Failed to fetch 4H candles — pre-screen will degrade gracefully.")

        # --- Local pre-screen — BIDIRECTIONAL (zero AI cost) ---
        # Pre-compute session context for session-specific setups (tokyo_gap_fill, london_orb).
        # Done before detect_setup so those setups can use gap_pts and Asia range.
        sess_ctx_pre = compute_session_context(candles_15m, candles_daily)

        # Run detect_setup() for BOTH directions independently.
        # 4H data now available at pre-screen for extreme_oversold_reversal and other setups.
        setup_long = detect_setup(
            tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_15m,
            exclude_direction="SHORT", session_context=sess_ctx_pre,
        )
        setup_short = detect_setup(
            tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_15m,
            exclude_direction="LONG", session_context=sess_ctx_pre,
        )

        # --- 5M fallback: for each direction that didn't find on 15M, try 5M ---
        if not setup_long["found"] and tf_5m:
            setup_5m_long = detect_setup(tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_5m, exclude_direction="SHORT", session_context=sess_ctx_pre)
            if setup_5m_long["found"] and self._5m_aligns_with_15m(setup_5m_long, tf_15m):
                setup_long = setup_5m_long
                setup_long["type"] += "_5m"
                setup_long["_entry_tf"] = "5m"
                logger.info(f"5M fallback: LONG {setup_long['type']} detected")

        if not setup_short["found"] and tf_5m:
            setup_5m_short = detect_setup(tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_5m, exclude_direction="LONG", session_context=sess_ctx_pre)
            if setup_5m_short["found"] and self._5m_aligns_with_15m(setup_5m_short, tf_15m):
                setup_short = setup_5m_short
                setup_short["type"] += "_5m"
                setup_short["_entry_tf"] = "5m"
                logger.info(f"5M fallback: SHORT {setup_short['type']} detected")

        # Neither direction found a setup
        if not setup_long["found"] and not setup_short["found"]:
            reasoning = setup_long.get("reasoning", "") or setup_short.get("reasoning", "")
            logger.info(f"Pre-screen: no setup (both dirs). {reasoning[:200]}")
            self._last_scan_detail = {"outcome": "no_setup", "price": current_price, "details": reasoning[:200]}
            self.storage.save_scan({
                "timestamp": datetime.now().isoformat(), "session": session["name"],
                "price": current_price, "indicators": {}, "market_context": {},
                "analysis": {"setup_found": False, "reasoning": reasoning[:120]},
                "setup_found": False, "confidence": None, "action_taken": "no_setup", "api_cost": 0,
            })
            return SCAN_INTERVAL_SECONDS

        # Log both directions for debugging
        if setup_long["found"]:
            logger.info(f"Pre-screen LONG: {setup_long['type']} ({setup_long.get('_entry_tf', '15m')})")
        if setup_short["found"]:
            logger.info(f"Pre-screen SHORT: {setup_short['type']} ({setup_short.get('_entry_tf', '15m')})")

        # Pick primary setup for now — confidence scoring below will finalize
        # If only one direction found, that's the primary
        if setup_long["found"] and not setup_short["found"]:
            setup = setup_long
        elif setup_short["found"] and not setup_long["found"]:
            setup = setup_short
        else:
            # Both found — pick primary after confidence scoring (use LONG as placeholder)
            setup = setup_long

        entry_timeframe = setup.get("_entry_tf", "15m")
        prescreen_direction = setup["direction"]
        logger.info(f"Pre-screen: {prescreen_direction} {setup['type']} ({entry_timeframe}) detected. Checking local confidence...")

        # Write force-open pending for extreme setups (local_score=100) so dashboard shows button
        if setup.get("local_score") == 100:
            self._write_force_open_pending(setup, session, current_price)

        # No AI cooldown — subscription is $0/call, always escalate if setup found
        # tf_4h and tf_daily already set from pre-screen fetch above

        # --- Local confidence score — BIDIRECTIONAL ---
        web_research = {"timestamp": datetime.now().isoformat()}
        try:
            web_research = self.researcher.research()
        except Exception as e:
            logger.warning(f"Web research failed: {e}")

        conf_long = None
        conf_short = None
        secondary_setup = None  # Will hold the non-primary direction's context

        if setup_long["found"]:
            conf_long = compute_confidence(
                direction="LONG",
                tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_15m,
                upcoming_events=web_research.get("economic_calendar", []),
                web_research=web_research,
                setup_type=setup_long.get("type"),
            )
            logger.info(f"Local confidence LONG: {conf_long['score']}%")
            if conf_long["score"] < 60:
                wc = conf_long.get("weighted_criteria", {})
                fails = [k for k, v in wc.items() if not v]
                rr = conf_long.get("estimated_rr", 0)
                rrf = conf_long.get("rr_factor", 1)
                logger.info(f"  LONG drags: {', '.join(fails) if fails else 'none'} | R:R est={rr:.2f} factor={rrf}")

        if setup_short["found"]:
            conf_short = compute_confidence(
                direction="SHORT",
                tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_15m,
                upcoming_events=web_research.get("economic_calendar", []),
                web_research=web_research,
                setup_type=setup_short.get("type"),
            )
            logger.info(f"Local confidence SHORT: {conf_short['score']}%")
            if conf_short["score"] < 60:
                wc = conf_short.get("weighted_criteria", {})
                fails = [k for k, v in wc.items() if not v]
                rr = conf_short.get("estimated_rr", 0)
                rrf = conf_short.get("rr_factor", 1)
                logger.info(f"  SHORT drags: {', '.join(fails) if fails else 'none'} | R:R est={rr:.2f} factor={rrf}")

        # --- Contradictory signal gate ---
        # If BOTH directions score high with tiny gap → market is ambiguous, no real edge.
        # e.g. LONG 94% + SHORT 94% means indicators are firing equally for both = skip.
        if conf_long and conf_short:
            s_long = conf_long["score"]
            s_short = conf_short["score"]
            if (s_long >= CONTRADICTORY_SIGNAL_MIN_SCORE
                    and s_short >= CONTRADICTORY_SIGNAL_MIN_SCORE
                    and abs(s_long - s_short) <= CONTRADICTORY_SIGNAL_MAX_GAP):
                logger.info(
                    f"Contradictory signals: LONG {s_long}% vs SHORT {s_short}% "
                    f"(gap={abs(s_long-s_short)}pts ≤ {CONTRADICTORY_SIGNAL_MAX_GAP}). "
                    f"No edge — skipping."
                )
                self._last_scan_detail = {"outcome": "contradictory_signals", "direction": "NONE",
                                          "confidence": max(s_long, s_short), "price": current_price}
                return SCAN_INTERVAL_SECONDS

        # --- Pick primary direction based on confidence ---
        # Rules:
        #   - If only one direction found: that's primary, no secondary
        #   - If both found: higher confidence = primary, other = secondary
        #   - If both found and scores equal: LONG takes priority (lower threshold)
        if conf_long and conf_short:
            if conf_short["score"] > conf_long["score"]:
                setup = setup_short
                local_conf = conf_short
                secondary_setup = {
                    "direction": "LONG",
                    "type": setup_long.get("type"),
                    "confidence": conf_long["score"],
                    "reasoning": setup_long.get("reasoning", "")[:200],
                    "criteria": conf_long.get("criteria", {}),
                    "passed_criteria": conf_long.get("passed_criteria", 0),
                }
            else:
                setup = setup_long
                local_conf = conf_long
                secondary_setup = {
                    "direction": "SHORT",
                    "type": setup_short.get("type"),
                    "confidence": conf_short["score"],
                    "reasoning": setup_short.get("reasoning", "")[:200],
                    "criteria": conf_short.get("criteria", {}),
                    "passed_criteria": conf_short.get("passed_criteria", 0),
                }
            entry_timeframe = setup.get("_entry_tf", "15m")
            prescreen_direction = setup["direction"]
            logger.info(
                f"Bidirectional: primary={prescreen_direction} {local_conf['score']}%, "
                f"secondary={secondary_setup['direction']} {secondary_setup['confidence']}%"
            )
        elif conf_long:
            local_conf = conf_long
            setup = setup_long
        elif conf_short:
            local_conf = conf_short
            setup = setup_short
        else:
            # Should not happen (at least one setup found), but safety fallback
            local_conf = compute_confidence(
                direction=prescreen_direction,
                tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_15m,
                upcoming_events=web_research.get("economic_calendar", []),
                web_research=web_research,
                setup_type=setup.get("type"),
            )

        entry_timeframe = setup.get("_entry_tf", "15m")
        prescreen_direction = setup["direction"]
        logger.info(f"Primary: {prescreen_direction} {setup['type']} ({entry_timeframe}) conf={local_conf['score']}%")

        # --- Hard blocks: C7/C8 are non-negotiable regardless of score or AI opinion ---
        criteria = local_conf.get("criteria", {})
        if not criteria.get("no_event_1hr", True):
            logger.info("Hard block: HIGH-impact event within 60min. No AI evaluation.")
            self._last_scan_detail = {"outcome": "event_block", "direction": prescreen_direction, "confidence": local_conf["score"], "price": current_price}
            self.storage.save_scan({
                "timestamp": datetime.now().isoformat(), "session": session["name"],
                "price": current_price, "indicators": {}, "market_context": {},
                "analysis": {"setup_found": False, "reasoning": "[Hard block] High-impact event within 60min"},
                "setup_found": False, "confidence": local_conf["score"], "action_taken": f"event_block_{prescreen_direction.lower()}", "api_cost": 0,
            })
            return SCAN_INTERVAL_SECONDS
        if not criteria.get("no_friday_monthend", True):
            logger.info("Hard block: Friday/month-end blackout. No AI evaluation.")
            self._last_scan_detail = {"outcome": "friday_block", "direction": prescreen_direction, "confidence": local_conf["score"], "price": current_price}
            self.storage.save_scan({
                "timestamp": datetime.now().isoformat(), "session": session["name"],
                "price": current_price, "indicators": {}, "market_context": {},
                "analysis": {"setup_found": False, "reasoning": "[Hard block] Friday/month-end blackout"},
                "setup_found": False, "confidence": local_conf["score"], "action_taken": f"friday_block_{prescreen_direction.lower()}", "api_cost": 0,
            })
            return SCAN_INTERVAL_SECONDS

        # --- Local confidence floor at HAIKU_MIN_SCORE (60%) ---
        # Filters true technical junk before AI evaluation. Sonnet handles borderline cases.
        if local_conf["score"] < HAIKU_MIN_SCORE:
            logger.info(
                f"Local score {local_conf['score']}% below floor ({HAIKU_MIN_SCORE}%). "
                f"Skipping (true technical junk)."
            )
            self._last_scan_detail = {"outcome": "low_conf", "direction": prescreen_direction, "confidence": local_conf["score"], "price": current_price, "setup_type": setup.get("type")}
            self.storage.save_scan({
                "timestamp": datetime.now().isoformat(), "session": session["name"],
                "price": current_price, "indicators": {}, "market_context": {},
                "analysis": {"setup_found": False, "reasoning": f"[Low confidence] {local_conf['score']}% < {HAIKU_MIN_SCORE}% floor"},
                "setup_found": False, "confidence": local_conf["score"], "action_taken": f"low_conf_{prescreen_direction.lower()}", "api_cost": 0,
            })
            return SCAN_INTERVAL_SECONDS

        failed_criteria = [k for k, v in criteria.items() if not v
                           and k not in ("no_event_1hr", "no_friday_monthend")]
        live_edge = self.storage.get_ai_context_block()
        indicators = {"m15": tf_15m, "h4": tf_4h, "daily": tf_daily}
        indicators["pivots"] = setup.get("indicators_snapshot", {}).get("pivots", {})
        if tf_5m:
            indicators["m5"] = tf_5m

        # ── Session context + order flow → injected into indicators_snapshot ──
        sess_ctx = sess_ctx_pre  # already computed above before detect_setup
        tick_density = self.ig.get_tick_density()
        snap = setup.get("indicators_snapshot", {})
        snap.update({
            "session_open":      sess_ctx.get("session_open"),
            "asia_high":         sess_ctx.get("asia_high"),
            "asia_low":          sess_ctx.get("asia_low"),
            "pdh_daily":         sess_ctx.get("pdh"),
            "pdl_daily":         sess_ctx.get("pdl"),
            "prev_week_high":    sess_ctx.get("prev_week_high"),
            "prev_week_low":     sess_ctx.get("prev_week_low"),
            "gap_pts":           sess_ctx.get("gap_pts"),
            "tick_density_signal": tick_density.get("signal"),
            "tick_density_latest": tick_density.get("latest"),
        })
        indicators["indicators_snapshot"] = snap

        recent_trades_ctx = self.storage.get_recent_trades(10)

        # Build open-position context for AI — lets Sonnet know existing exposure and daily P&L
        _open_pos_list = self.storage.get_open_positions()
        _today_str = display_now().strftime("%Y-%m-%d")
        _daily_pnl = sum(
            (t.get("pnl") or 0)
            for t in recent_trades_ctx
            if str(t.get("opened_at", ""))[:10] == _today_str
        )
        open_positions_ctx = {
            "count": len(_open_pos_list),
            "directions": [p.get("direction", "?") for p in _open_pos_list],
            "daily_pnl": _daily_pnl,
        }

        logger.info(
            f"Volume signals → 5M: {tf_5m.get('volume_signal')}({tf_5m.get('volume_ratio')}) "
            f"| 15M: {tf_15m.get('volume_signal')}({tf_15m.get('volume_ratio')}) "
            f"| Daily: {tf_daily.get('volume_signal')}({tf_daily.get('volume_ratio')})"
        )

        local_score = local_conf.get("score", 0)

        # --- Sonnet first, then Opus sequential with full context ---
        logger.info(f"Local score {local_score}%. Escalating to Sonnet...")

        recent_scans = self.storage.get_recent_scans(5)
        market_context = self.storage.get_market_context()
        market_context["prescreen_setup"] = {
            "type": setup.get("type"),
            "reasoning": setup.get("reasoning"),
            "session": session.get("name"),
        }
        market_context["prescreen_setup_type"] = setup.get("type", "")
        market_context["prescreen_reasoning"]  = setup.get("reasoning", "")
        market_context["session_name"]         = session.get("name", "")
        market_context["entry_timeframe"]      = entry_timeframe

        # Secondary setup context for bidirectional AI awareness
        if secondary_setup:
            market_context["secondary_setup"] = secondary_setup

        loop = asyncio.get_event_loop()

        # Launch Sonnet via executor (non-blocking)
        sonnet_future = loop.run_in_executor(
            None,
            lambda: self.analyzer.scan_with_sonnet(
                indicators=indicators,
                recent_scans=recent_scans,
                market_context=market_context,
                web_research=web_research,
                prescreen_direction=prescreen_direction,
                local_confidence=local_conf,
                live_edge_block=live_edge,
                failed_criteria=failed_criteria,
                recent_trades=recent_trades_ctx,
                open_positions_context=open_positions_ctx,
            ),
        )

        # Await Sonnet result
        sonnet_result = await sonnet_future
        final_result = sonnet_result
        final_confidence = final_result.get("confidence", 0)
        logger.info(f"AI: found={final_result.get('setup_found')}, confidence={final_confidence}%")
        _log_reasoning = final_result.get("reasoning_short") or final_result.get("reasoning", "N/A")[:500]
        logger.info(f"AI reasoning: {_log_reasoning}")

        # --- Determine final outcome before saving ---
        direction = final_result.get("direction") or prescreen_direction
        min_conf = MIN_CONFIDENCE_SHORT if direction == "SHORT" else MIN_CONFIDENCE
        ai_confirmed = final_result.get("setup_found", False) and final_confidence >= min_conf

        action_taken = f"pending_{direction.lower()}" if ai_confirmed else f"ai_rejected_{direction.lower()}"

        # --- Save scan ---
        scan_cost = final_result.get("_cost", 0)
        self.storage.save_scan({
            "timestamp": datetime.now().isoformat(),
            "session": session["name"],
            "price": current_price,
            "indicators": indicators,
            "market_context": web_research,
            "analysis": final_result,
            "setup_found": final_result.get("setup_found", False),
            "confidence": final_confidence,
            "action_taken": action_taken,
            "api_cost": scan_cost,
        })

        if not ai_confirmed:
            logger.info(
                f"Setup not confirmed by AI. "
                f"Found={final_result.get('setup_found')}, "
                f"Confidence={final_confidence}% (need {min_conf}%)"
            )
            self._last_scan_detail = {"outcome": "ai_rejected", "direction": direction, "confidence": final_confidence, "price": current_price, "setup_type": setup.get("type")}

            # --- Opus evaluates OPPOSITE direction as swing trade ---
            # Gate: Sonnet must have < 50% confidence in the primary direction.
            # If Sonnet scored >= 50% it had real conviction in the primary — Opus
            # flipping to the opposite would be contradictory. Below 50% means
            # Sonnet had no real setup, so the opposite direction is fair game.
            if final_confidence >= 50:
                logger.info(
                    f"Sonnet partial conviction (conf {final_confidence}% >= 50%). "
                    f"Skipping Opus opposite eval — Sonnet leaning primary direction."
                )
            else:
                _opposite_dir = "SHORT" if direction == "LONG" else "LONG"
                _opposite_conf = conf_short if direction == "LONG" else conf_long
                _opposite_setup = setup_short if direction == "LONG" else setup_long
                _sonnet_conf_score = final_result.get("confidence", 0)
                _counter_signal = final_result.get("counter_signal")  # "LONG"/"SHORT" set by Sonnet

                # Normal gate: pre-detected opposite setup with local conf >= 60%
                # Also fires when Sonnet returned confidence=0 (parse error fallback) —
                # don't penalise a valid pre-detected opposite setup due to a parse failure.
                _sonnet_parse_error = _sonnet_conf_score == 0 and not final_result.get("found", False)
                _normal_gate = (
                    _opposite_conf is not None
                    and _opposite_conf.get("score", 0) >= 60
                    and _opposite_setup.get("found", False)
                    and (_sonnet_conf_score >= 30 or _sonnet_parse_error)
                )
                # Counter-signal gate: Sonnet explicitly identified an opposite-direction opportunity
                # (e.g., swept_low = liquidity grab → bullish reversal while evaluating SHORT)
                # Does NOT require pre-detected opposite setup — that's the whole point of this gate.
                _counter_gate = (
                    _counter_signal == _opposite_dir
                    and _sonnet_conf_score <= 45  # strong rejection of primary only
                )

                if _normal_gate or _counter_gate:
                    if _counter_gate and not _normal_gate:
                        logger.info(
                            f"Sonnet counter_signal={_counter_signal} detected "
                            f"(primary rejected at {_sonnet_conf_score}%). "
                            f"Triggering Opus on {_opposite_dir} despite no pre-detected setup."
                        )
                    else:
                        logger.info(
                            f"Sonnet rejected {direction}. Opus evaluating {_opposite_dir} "
                            f"(opposite local conf: {_opposite_conf.get('score')}%)"
                        )
                    sonnet_key_levels = final_result.get("key_levels", {"support": [], "resistance": []})
                    try:
                        opus_result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: self.analyzer.evaluate_opposite(
                                indicators=indicators,
                                opposite_direction=_opposite_dir,
                                opposite_local_conf=_opposite_conf,
                                sonnet_rejection_reasoning=final_result.get("reasoning", ""),
                                sonnet_key_levels=sonnet_key_levels,
                                recent_scans=recent_scans,
                                market_context=market_context,
                                web_research=web_research,
                                recent_trades=recent_trades_ctx,
                                live_edge_block=live_edge,
                                recent_opus_decision=self.storage.get_recent_opus_decision(),
                            ),
                        )

                        if opus_result.get("setup_found") and opus_result.get("direction") == _opposite_dir:
                            opus_conf = opus_result.get("confidence", 0)
                            min_conf_opus = MIN_CONFIDENCE_SHORT if _opposite_dir == "SHORT" else MIN_CONFIDENCE

                            if opus_conf >= min_conf_opus:
                                # Store decision for consistency tracking
                                self.storage.save_opus_decision({
                                    "direction": _opposite_dir,
                                    "viable": True,
                                    "confidence": opus_conf,
                                    "reasoning": opus_result.get("reasoning", "")[:300],
                                    "timestamp": datetime.now().isoformat(),
                                })

                                entry = float(opus_result.get("entry", current_price))
                                sl = float(opus_result.get("stop_loss", 0))
                                tp = float(opus_result.get("take_profit", 0))

                                if not sl or not tp:
                                    logger.warning("Opus opposite eval: missing SL/TP, skipping")
                                else:
                                    sl_distance = abs(entry - sl)
                                    balance_info = await asyncio.get_event_loop().run_in_executor(
                                        None, self.ig.get_account_info
                                    )
                                    balance = balance_info.get("balance", 0) if balance_info else 0
                                    if balance <= 0:
                                        logger.error("Opus opposite: could not get account balance")
                                    else:
                                        lots = self.risk.get_safe_lot_size(balance, current_price, sl_distance=sl_distance)

                                        validation = self.risk.validate_trade(
                                            direction=_opposite_dir,
                                            lots=lots,
                                            entry=entry,
                                            stop_loss=sl,
                                            take_profit=tp,
                                            confidence=opus_conf,
                                            balance=balance,
                                            upcoming_events=web_research.get("economic_calendar", []),
                                            indicators_snapshot=indicators,
                                            setup_type=opus_result.get("setup_type"),
                                        )

                                        if validation["approved"]:
                                            risk_pts = abs(entry - sl)
                                            reward_pts = abs(tp - entry)
                                            effective_risk = risk_pts + SPREAD_ESTIMATE
                                            effective_reward = reward_pts - SPREAD_ESTIMATE
                                            rr_computed = effective_reward / effective_risk if effective_risk > 0 else 0

                                            opus_alert = {
                                                "direction": _opposite_dir,
                                                "entry": entry,
                                                "sl": sl,
                                                "tp": tp,
                                                "lots": lots,
                                                "confidence": opus_conf,
                                                "rr_ratio": rr_computed,
                                                "margin": calculate_margin(lots, entry),
                                                "free_margin": balance - calculate_margin(lots, entry),
                                                "dollar_risk": calculate_profit(lots, risk_pts),
                                                "dollar_reward": calculate_profit(lots, reward_pts),
                                                "setup_type": opus_result.get("setup_type", "opus_opposite"),
                                                "session": session["name"],
                                                "reasoning": opus_result.get("reasoning", ""),
                                                "timestamp": datetime.now().isoformat(),
                                                "local_confidence": (_opposite_conf or {}).get("score"),
                                                "opus_confidence": opus_conf,
                                                "ai_analysis": f"[OPUS OPPOSITE] {opus_result.get('reasoning', '')}",
                                                "indicators_compact": setup.get("indicators_snapshot", {}),
                                                "is_scalp": False,
                                            }

                                            _opus_short = opus_result.get("reasoning_short") or opus_result.get("reasoning", "")[:400]
                                            logger.info(
                                                f"Opus EXECUTING {_opposite_dir} @ {entry:.0f} | "
                                                f"SL={sl:.0f} TP={tp:.0f} RR={rr_computed:.1f} conf={opus_conf}%"
                                            )
                                            logger.info(f"Opus reasoning: {_opus_short}")
                                            await self.telegram.send_trade_alert(opus_alert)
                                            await self._on_trade_confirm(opus_alert)
                                            return 0
                                        else:
                                            logger.info(
                                                f"Opus opposite risk validation failed: "
                                                f"{validation['rejection_reason']}"
                                            )
                            else:
                                _opus_short = opus_result.get("reasoning_short") or opus_result.get("reasoning", "")[:400]
                                logger.info(
                                    f"Opus opposite conf {opus_conf}% < threshold {min_conf_opus}% — {_opus_short}"
                                )
                                self.storage.save_opus_decision({
                                    "direction": _opposite_dir,
                                    "viable": False,
                                    "confidence": opus_conf,
                                    "reasoning": opus_result.get("reasoning", "")[:300],
                                    "timestamp": datetime.now().isoformat(),
                                })
                        else:
                            _opus_short = opus_result.get("reasoning_short") or opus_result.get("reasoning", "")[:400]
                            logger.info(
                                f"Opus opposite: no {_opposite_dir} setup — {_opus_short}"
                            )
                            self.storage.save_opus_decision({
                                "direction": _opposite_dir,
                                "viable": False,
                                "confidence": 0,
                                "reasoning": opus_result.get("reasoning", "")[:300],
                                "timestamp": datetime.now().isoformat(),
                            })
                    except Exception as e:
                        logger.warning(f"Opus opposite evaluation failed: {e}")
                else:
                    logger.info(
                        f"Opus opposite skipped: no gate passed "
                        f"(opposite_found={_opposite_setup.get('found', False)}, "
                        f"opposite_conf={_opposite_conf.get('score', 0) if _opposite_conf else 'N/A'}%, "
                        f"sonnet_conf={_sonnet_conf_score}%, counter_signal={_counter_signal})"
                    )

            # --- Force Open: 100% local confidence, AI rejected → user decides ---
            if local_score >= 100:
                criteria_detail = f"{local_conf.get('passed_criteria', 12)}/{local_conf.get('total_criteria', 12)}"
                logger.info(
                    f"Force open offered: 100% local ({criteria_detail}), AI rejected. "
                    f"Sending to Telegram for user decision."
                )
                fo_entry = current_price
                fo_sl = setup.get("sl", fo_entry - (150 if direction == "LONG" else -150))
                fo_tp = setup.get("tp", fo_entry + (400 if direction == "LONG" else -400))
                fo_sl_dist = abs(fo_entry - fo_sl)

                # Compute lots (same as regular trade path)
                fo_balance_info = self.ig.get_account_info()
                fo_balance = fo_balance_info.get("balance", 0) if fo_balance_info else 0
                fo_lots = self.risk.get_safe_lot_size(fo_balance, fo_entry, sl_distance=fo_sl_dist) if fo_balance > 0 else CONTRACT_SIZE

                force_alert = {
                    "direction": direction,
                    "setup_type": setup.get("type", "unknown"),
                    "entry": fo_entry,
                    "sl": fo_sl,
                    "tp": fo_tp,
                    "lots": fo_lots,
                    "confidence": local_score,
                    "session": session["name"],
                    "reasoning": f"100% local confidence ({criteria_detail}). AI rejected.",
                    "ai_reasoning": final_result.get("reasoning", "")[:300],
                    "force_open": True,
                    "timestamp": datetime.now().isoformat(),
                    "local_confidence": local_score,
                }
                await self.telegram.send_force_open_alert(force_alert)

            # Bidirectional retry removed — Opus evaluates both directions in single call above

            # No cooldown — subscription is $0/call, scan again in 5 min
            return SCAN_INTERVAL_SECONDS

        # --- Risk validation ---
        balance_info = self.ig.get_account_info()
        balance = balance_info.get("balance", 0) if balance_info else 0
        if balance <= 0:
            logger.error("Could not get account balance")
            return SCAN_INTERVAL_SECONDS

        entry = final_result.get("entry", current_price)
        sl = final_result.get("stop_loss", 0)
        tp = final_result.get("take_profit", 0)

        if not sl or not tp:
            logger.error(f"AI returned null SL or TP: sl={sl}, tp={tp}")
            return SCAN_INTERVAL_SECONDS

        sl_distance = abs(entry - sl)
        lots = self.risk.get_safe_lot_size(
            balance, current_price, sl_distance=sl_distance, confidence=final_confidence
        )
        logger.info(f"Lot size: {lots} (balance=${balance:.2f}, SL={sl_distance:.0f}pts)")

        validation = self.risk.validate_trade(
            direction=direction,
            lots=lots,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            confidence=final_confidence,
            balance=balance,
            upcoming_events=web_research.get("economic_calendar", []),
            indicators_snapshot=indicators,
            setup_type=final_result.get("setup_type") or setup.get("type"),
        )

        if not validation["approved"]:
            logger.info(f"Risk validation failed: {validation['rejection_reason']}")
            await self.telegram.send_alert(
                f"Setup found but risk check failed:\n{validation['rejection_reason']}"
            )
            return SCAN_INTERVAL_SECONDS

        # --- Send trade alert to Telegram ---
        risk_pts = abs(entry - sl)
        reward_pts = abs(tp - entry)
        effective_risk = risk_pts + SPREAD_ESTIMATE
        effective_reward = reward_pts - SPREAD_ESTIMATE
        rr = effective_reward / effective_risk if effective_risk > 0 else 0

        margin = calculate_margin(lots, entry)
        free_margin = balance - margin

        trade_alert = {
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "lots": lots,
            "confidence": final_confidence,
            "rr_ratio": rr,
            "margin": margin,
            "free_margin": free_margin,
            "dollar_risk": calculate_profit(lots, risk_pts),
            "dollar_reward": calculate_profit(lots, reward_pts),
            "setup_type": final_result.get("setup_type", "unknown"),
            "session": session["name"],
            "reasoning": final_result.get("reasoning", ""),
            "timestamp": datetime.now().isoformat(),
            "confidence_breakdown": final_result.get("confidence_breakdown", {}),
            "local_confidence": local_conf.get("score"),
            "ai_analysis": final_result.get("reasoning", ""),
            "indicators_compact": setup.get("indicators_snapshot", {}),
        }

        self._last_scan_detail = {"outcome": "trade_alert", "direction": direction, "confidence": final_confidence, "price": current_price, "setup_type": setup.get("type")}

        # Send alert to Telegram with CONFIRM/REJECT buttons — never auto-execute.
        logger.info(
            f"Sonnet signal: {direction} @ {entry:.0f}, "
            f"confidence={final_confidence}%, SL={sl:.0f}, TP={tp:.0f} — awaiting user confirmation"
        )
        await self.telegram.send_trade_alert(trade_alert)

        return SCAN_INTERVAL_SECONDS  # Wait for user confirmation via Telegram

    # ============================================================
    # MONITORING MODE
    # ============================================================

    async def _monitoring_cycle(self, pos_state: dict):
        """Position monitoring — runs every 2s when a trade is open.
        Position existence check is now done in _check_all_positions_exist() (once for all positions).
        """
        deal_id = pos_state.get("deal_id")
        direction = (pos_state.get("direction") or "BUY").upper()
        logical_direction = "LONG" if direction in ("BUY", "LONG") else "SHORT"
        entry = float(pos_state.get("entry_price") or 0)
        phase = pos_state.get("phase", ExitPhase.INITIAL)
        tracker = self._get_tracker(deal_id)

        # --- Get current price (streaming preferred; REST fallback on stale/disconnect) ---
        streaming_price = self.ig.get_streaming_price()
        if streaming_price:
            current_price = streaming_price
            self._current_price = current_price
            self._streaming_reconnect_counter = 0
        else:
            # Streaming stale or unavailable — attempt background reconnect after 30 consecutive misses (60s)
            self._streaming_reconnect_counter += 1
            if self._streaming_reconnect_counter >= 30:
                self._streaming_reconnect_counter = 0
                async def _try_reconnect_streaming():
                    ok = await asyncio.get_event_loop().run_in_executor(None, self.ig.start_streaming)
                    if ok:
                        logger.info("Lightstreamer streaming reconnected")
                asyncio.create_task(_try_reconnect_streaming())

            # REST fallback
            market = await asyncio.get_event_loop().run_in_executor(
                None, self.ig.get_market_info
            )
            if not market:
                logger.warning("Failed to get market price for monitoring")
                return
            current_price = market.get("bid", 0) if logical_direction == "LONG" else market.get("offer", 0)
            self._current_price = current_price

        # --- Update momentum tracker (per-position) ---
        if tracker["momentum_tracker"] is None:
            tracker["momentum_tracker"] = MomentumTracker(logical_direction, entry)
        tracker["momentum_tracker"].add_price(current_price)
        # Sync legacy singleton for backward compat
        self.momentum_tracker = tracker["momentum_tracker"]

        # --- P&L ---
        pnl_points = (
            current_price - entry if logical_direction == "LONG"
            else entry - current_price
        )
        pnl_dollars = pnl_points * pos_state.get("lots", 0) * CONTRACT_SIZE

        logger.info(
            f"Monitoring | {logical_direction} @ {entry:.0f} | "
            f"Current: {current_price:.0f} | P&L: {pnl_points:+.0f}pts "
            f"(${pnl_dollars:+.2f}) | Phase: {phase}"
        )

        # --- Update state file for dashboard (every cycle) ---
        session = get_current_session()
        self._write_state(session_name=session["name"])

        # --- Stale data check ---
        if tracker["momentum_tracker"].is_stale():
            logger.warning(f"Stale data detected for {deal_id} — same price 10+ consecutive readings")
            # Telegram alert disabled — too noisy with 30min scan interval
            # await self.telegram.send_alert(
            #     f"WARNING: Stale data for {deal_id}. Same price for 10+ readings.\n"
            #     "Possible API issue or market halt. Not modifying position."
            # )
            return  # Don't act on stale data

        # --- Milestone alerts ---
        milestone_msg = tracker["momentum_tracker"].milestone_alert()
        if milestone_msg:
            await self.telegram.send_alert(milestone_msg)

        # --- Adverse move alerts: DISABLED (user request — too noisy) ---
        # should_alert, tier, alert_msg = tracker["momentum_tracker"].should_alert()
        # if should_alert and tier == TIER_SEVERE:
        #     await self.telegram.send_adverse_alert(alert_msg, tier, deal_id)
        #     # SEVERE: alert only — SL stays fixed at original level (no auto-breakeven)

        # --- Opus position evaluator (every 2 minutes, or on-demand via dashboard/telegram) ---
        tracker["price_buffer"].append(current_price)
        # Sync legacy singleton
        self._position_price_buffer = tracker["price_buffer"]

        # Persist buffer to disk every ~60 cycles (2 min) so restarts only gap-fill
        tracker["buffer_save_counter"] += 1
        if tracker["buffer_save_counter"] >= 60:
            tracker["buffer_save_counter"] = 0
            self._save_price_buffer_for(deal_id, pos_state.get("opened_at", ""))

        # DISABLED: force-trigger for opus pos check also disabled
        # if self._pos_check_trigger_path.exists():
        #     try:
        #         self._pos_check_trigger_path.unlink(missing_ok=True)
        #     except Exception:
        #         pass
        #     tracker["opus_eval_counter"] = OPUS_POSITION_EVAL_EVERY_N  # force eval this cycle

        # DISABLED: Opus periodic position eval (commented out to reduce API usage)
        # tracker["opus_eval_counter"] += 1
        # if tracker["opus_eval_counter"] >= OPUS_POSITION_EVAL_EVERY_N:
        #     tracker["opus_eval_counter"] = 0
        #     if self._pos_check_running:
        #         logger.info("Opus position eval skipped (on-demand check already running)")
        #     else:
        #         logger.info("Opus position eval triggered (periodic 2-min)")
        #         self._pos_check_running = True
        #         try:
        #             sl_level = pos_state.get("stop_level") or 0
        #             tp_level = pos_state.get("limit_level") or 0
        #             opened_at_str = pos_state.get("opened_at", "")
        #             try:
        #                 opened_at = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
        #                 time_open_min = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60
        #             except Exception:
        #                 time_open_min = 0
        #             try:
        #                 entry_context = json.loads(pos_state.get("entry_context") or "{}")
        #             except Exception:
        #                 entry_context = {}
        #             current_indicators = await self._fetch_current_indicators()
        #
        #             _price_buf = list(tracker["price_buffer"])
        #             eval_result = await asyncio.get_event_loop().run_in_executor(
        #                 None,
        #                 lambda: self.analyzer.evaluate_open_position(
        #                     direction=logical_direction,
        #                     entry=entry,
        #                     current_price=current_price,
        #                     stop_loss=sl_level,
        #                     take_profit=tp_level,
        #                     phase=phase,
        #                     time_in_trade_min=time_open_min,
        #                     recent_prices=_price_buf,
        #                     lots=pos_state.get("lots", 1.0),
        #                     entry_context=entry_context,
        #                     current_indicators=current_indicators,
        #                 ),
        #             )
        #
        #             rec = eval_result.get("recommendation", "HOLD")
        #             conf = eval_result.get("confidence", 0)
        #
        #             await self.telegram.send_position_eval(
        #                 eval_result=eval_result,
        #                 direction=logical_direction,
        #                 entry=entry,
        #                 current_price=current_price,
        #                 pnl_pts=pnl_points,
        #                 phase=phase,
        #                 deal_id=deal_id,
        #                 lots=pos_state.get("lots", 1.0),
        #             )
        #
        #             # Opus says CLOSE_NOW — send Telegram alert for user confirmation (never auto-close)
        #             if rec == "CLOSE_NOW" and conf >= 70:
        #                 logger.warning(
        #                     f"Opus position eval recommends CLOSE_NOW ({conf}%). Sending Telegram for confirmation."
        #                 )
        #                 await self.telegram.send_alert(
        #                     f"🔴 <b>OPUS: CLOSE NOW</b> ({conf}%)\n"
        #                     f"Deal: {deal_id} | {logical_direction}\n"
        #                     f"Reason: {eval_result.get('reasoning', 'N/A')[:200]}\n\n"
        #                     f"Use /close to confirm closing."
        #                 )
        #         finally:
        #             self._pos_check_running = False

        # --- 3-Phase exit strategy ---
        position_data = {
            "deal_id": deal_id,
            "direction": direction,
            "entry": entry,
            "size": pos_state.get("lots", 0),
            "stop_level": pos_state.get("stop_level"),
            "limit_level": pos_state.get("limit_level"),
            "current_price": current_price,
            "opened_at": pos_state.get("opened_at"),
            "phase": phase,
        }

        # SL and TP are fixed at entry — IG manages them. No mechanical modifications.

    async def _handle_position_closed(self, pos_state: dict):
        """Handle position closure detected by monitor."""
        deal_id = pos_state.get("deal_id")
        entry = float(pos_state.get("entry_price") or 0)
        direction = (pos_state.get("direction") or "BUY").upper()
        logical_direction = "LONG" if direction in ("BUY", "LONG") else "SHORT"
        phase = pos_state.get("phase", "initial")

        # Get final balance
        account = await asyncio.get_event_loop().run_in_executor(
            None, self.ig.get_account_info
        )
        new_balance = account.get("balance", 0) if account else 0

        # PnL from IG balance change — exact, includes spread and all charges
        old_balance = self.storage.get_account_state().get("balance", 0) or 0
        pnl_dollars = round(new_balance - old_balance, 2) if new_balance else 0

        # Exit price for display: use per-position momentum tracker if available, else estimate from PnL
        last_price = 0
        pos_tracker = self._position_trackers.get(deal_id, {})
        mt = pos_tracker.get("momentum_tracker") or self.momentum_tracker
        if mt and mt._prices:
            last_price = mt._prices[-1]["price"]
        if not last_price and pnl_dollars:
            lots = float(pos_state.get("lots") or 1)
            if lots and CONTRACT_SIZE:
                pnl_points_est = pnl_dollars / (lots * CONTRACT_SIZE)
                last_price = entry + pnl_points_est if logical_direction == "LONG" else entry - pnl_points_est

        pnl_points = (
            last_price - entry if logical_direction == "LONG" else entry - last_price
        ) if last_price else 0

        # Determine result (TP/SL best guess)
        tp = pos_state.get("limit_level")
        sl = pos_state.get("stop_level")
        if tp and sl and abs(last_price - tp) < abs(last_price - sl):
            result = "TP_HIT"
        elif tp and not sl and abs(last_price - tp) < 30:
            result = "TP_HIT"
        elif sl and abs(last_price - sl) < 30:
            result = "SL_HIT"
        else:
            result = "CLOSED_UNKNOWN"

        # Calculate duration
        opened_at = pos_state.get("opened_at", "")
        duration = 0
        if opened_at:
            try:
                open_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                if open_dt.tzinfo is None:
                    open_dt = open_dt.replace(tzinfo=timezone.utc)
                duration = int((datetime.now(timezone.utc) - open_dt).total_seconds() / 60)
            except ValueError:
                pass

        self.storage.log_trade_close(deal_id, {
            "exit_price": last_price,
            "pnl": pnl_dollars,
            "balance_after": new_balance,
            "result": result,
            "duration_minutes": duration,
            "phase_at_close": phase,
        })
        self.storage.record_trade_result(pnl_dollars, new_balance)
        self.storage.set_position_closed(deal_id)
        self.storage.clear_ai_cooldown()  # Position closed — allow immediate AI escalation on next scan
        self._remove_tracker(deal_id)
        self.momentum_tracker = None

        sign = "+" if pnl_dollars >= 0 else ""
        await self.telegram.send_alert(
            f"Trade Closed: {result}\n"
            f"{logical_direction} | Entry: {entry:.0f} | Exit: {last_price:.0f}\n"
            f"P&L: {sign}{pnl_points:.0f} pts (${sign}{pnl_dollars:.2f})\n"
            f"Balance: ${new_balance:.2f} | Duration: {duration} min"
        )
        logger.info(f"Position closed: {result} | P&L: {pnl_points:+.0f}pts (${pnl_dollars:+.2f})")

        # Clear price buffer cache — no longer needed for this trade
        self._price_buffer_cache_path.unlink(missing_ok=True)
        deal_cache = Path(__file__).parent / "storage" / "data" / f"price_buffer_{deal_id}.json"
        deal_cache.unlink(missing_ok=True)
        self._position_price_buffer = deque(maxlen=10800)
        self._buffer_save_counter = 0

        # Post-trade learning: update prompt_learnings.json + brier_scores.json
        try:
            # entry_context (JSON) holds setup_type, session, ai_reasoning — position_state has no those columns
            _ec = pos_state.get("entry_context") or {}
            if isinstance(_ec, str):
                try:
                    _ec = json.loads(_ec)
                except Exception:
                    _ec = {}
            trade_data = {
                "pnl": pnl_dollars,
                "setup_type": _ec.get("setup_type") or "unknown",
                "session": _ec.get("session") or "unknown",
                "confidence": pos_state.get("confidence", 0),
                "direction": logical_direction, "duration_minutes": duration,
                "phase_at_close": phase, "result": result,
            }
            ai_analysis = _ec.get("ai_reasoning", "") or ""
            post_trade_analysis(trade_data, ai_analysis)
        except Exception as e:
            logger.warning(f"Post-trade analysis failed (non-fatal): {e}")


    # ============================================================
    # SCALP AUTO-EXECUTION (Opus-gated near-miss trades)
    # ============================================================

    async def _execute_scalp(
        self, scalp_result: dict, direction: str, setup: dict,
        session: dict, current_price: float, local_conf: dict,
        final_confidence: int, indicators_snapshot: dict = None,
    ):
        """Auto-execute a scalp trade approved by Opus. No user confirmation needed."""
        tp_distance = scalp_result.get("tp_distance", 200)
        sl_distance = scalp_result.get("sl_distance", 100)  # Opus picks structure-based SL (60-120)

        # --- H1 fix: Re-fetch LIVE price (current_price is 30-120s stale from scan start) ---
        market = await asyncio.get_event_loop().run_in_executor(None, self.ig.get_market_info)
        if not market:
            logger.error("Scalp aborted: could not fetch live price")
            return
        live_price = market.get("offer") if direction == "LONG" else market.get("bid")
        live_spread = market.get("spread", SPREAD_ESTIMATE)

        tp_sign = 1 if direction == "LONG" else -1
        sl = live_price - tp_sign * sl_distance
        tp = live_price + tp_sign * tp_distance

        # --- Balance & lot sizing (now risk-based via M1+M2 fix) ---
        balance_info = await asyncio.get_event_loop().run_in_executor(None, self.ig.get_account_info)
        balance = balance_info.get("balance", 0) if balance_info else 0
        if balance <= 0:
            logger.error("Scalp aborted: could not get account balance")
            return
        lots = self.risk.get_safe_lot_size(balance, live_price, sl_distance=sl_distance)

        # --- R:R validation using live spread ---
        effective_rr = (tp_distance - live_spread) / (sl_distance + live_spread)
        if effective_rr < 1.5:
            logger.info(f"Scalp aborted: effective R:R {effective_rr:.2f} < 1.5 (spread={live_spread:.1f})")
            return

        # Position check moved to _on_trade_confirm (under lock)

        logger.info(
            f"Scalp auto-executing: {direction} @ {live_price:.0f} (was {current_price:.0f}), "
            f"SL_dist={sl_distance} TP_dist={tp_distance}, lots={lots}, spread={live_spread:.1f}"
        )

        # --- Risk validation (scalp uses lower confidence floor: 60% LONG / 65% SHORT) ---
        validation = self.risk.validate_trade(
            direction=direction,
            lots=lots,
            entry=live_price,
            stop_loss=sl,
            take_profit=tp,
            confidence=scalp_result.get("confidence", final_confidence),
            balance=balance,
            upcoming_events=[],
            indicators_snapshot=indicators_snapshot or {},
            is_scalp=True,
            setup_type=setup.get("type") if setup else None,
        )
        if not validation["approved"]:
            logger.info(f"Scalp risk validation failed: {validation['rejection_reason']}")
            return

        # Build alert data in the same format _on_trade_confirm expects
        scalp_alert = {
            "direction": direction,
            "entry": live_price,
            "sl": sl,
            "tp": tp,
            "lots": lots,
            "confidence": final_confidence,
            "setup_type": setup.get("type", "unknown"),
            "session": session["name"],
            "reasoning": scalp_result.get("reasoning", ""),
            "timestamp": datetime.now().isoformat(),
            "is_scalp": True,
            "local_confidence": local_conf.get("score"),
            "opus_confidence": scalp_result.get("confidence", 0),
            "scalp_tp_distance": tp_distance,
            "scalp_sl_distance": sl_distance,
            "effective_rr": scalp_result.get("effective_rr"),
            "ai_analysis": (
                f"[OPUS SCALP] direction={direction}, conf={scalp_result.get('confidence', 0)}%, "
                f"SL={sl_distance}pts, TP={tp_distance}pts, R:R={effective_rr:.2f}. "
                f"{scalp_result.get('reasoning', '')}"
            ),
            "indicators_compact": setup.get("indicators_snapshot", {}) if setup else {},
        }

        # Send scalp alert to Telegram with CONFIRM/REJECT buttons — never auto-execute.
        await self.telegram.send_trade_alert(scalp_alert)

    # ============================================================
    # TRADE EXECUTION (called by Telegram on CONFIRM)
    # ============================================================

    async def _on_trade_confirm(self, alert_data: dict):
        """Execute trade after user confirms via Telegram.

        Protected by _trade_execution_lock to prevent race conditions between
        auto-execute timer, user click, scalp auto-execute, and force-open.
        """
        async with self._trade_execution_lock:
            await self._on_trade_confirm_inner(alert_data)

    async def _on_trade_confirm_inner(self, alert_data: dict):
        """Inner execution logic — always called under _trade_execution_lock."""
        logger.info("Trade execution started...")

        # --- C1 fix: Re-check position count under lock ---
        _current_open = self.storage.get_open_positions_count()
        if _current_open >= MAX_OPEN_POSITIONS:
            logger.warning(f"Trade aborted: at max positions {_current_open}/{MAX_OPEN_POSITIONS} (race condition prevented)")
            await self.telegram.send_alert(f"Trade skipped — at max positions ({_current_open}/{MAX_OPEN_POSITIONS}).")
            return

        direction = alert_data.get("direction", "LONG")
        ig_direction = "BUY" if direction == "LONG" else "SELL"
        analyzed_entry = float(alert_data.get("entry", 0))

        # --- C2/C3 fix: Lightweight risk re-validation ---
        account = self.storage.get_account_state()
        consec_losses = account.get("consecutive_losses", 0)
        last_loss_time = account.get("last_loss_time")
        from config.settings import MAX_CONSECUTIVE_LOSSES, COOLDOWN_HOURS
        _max_consec = MAX_CONSECUTIVE_LOSSES
        if consec_losses >= _max_consec and last_loss_time:
            cooldown_end = datetime.fromisoformat(last_loss_time) + timedelta(hours=COOLDOWN_HOURS)
            if datetime.now() < cooldown_end:
                # High-confidence bypass: skip cooldown for strong setups
                local_conf_score = alert_data.get("local_confidence", 0)
                sonnet_conf = alert_data.get("confidence", 0)
                opus_conf = alert_data.get("opus_confidence", 0)
                bypass = False
                if local_conf_score >= 100:
                    bypass = True
                    logger.info(f"Cooldown bypass: local confidence 100% — resetting cooldown")
                    self.storage.reset_consecutive_losses()
                elif sonnet_conf >= 85:
                    bypass = True
                    logger.info(f"Cooldown bypass: Sonnet confidence {sonnet_conf}% >= 85%")
                elif opus_conf >= 80:
                    bypass = True
                    logger.info(f"Cooldown bypass: Opus confidence {opus_conf}% >= 80%")
                if not bypass:
                    cd_display = cooldown_end.replace(tzinfo=timezone.utc).astimezone(DISPLAY_TZ).strftime('%H:%M')
                    logger.warning(f"Trade aborted: in loss cooldown until {cd_display}")
                    await self.telegram.send_alert(f"Trade blocked: loss cooldown until {cd_display}.")
                    return

        # Check daily loss limit
        balance_info = await asyncio.get_event_loop().run_in_executor(None, self.ig.get_account_info)
        balance = balance_info.get("balance", 0) if balance_info else 0
        from config.settings import DAILY_LOSS_LIMIT_PERCENT
        daily_loss = account.get("daily_loss_today", 0)
        if balance > 0 and abs(daily_loss) >= balance * DAILY_LOSS_LIMIT_PERCENT:
            logger.warning(f"Trade aborted: daily loss limit reached (${abs(daily_loss):.2f})")
            await self.telegram.send_alert(f"Trade blocked: daily loss limit reached (${abs(daily_loss):.2f}).")
            return

        # Check system paused
        if not account.get("system_active", True):
            logger.warning("Trade aborted: system paused")
            await self.telegram.send_alert("Trade blocked: system is paused. Use /resume.")
            return

        # --- Re-fetch current price — check for drift ---
        market = await asyncio.get_event_loop().run_in_executor(
            None, self.ig.get_market_info
        )
        if not market:
            await self.telegram.send_alert("Execution failed: could not fetch current price.")
            return

        current_price = market.get("offer") if direction == "LONG" else market.get("bid")
        live_spread = market.get("spread", SPREAD_ESTIMATE)
        price_drift = abs(current_price - analyzed_entry)

        if price_drift > PRICE_DRIFT_ABORT_PTS:
            await self.telegram.send_alert(
                f"Trade ABORTED: price moved {price_drift:.0f} pts during analysis "
                f"(analyzed at {analyzed_entry:.0f}, now {current_price:.0f}).\n"
                f"Max allowed drift: {PRICE_DRIFT_ABORT_PTS} pts. Re-scanning."
            )
            logger.warning(f"Trade aborted: price drift {price_drift:.0f}pts")
            self.storage.clear_pending_alert()
            self._force_scan_event.set()
            return

        # --- C4+H2 fix: Use distance-based SL/TP relative to fill ---
        sl_level = alert_data.get("sl")
        tp_level = alert_data.get("tp")
        if sl_level and tp_level:
            sl_distance = int(abs(current_price - sl_level))
            tp_distance = int(abs(tp_level - current_price))
            # --- Post-drift R:R revalidation ---
            if sl_distance > 0 and price_drift > 5:
                new_rr = tp_distance / sl_distance
                if new_rr < MIN_RR_RATIO:
                    await self.telegram.send_alert(
                        f"Trade ABORTED: R:R degraded to {new_rr:.2f} after {price_drift:.0f}pt drift "
                        f"(need {MIN_RR_RATIO}). Entry {analyzed_entry:.0f}→{current_price:.0f}, "
                        f"SL dist={sl_distance}, TP dist={tp_distance}."
                    )
                    logger.warning(
                        f"Trade aborted: R:R degraded {new_rr:.2f} < {MIN_RR_RATIO} "
                        f"after {price_drift:.0f}pt drift"
                    )
                    self.storage.clear_pending_alert()
                    self._force_scan_event.set()
                    return
        else:
            sl_distance = int(DEFAULT_SL_DISTANCE)
            tp_distance = 400

        _final_lots = alert_data.get("lots", 0.01)

        # --- Place order with distance-based SL/TP ---
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.ig.open_position(
                    direction=ig_direction,
                    size=_final_lots,
                    stop_distance=sl_distance,
                    limit_distance=tp_distance,
                )
            )
        except Exception as e:
            logger.error(f"Order placement exception: {e}")
            await self.telegram.send_alert(
                f"Order placement FAILED (exception).\n"
                f"Check IG immediately — order may still have been placed.\n"
                f"Error: {str(e)[:200]}"
            )
            return

        if result is None:
            # _confirm_deal failed — order may still be open at broker
            await self.telegram.send_alert(
                "Order placed but confirmation TIMED OUT.\n"
                "Check IG immediately — position may be open.\n"
                "Bot will resume normal monitoring cycle."
            )
            return

        if result.get("error"):
            await self.telegram.send_alert(
                f"Trade REJECTED by broker: {result.get('reason')}"
            )
            return

        # --- Success ---
        actual_entry = float(result.get("level") or current_price)
        actual_sl = result.get("stop_level") or alert_data.get("sl")
        actual_tp = result.get("limit_level") or alert_data.get("tp")
        deal_id = result.get("deal_id")

        # --- CRITICAL: Verify SL/TP were actually set by IG ---
        ig_sl_set = result.get("stop_level") is not None
        ig_tp_set = result.get("limit_level") is not None
        if not ig_sl_set or not ig_tp_set:
            logger.critical(
                f"SL/TP NOT SET by IG! stop_level={result.get('stop_level')}, "
                f"limit_level={result.get('limit_level')}. Attempting modify_position repair..."
            )
            # Compute intended levels from distances
            tp_sign = 1 if direction == "LONG" else -1
            repair_sl = actual_entry - tp_sign * sl_distance if not ig_sl_set else None
            repair_tp = actual_entry + tp_sign * tp_distance if not ig_tp_set else None
            # Use actual levels if we have them
            if not ig_sl_set and actual_sl:
                repair_sl = actual_sl
            if not ig_tp_set and actual_tp:
                repair_tp = actual_tp

            try:
                repaired = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.ig.modify_position(
                        deal_id=deal_id,
                        stop_level=repair_sl if not ig_sl_set else result.get("stop_level"),
                        limit_level=repair_tp if not ig_tp_set else result.get("limit_level"),
                    )
                )
                if repaired:
                    actual_sl = repair_sl if not ig_sl_set else actual_sl
                    actual_tp = repair_tp if not ig_tp_set else actual_tp
                    logger.info(f"SL/TP REPAIRED: SL={actual_sl} TP={actual_tp}")
                    await self.telegram.send_alert(
                        f"⚠ SL/TP was NOT set by IG on order placement.\n"
                        f"Auto-repaired: SL={actual_sl:.0f} TP={actual_tp:.0f}"
                    )
                else:
                    logger.critical("SL/TP repair FAILED — modify_position returned False")
                    await self.telegram.send_alert(
                        f"CRITICAL: SL/TP NOT SET and repair FAILED!\n"
                        f"Deal: {deal_id}\n"
                        f"Set SL={actual_sl:.0f} TP={actual_tp:.0f} MANUALLY on IG NOW!"
                    )
            except Exception as e:
                logger.critical(f"SL/TP repair exception: {e}")
                await self.telegram.send_alert(
                    f"CRITICAL: SL/TP NOT SET and repair FAILED!\n"
                    f"Deal: {deal_id}\n"
                    f"Set SL={actual_sl:.0f} TP={actual_tp:.0f} MANUALLY on IG NOW!"
                )

        if not balance_info:
            balance_info = await asyncio.get_event_loop().run_in_executor(
                None, self.ig.get_account_info
            )
            balance = balance_info.get("balance", 0) if balance_info else 0

        sl_pts = abs(actual_entry - actual_sl)
        tp_pts = abs(actual_tp - actual_entry)
        entry_rr = round((tp_pts - SPREAD_ESTIMATE) / (sl_pts + SPREAD_ESTIMATE), 2) if sl_pts > 0 else 0
        ind = alert_data.get("indicators_compact") or {}
        entry_context = {
            "setup_type":    alert_data.get("setup_type", "unknown"),
            "confidence":    alert_data.get("confidence"),
            "session":       alert_data.get("session"),
            "sl_pts":        round(sl_pts),
            "tp_pts":        round(tp_pts),
            "rr":            entry_rr,
            "ai_reasoning":  alert_data.get("ai_analysis") or "",
            "rsi_15m":       ind.get("rsi_15m"),
            "rsi_4h":        ind.get("rsi_4h"),
            "ema50_15m":     ind.get("ema50_15m"),
            "above_vwap":    ind.get("above_vwap"),
            "ha_bullish":    ind.get("ha_bullish"),
            "ha_streak":     ind.get("ha_streak"),
            "daily_bullish": ind.get("daily_bullish"),
            "volume_ratio":  ind.get("volume_ratio"),
            "swing_high_20": ind.get("swing_high_20"),
            "swing_low_20":  ind.get("swing_low_20"),
        }

        trade_num = self.storage.open_trade_atomic(
            trade={
                "deal_id": result.get("deal_id"),
                "direction": direction,
                "lots": _final_lots,
                "entry_price": actual_entry,
                "stop_loss": actual_sl,
                "take_profit": actual_tp,
                "balance_before": balance,
                "confidence": alert_data.get("confidence"),
                "confidence_breakdown": alert_data.get("confidence_breakdown"),
                "setup_type": alert_data.get("setup_type"),
                "session": alert_data.get("session"),
                "ai_analysis": alert_data.get("ai_analysis"),
            },
            position={
                "deal_id": result.get("deal_id"),
                "direction": direction,
                "lots": _final_lots,
                "entry_price": actual_entry,
                "stop_level": actual_sl,
                "limit_level": actual_tp,
                "confidence": alert_data.get("confidence"),
                "entry_context": entry_context,
            }
        )

        # Init per-position tracker for new trade
        new_deal_id = result.get("deal_id")
        tracker = self._get_tracker(new_deal_id)
        tracker["momentum_tracker"] = MomentumTracker(direction, actual_entry)
        tracker["price_buffer"] = deque(maxlen=10800)
        tracker["opus_eval_counter"] = 0
        tracker["buffer_save_counter"] = 0
        tracker["empty_count"] = 0
        tracker["check_counter"] = 0
        # Sync legacy singleton
        self.momentum_tracker = tracker["momentum_tracker"]
        self._position_price_buffer = tracker["price_buffer"]
        self._position_empty_count = 0
        # Clear stale buffer cache from previous trade
        self._price_buffer_cache_path.unlink(missing_ok=True)

        # Clean up pending force-open file — trade is now open
        self._force_open_pending_path.unlink(missing_ok=True)

        actual_sl_dist = abs(actual_entry - actual_sl) if actual_sl else sl_distance
        actual_tp_dist = abs(actual_tp - actual_entry) if actual_tp else tp_distance
        await self.telegram.send_alert(
            f"Trade #{trade_num} OPENED\n"
            f"{direction} {alert_data.get('lots')} lots @ {actual_entry:.0f}\n"
            f"SL: {actual_sl:.0f} ({actual_sl_dist:.0f}pts) | TP: {actual_tp:.0f} ({actual_tp_dist:.0f}pts)\n"
            f"Spread: {live_spread:.1f}pts | Drift: {price_drift:.0f}pts\n"
            f"Monitoring active."
        )
        logger.info(f"Trade #{trade_num} opened: {direction} @ {actual_entry:.0f} SL_dist={actual_sl_dist:.0f} TP_dist={actual_tp_dist:.0f}")

    async def _poll_trigger_file(self):
        """Background task: check for dashboard force_scan.trigger every 2s, wake main loop."""
        while self.running:
            try:
                if self._trigger_path.exists():
                    self._trigger_path.unlink(missing_ok=True)
                    self._dashboard_force_scan = True
                    logger.info("Dashboard force-scan trigger detected (poll). Waking main loop.")
                    self._force_scan_event.set()
                if self._force_open_trigger_path.exists():
                    logger.info("Dashboard force-open trigger detected (poll). Waking main loop.")
                    self._force_scan_event.set()
                if self._clear_cd_path.exists():
                    self._clear_cd_path.unlink()
                    self.storage.clear_ai_cooldown()
                    logger.info("Dashboard clear-cooldown trigger detected (poll).")
            except Exception:
                pass
            await asyncio.sleep(2)

    def _save_price_buffer(self, opened_at_str: str) -> None:
        """Persist price buffer to disk so restarts don't lose history (legacy)."""
        try:
            data = {
                "opened_at": opened_at_str,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "prices": list(self._position_price_buffer),
            }
            self._price_buffer_cache_path.write_text(json.dumps(data))
        except Exception as e:
            logger.warning(f"Price buffer save failed: {e}")

    def _save_price_buffer_for(self, deal_id: str, opened_at_str: str) -> None:
        """Persist per-position price buffer to disk."""
        tracker = self._position_trackers.get(deal_id)
        if not tracker:
            return
        try:
            cache_path = Path(__file__).parent / "storage" / "data" / f"price_buffer_{deal_id}.json"
            data = {
                "opened_at": opened_at_str,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "prices": list(tracker["price_buffer"]),
            }
            cache_path.write_text(json.dumps(data))
        except Exception as e:
            logger.warning(f"Price buffer save failed for {deal_id}: {e}")

    def _load_price_buffer(self, opened_at_str: str) -> tuple:
        """
        Load price buffer from disk cache (legacy).
        Returns (prices: list, saved_at: datetime | None) if cache exists and matches this trade.
        Returns ([], None) otherwise.
        """
        try:
            if not self._price_buffer_cache_path.exists():
                return [], None
            data = json.loads(self._price_buffer_cache_path.read_text())
            if data.get("opened_at") != opened_at_str:
                # Stale cache from a different trade
                self._price_buffer_cache_path.unlink(missing_ok=True)
                return [], None
            saved_at = datetime.fromisoformat(data["saved_at"].replace("Z", "+00:00"))
            return data.get("prices", []), saved_at
        except Exception as e:
            logger.warning(f"Price buffer load failed: {e}")
            return [], None

    def _load_price_buffer_for(self, deal_id: str, opened_at_str: str) -> tuple:
        """Load per-position price buffer from disk cache."""
        cache_path = Path(__file__).parent / "storage" / "data" / f"price_buffer_{deal_id}.json"
        try:
            if not cache_path.exists():
                return self._load_price_buffer(opened_at_str)  # fallback to legacy
            data = json.loads(cache_path.read_text())
            if data.get("opened_at") != opened_at_str:
                cache_path.unlink(missing_ok=True)
                return [], None
            saved_at = datetime.fromisoformat(data["saved_at"].replace("Z", "+00:00"))
            return data.get("prices", []), saved_at
        except Exception as e:
            logger.warning(f"Price buffer load failed for {deal_id}: {e}")
            return [], None

    async def _async_fetch_fetch_and_set_buffer(self, opened_at_str: str) -> None:
        """Legacy: On startup with existing position, restore price buffer."""
        try:
            opened_at_dt = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
        except Exception:
            return

        cached_prices, saved_at = self._load_price_buffer(opened_at_str)

        if cached_prices and saved_at:
            gap_prices = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.ig.get_trade_history_buffer(saved_at)
            )
            self._position_price_buffer = deque((cached_prices + gap_prices)[-10800:], maxlen=10800)
            logger.info(
                f"Buffer restored: {len(cached_prices)} cached + {len(gap_prices)} gap → "
                f"{len(self._position_price_buffer)} total"
            )
        else:
            full_prices = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.ig.get_trade_history_buffer(opened_at_dt)
            )
            self._position_price_buffer = deque(full_prices, maxlen=10800)
            logger.info(f"Buffer cold-filled: {len(full_prices)} price points from IG")

    async def _async_fetch_and_set_buffer_for(self, deal_id: str, opened_at_str: str) -> None:
        """Per-position: On startup, restore price buffer for specific deal."""
        try:
            opened_at_dt = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
        except Exception:
            return

        tracker = self._get_tracker(deal_id)
        cached_prices, saved_at = self._load_price_buffer_for(deal_id, opened_at_str)

        if cached_prices and saved_at:
            gap_prices = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.ig.get_trade_history_buffer(saved_at)
            )
            tracker["price_buffer"] = deque((cached_prices + gap_prices)[-10800:], maxlen=10800)
            logger.info(
                f"Buffer restored for {deal_id}: {len(cached_prices)} cached + {len(gap_prices)} gap → "
                f"{len(tracker['price_buffer'])} total"
            )
        else:
            full_prices = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.ig.get_trade_history_buffer(opened_at_dt)
            )
            tracker["price_buffer"] = deque(full_prices, maxlen=10800)
            logger.info(f"Buffer cold-filled for {deal_id}: {len(full_prices)} price points from IG")

        # Immediately persist so next restart has this as baseline
        self._save_price_buffer(opened_at_str)

    async def _fetch_current_indicators(self) -> dict:
        """Fetch fresh 15M + 4H candles and return compact indicator snapshot for position evaluator."""
        try:
            candles_15m, candles_4h = await asyncio.gather(
                asyncio.get_event_loop().run_in_executor(None, lambda: self.ig.get_prices("MINUTE_15", 30)),
                asyncio.get_event_loop().run_in_executor(None, lambda: self.ig.get_prices("HOUR_4", 30)),
            )
            tf_15m = analyze_timeframe(candles_15m) if candles_15m else {}
            tf_4h  = analyze_timeframe(candles_4h)  if candles_4h  else {}
            price  = tf_15m.get("price", 0) or 0
            ema9   = tf_15m.get("ema9",  0) or 0
            ema50  = tf_15m.get("ema50", 0) or 0
            return {
                "rsi_15m":     tf_15m.get("rsi"),
                "rsi_4h":      tf_4h.get("rsi"),
                "ema9_dist":   round(price - ema9,  1) if price and ema9  else None,
                "ema50_dist":  round(price - ema50, 1) if price and ema50 else None,
                "above_vwap":  tf_15m.get("above_vwap"),
                "vwap":        tf_15m.get("vwap"),
                "bb_upper":    tf_15m.get("bollinger_upper"),
                "bb_mid":      tf_15m.get("bollinger_mid"),
                "bb_lower":    tf_15m.get("bollinger_lower"),
                "ha_bullish":  tf_15m.get("ha_bullish"),
                "ha_streak":   tf_15m.get("ha_streak"),
                "daily_bullish": tf_15m.get("above_ema200_fallback"),
            }
        except Exception as e:
            logger.warning(f"_fetch_current_indicators failed: {e}")
            return {}

    async def _run_concurrent_scan(self):
        """Background scan that runs while a position is open and below position cap.
        Fires every SCAN_INTERVAL_SECONDS from the monitoring loop.
        Protected by _concurrent_scan_running flag to prevent overlapping scans.
        """
        try:
            logger.info("Concurrent scan started (position open, capacity available)")
            await self._scanning_cycle()
            self._last_concurrent_scan_at = datetime.now(timezone.utc)
        except Exception as e:
            logger.error(f"Concurrent scan error: {e}", exc_info=True)
        finally:
            self._concurrent_scan_running = False

    async def _on_force_scan(self):
        """Triggered by /force command. Wakes the main loop immediately."""
        await self.telegram.send_alert("Force scan requested. Running next cycle immediately...")
        self._force_scan_event.set()

    async def _on_pos_check(self):
        """DISABLED: Opus position eval disabled to reduce API usage."""
        await self.telegram.send_alert("ℹ️ Position check is currently disabled.")

    # ============================================================
    # SHUTDOWN
    # ============================================================

    async def _shutdown(self):
        logger.info("Shutting down...")
        try:
            self.ig.stop_streaming()
        except Exception:
            pass
        try:
            # Only attempt Telegram alert if the app was fully initialized
            if self.telegram and getattr(self.telegram, 'app', None) and self.telegram.app.running:
                await asyncio.wait_for(
                    self.telegram.send_alert("Monitor shutting down. Positions remain protected by broker stops."),
                    timeout=2.0,
                )
        except Exception:
            pass
        try:
            if self.telegram and getattr(self.telegram, 'app', None):
                await asyncio.wait_for(self.telegram.stop(), timeout=2.0)
        except Exception:
            pass
        try:
            self.researcher.close()
        except Exception:
            pass
        logger.info("Monitor stopped.")


def main():
    monitor = TradingMonitor()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _main_task = None

    def signal_handler(sig, frame):
        logger.info(f"Signal {sig} received. Exiting immediately.")
        # Positions are protected by broker stops — safe to hard-exit.
        import os
        os._exit(0)

    def force_scan_handler(sig, frame):
        logger.info("SIGUSR1 received — dashboard force scan. Waking main loop.")
        loop.call_soon_threadsafe(monitor._force_scan_event.set)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGUSR1, force_scan_handler)

    try:
        _main_task = loop.create_task(monitor.start())
        loop.run_until_complete(_main_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        monitor.running = False
    finally:
        # Shutdown already ran inside start()'s finally block.
        # Force-exit to avoid hanging on Telegram's internal polling tasks.
        import os
        os._exit(0)


if __name__ == "__main__":
    main()
