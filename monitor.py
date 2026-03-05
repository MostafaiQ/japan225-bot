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
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config.settings import (
    LOG_FORMAT, LOG_LEVEL, TRADING_MODE, CONTRACT_SIZE,
    MONITOR_INTERVAL_SECONDS, POSITION_CHECK_EVERY_N_CYCLES, OPUS_POSITION_EVAL_EVERY_N,
    SCAN_INTERVAL_SECONDS, OFFHOURS_INTERVAL_SECONDS,
    AI_COOLDOWN_MINUTES, HAIKU_MIN_SCORE, PRICE_DRIFT_ABORT_PTS, SAFETY_CONSECUTIVE_EMPTY,
    calculate_margin, calculate_profit, SPREAD_ESTIMATE, DEFAULT_SL_DISTANCE,
    MIN_CONFIDENCE, MIN_CONFIDENCE_SHORT,
    DAILY_EMA200_CANDLES, PRE_SCREEN_CANDLES, AI_ESCALATION_CANDLES,
    MINUTE_5_CANDLES, DISPLAY_TZ, display_now,
    EXTREME_DAY_RANGE_PTS, MOMENTUM_SCAN_BYPASS_SIGNALS,
    TOKYO_FORCED_LOTS, TOKYO_MAX_CONSECUTIVE_LOSSES,
)
from core.ig_client import IGClient, POSITIONS_API_ERROR
from core.indicators import analyze_timeframe, detect_setup
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
        self._opus_pos_eval_counter = 0          # Counts monitoring cycles; eval fires every OPUS_POSITION_EVAL_EVERY_N
        self._position_price_buffer: list = []   # Rolling price buffer for Opus position evaluator
        self._streaming_reconnect_counter = 0    # Consecutive cycles without streaming price → triggers reconnect
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
        self._write_state(phase="STARTING")

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
                self._write_state(phase="IG_DISCONNECTED")
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
        Handles the case where bot crashed while a position was open.
        """
        logger.info("Running startup sync...")
        ig_positions = self.ig.get_open_positions()
        db_state = self.storage.get_position_state()

        if ig_positions is POSITIONS_API_ERROR:
            msg = "Startup sync: IG API unavailable. Cannot verify position state. Proceeding with DB state."
            logger.warning(msg)
            await self.telegram.send_alert(msg)
            return

        has_ig_position = len(ig_positions) > 0
        has_db_position = bool(db_state.get("has_open"))

        if has_ig_position and not has_db_position:
            # IG has a position we didn't know about (crashed after open, before DB write)
            pos = ig_positions[0]
            logger.warning("RECOVERY: IG has position not in DB. Syncing.")
            direction_raw = (pos.get("direction") or "BUY").upper()
            direction_log = "LONG" if direction_raw in ("BUY", "LONG") else "SHORT"
            self.storage.open_trade_atomic(
                trade={
                    "deal_id": pos.get("deal_id"),
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
                    "deal_id": pos.get("deal_id"),
                    "direction": direction_log,
                    "lots": pos.get("size"),
                    "entry_price": pos.get("level"),
                    "stop_level": pos.get("stop_level"),
                    "limit_level": pos.get("limit_level"),
                    "opened_at": pos.get("created", datetime.now(timezone.utc).isoformat()),
                    "confidence": 0,
                },
            )
            # Init momentum tracker for recovered position
            direction = "LONG" if (pos.get("direction") or "BUY").upper() in ("BUY", "LONG") else "SHORT"
            self.momentum_tracker = MomentumTracker(direction, float(pos.get("level") or 0))
            # Restore price buffer from disk cache + gap-fill from IG
            opened_at_str = pos.get("created", "")
            if opened_at_str:
                await self._async_fetch_fetch_and_set_buffer(opened_at_str)
            await self.telegram.send_alert(
                "Bot restarted. Found open position on IG not in DB.\n"
                f"Deal: {pos.get('deal_id')} | {pos.get('direction')} @ {pos.get('level')}\n"
                "Synced and resuming monitoring."
            )

        elif not has_ig_position and has_db_position:
            # DB says we have a position but IG doesn't — position closed while offline
            logger.warning("RECOVERY: DB shows position but IG has none. Position closed while offline.")
            deal_id = db_state.get("deal_id")
            self.storage.set_position_closed()
            if deal_id:
                # Attempt to record the close
                self.storage.log_trade_close(deal_id, {
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                    "result": "CLOSED_WHILE_OFFLINE",
                    "notes": "Position detected closed on bot restart",
                })
            await self.telegram.send_alert(
                "Bot restarted. Position was closed while offline.\n"
                "Check IG for final P&L details."
            )

        elif has_ig_position and has_db_position:
            # Both agree — verify direction matches
            ig_dir = (ig_positions[0].get("direction") or "BUY").upper()
            db_dir = (db_state.get("direction") or "BUY").upper()
            db_deal = db_state.get("deal_id")
            ig_deal = ig_positions[0].get("deal_id")

            if db_deal == ig_deal:
                # Reinit momentum tracker from DB entry
                direction = "LONG" if db_dir in ("BUY", "LONG") else "SHORT"
                entry = float(db_state.get("entry_price") or 0)
                self.momentum_tracker = MomentumTracker(direction, entry)
                logger.info("RECOVERY: Position intact on both. Resuming monitoring.")
                # Restore price buffer from disk cache + gap-fill from IG
                opened_at_str = db_state.get("opened_at", "")
                if opened_at_str:
                    await self._async_fetch_fetch_and_set_buffer(opened_at_str)
                await self.telegram.send_alert(
                    f"Bot restarted. {db_dir} position intact.\n"
                    f"Entry: {db_state.get('entry_price', 0):.0f} | "
                    f"Phase: {db_state.get('phase', 'initial')}\n"
                    "Resuming monitoring."
                )
            else:
                logger.warning(f"Deal ID mismatch: DB={db_deal} IG={ig_deal}. Syncing to IG.")
                await self.telegram.send_alert(
                    f"Deal ID mismatch detected. DB: {db_deal}, IG: {ig_deal}.\n"
                    "Syncing DB to IG state."
                )

        else:
            # Clean start — no position anywhere
            logger.info("Clean start. No open positions.")
            await self.telegram.send_alert(
                f"Bot started. Scanning mode active.\n"
                f"Mode: {TRADING_MODE.upper()}"
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
        except Exception as e:
            logger.debug(f"_reload_overrides skipped: {e}")

    def _write_state(self, session_name: str | None = None, phase: str | None = None):
        """Write bot_state.json for the dashboard to read."""
        try:
            if session_name:
                self._current_session = session_name
            uptime_secs = int((datetime.now(timezone.utc) - self._started_at).total_seconds())
            h, rem = divmod(uptime_secs, 3600)
            m = rem // 60
            state = {
                "session":        self._current_session or "—",
                "phase":          phase or (
                    "MONITORING" if self.storage.get_position_state().get("has_open")
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

            pos_state = self.storage.get_position_state()

            if pos_state.get("has_open"):
                await self._monitoring_cycle(pos_state)
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
        # Run detect_setup() for BOTH directions independently.
        # 4H data now available at pre-screen for extreme_oversold_reversal and other setups.
        setup_long = detect_setup(
            tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_15m,
            exclude_direction="SHORT",
        )
        setup_short = detect_setup(
            tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_15m,
            exclude_direction="LONG",
        )

        # --- 5M fallback: for each direction that didn't find on 15M, try 5M ---
        if not setup_long["found"] and tf_5m:
            setup_5m_long = detect_setup(tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_5m, exclude_direction="SHORT")
            if setup_5m_long["found"] and self._5m_aligns_with_15m(setup_5m_long, tf_15m):
                setup_long = setup_5m_long
                setup_long["type"] += "_5m"
                setup_long["_entry_tf"] = "5m"
                logger.info(f"5M fallback: LONG {setup_long['type']} detected")

        if not setup_short["found"] and tf_5m:
            setup_5m_short = detect_setup(tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_5m, exclude_direction="LONG")
            if setup_5m_short["found"] and self._5m_aligns_with_15m(setup_5m_short, tf_15m):
                setup_short = setup_5m_short
                setup_short["type"] += "_5m"
                setup_short["_entry_tf"] = "5m"
                logger.info(f"5M fallback: SHORT {setup_short['type']} detected")

        # Neither direction found a setup
        if not setup_long["found"] and not setup_short["found"]:
            # --- Momentum scan bypass: if indicators overwhelmingly bullish, skip to Opus ---
            # 5 signals: above EMA50, above VWAP, HA streak >= 2, RSI 45-72, 4H above EMA50
            _mom_signals = 0
            if tf_15m.get("above_ema50"):
                _mom_signals += 1
            if tf_15m.get("above_vwap"):
                _mom_signals += 1
            _ha_st = tf_15m.get("ha_streak")
            if _ha_st is not None and _ha_st >= 2:
                _mom_signals += 1
            _rsi_15 = tf_15m.get("rsi")
            if _rsi_15 is not None and 45 <= _rsi_15 <= 72:
                _mom_signals += 1
            if tf_4h.get("above_ema50"):
                _mom_signals += 1

            if _mom_signals >= MOMENTUM_SCAN_BYPASS_SIGNALS:
                logger.info(
                    f"MOMENTUM BYPASS: {_mom_signals}/5 bullish signals, no formal setup. "
                    f"Sending to Opus for evaluation."
                )
                indicators = {"m15": tf_15m, "h4": tf_4h, "daily": tf_daily}
                if tf_5m:
                    indicators["m5"] = tf_5m
                try:
                    _mom_opus_result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: self.analyzer.evaluate_scalp(
                            indicators=indicators,
                            primary_direction="LONG",
                            setup_type="momentum_bypass",
                            local_confidence=65,
                            ai_confidence=0,
                            ai_reasoning=(
                                f"MOMENTUM BYPASS: No formal setup fired, but {_mom_signals}/5 bullish signals active. "
                                f"RSI={_rsi_15}, HA streak={_ha_st}, above EMA50+VWAP. "
                                f"Market is strongly trending — evaluate for trend-following LONG entry."
                            ),
                        ),
                    )
                    if _mom_opus_result.get("scalp_viable"):
                        _mom_conf = _mom_opus_result.get("confidence", 0)
                        if _mom_conf >= 60:
                            logger.info(
                                f"Momentum bypass: Opus approved LONG scalp, conf={_mom_conf}%"
                            )
                            self._last_scan_detail = {
                                "outcome": "momentum_bypass",
                                "direction": "LONG",
                                "confidence": _mom_conf,
                                "price": current_price,
                            }
                            # Build minimal local_conf for _execute_scalp
                            _mom_local_conf = {"score": 65, "criteria": {}, "reasons": {}}
                            await self._execute_scalp(
                                scalp_result=_mom_opus_result,
                                direction=_mom_opus_result.get("direction", "LONG"),
                                setup={"type": "momentum_bypass", "direction": "LONG",
                                       "reasoning": f"Momentum bypass ({_mom_signals}/5 signals)"},
                                session=session,
                                current_price=current_price,
                                local_conf=_mom_local_conf,
                                final_confidence=_mom_conf,
                                indicators_snapshot=indicators,
                            )
                            return 0
                        else:
                            logger.info(f"Momentum bypass: Opus conf {_mom_conf}% < 60%, skipping")
                    else:
                        logger.info(f"Momentum bypass: Opus rejected — {_mom_opus_result.get('reasoning', '')[:100]}")
                except Exception as e:
                    logger.warning(f"Momentum bypass Opus eval failed: {e}")

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

        if setup_short["found"]:
            conf_short = compute_confidence(
                direction="SHORT",
                tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_15m,
                upcoming_events=web_research.get("economic_calendar", []),
                web_research=web_research,
                setup_type=setup_short.get("type"),
            )
            logger.info(f"Local confidence SHORT: {conf_short['score']}%")

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

        recent_trades_ctx = self.storage.get_recent_trades(10)

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
            ),
        )

        # Await Sonnet result
        sonnet_result = await sonnet_future
        final_result = sonnet_result
        final_confidence = final_result.get("confidence", 0)
        logger.info(f"AI: found={final_result.get('setup_found')}, confidence={final_confidence}%")
        logger.info(f"AI reasoning: {final_result.get('reasoning', 'N/A')[:500]}")

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

            # --- Sequential Opus scalp eval (Sonnet rejected → Opus gets full context) ---
            # Gate: skip Opus for quick-rejects (Sonnet conf < 50%). Saves API cost.
            # EXCEPTION: momentum setups always go to Opus — Sonnet may undervalue them
            # and Opus can find a scalp even when the full TP target is unrealistic.
            _momentum_types = {"momentum_continuation_long", "breakout_long", "vwap_bounce_long",
                               "ema9_pullback_long", "momentum_continuation_short", "vwap_rejection_short_momentum"}
            _is_momentum = setup.get("type") in _momentum_types
            if final_confidence < 50 and not _is_momentum:
                logger.info(
                    f"Sonnet quick-reject (conf {final_confidence}% < 50%). "
                    f"Skipping Opus — setup too weak."
                )
                return SCAN_INTERVAL_SECONDS
            if _is_momentum and final_confidence < 50:
                logger.info(
                    f"Sonnet low-conf ({final_confidence}%) but momentum setup — sending to Opus scalper."
                )
            logger.info(
                f"Sonnet rejected (conf {final_confidence}%). "
                f"Launching Opus scalp eval with Sonnet's full analysis..."
            )
            try:
                sonnet_reasoning = final_result.get("reasoning", "")
                opus_context = (
                    f"SONNET ANALYSIS: {sonnet_reasoning}\n"
                    f"SONNET CONFIDENCE: {final_confidence}%\n"
                    f"SONNET DECISION: {'APPROVED' if final_result.get('setup_found') else 'REJECTED'}\n"
                    f"LOCAL PRE-SCREEN: {prescreen_direction} {setup.get('type', 'unknown')} "
                    f"| local conf {local_score}%"
                )
                if secondary_setup:
                    sec = secondary_setup
                    opus_context += (
                        f"\nSECONDARY: {sec['direction']} {sec.get('type', '?')} "
                        f"local conf {sec.get('confidence', '?')}%: "
                        f"{sec.get('reasoning', '')}"
                    )

                # Recent Opus decision for consistency
                recent_opus = None
                if self._last_opus_decision:
                    age_sec = (datetime.now() - datetime.fromisoformat(self._last_opus_decision["timestamp"])).total_seconds()
                    if age_sec < 900:  # 15 min
                        recent_opus = self._last_opus_decision

                scalp_result = await loop.run_in_executor(
                    None,
                    lambda: self.analyzer.evaluate_scalp(
                        indicators=indicators,
                        primary_direction=prescreen_direction,
                        setup_type=setup.get("type", "unknown"),
                        local_confidence=local_score,
                        ai_confidence=final_confidence,
                        ai_reasoning=opus_context,
                        recent_opus_decision=recent_opus,
                    ),
                )

                # Track Opus decision for directional consistency
                self._last_opus_decision = {
                    "direction": scalp_result.get("direction", direction),
                    "reasoning": scalp_result.get("reasoning", "")[:300],
                    "viable": scalp_result.get("scalp_viable", False),
                    "confidence": scalp_result.get("confidence", 0),
                    "timestamp": datetime.now().isoformat(),
                }
                if scalp_result.get("scalp_viable"):
                    opus_direction = scalp_result.get("direction", direction)
                    opus_conf = scalp_result.get("confidence", 0)
                    setup_type = setup.get("type", "unknown")

                    # --- Guard 1: Opus must have >= 60% confidence ---
                    if opus_conf < 60:
                        logger.info(
                            f"Opus scalp rejected: confidence {opus_conf}% < 60% minimum"
                        )
                        return SCAN_INTERVAL_SECONDS

                    # --- Guard 2: Block direction flip on inherently-directional setups ---
                    # Bounce setups (lower_bounce, oversold_reversal) are inherently LONG.
                    # Breakdown setups are inherently SHORT. Opus should NOT flip these.
                    _bounce_setups = {
                        "bollinger_lower_bounce", "bollinger_mid_bounce", "ema50_bounce",
                        "oversold_reversal", "extreme_oversold_reversal",
                        "momentum_continuation_long", "breakout_long",
                        "vwap_bounce_long", "ema9_pullback_long", "momentum_bypass",
                    }
                    _breakdown_setups = {
                        "bear_flag_breakdown", "breakdown_continuation", "multi_tf_bearish",
                        "dead_cat_bounce_short", "vwap_rejection_short",
                        "high_volume_distribution", "lower_lows_bearish",
                        "bollinger_upper_rejection", "ema50_rejection", "bb_mid_rejection",
                        "overbought_reversal", "ema200_rejection", "pivot_r1_rejection",
                        "momentum_continuation_short", "vwap_rejection_short_momentum",
                    }
                    if setup_type in _bounce_setups and opus_direction == "SHORT":
                        logger.info(
                            f"Opus scalp blocked: {setup_type} is a LONG setup, "
                            f"Opus tried SHORT — contradicts setup thesis"
                        )
                        return SCAN_INTERVAL_SECONDS
                    if setup_type in _breakdown_setups and opus_direction == "LONG":
                        logger.info(
                            f"Opus scalp blocked: {setup_type} is a SHORT setup, "
                            f"Opus tried LONG — contradicts setup thesis"
                        )
                        return SCAN_INTERVAL_SECONDS

                    logger.info(f"Opus scalp: {opus_direction} (pre-screen was {direction})")
                    await self._execute_scalp(
                        scalp_result=scalp_result,
                        direction=opus_direction,
                        setup=setup,
                        session=session,
                        current_price=current_price,
                        local_conf=local_conf,
                        final_confidence=final_confidence,
                        indicators_snapshot=indicators,
                    )
                    return 0  # Enter monitoring immediately
                else:
                    logger.info(f"Opus rejected both directions: {scalp_result.get('reasoning', '')[:150]}")
            except Exception as e:
                logger.warning(f"Opus scalp evaluation failed: {e}")

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
        lots = self.risk.get_safe_lot_size(balance, current_price, sl_distance=sl_distance)
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

        # Auto-execute immediately — Sonnet passed confidence thresholds (70 LONG / 75 SHORT)
        # + risk validation. Notify user then execute (same flow as Opus scalp).
        logger.info(
            f"Sonnet auto-executing: {direction} @ {entry:.0f}, "
            f"confidence={final_confidence}%, SL={sl:.0f}, TP={tp:.0f}"
        )
        await self.telegram.send_trade_alert(trade_alert)
        await self._on_trade_confirm(trade_alert)

        return 0  # Enter monitoring immediately

    # ============================================================
    # MONITORING MODE
    # ============================================================

    async def _monitoring_cycle(self, pos_state: dict):
        """Position monitoring — runs every 2s when a trade is open.
        15 cycles × 2s = 30s window:
          - 14 cycles: get_market_info only (price check)
          -  1 cycle : get_open_positions only (existence check, replaces price call)
        Total: exactly 15 calls per 30s = 30 calls/min — within IG non-trading limit.
        """
        deal_id = pos_state.get("deal_id")
        direction = (pos_state.get("direction") or "BUY").upper()
        logical_direction = "LONG" if direction in ("BUY", "LONG") else "SHORT"
        entry = float(pos_state.get("entry_price") or 0)
        phase = pos_state.get("phase", ExitPhase.INITIAL)

        # --- Check if position still exists on IG (every N cycles = every 30s) ---
        self._position_check_counter += 1
        if self._position_check_counter >= POSITION_CHECK_EVERY_N_CYCLES:
            self._position_check_counter = 0
            live_positions = await asyncio.get_event_loop().run_in_executor(
                None, self.ig.get_open_positions
            )

            if live_positions is POSITIONS_API_ERROR:
                logger.warning("IG API error checking positions. Skipping cycle.")
                await self.telegram.send_alert(
                    "WARNING: IG API error while checking position. Will retry."
                )
                self._position_empty_count = 0
                return

            position_exists = any(
                p.get("deal_id") == deal_id for p in live_positions
            )

            if not position_exists:
                self._position_empty_count += 1
                if self._position_empty_count < SAFETY_CONSECUTIVE_EMPTY:
                    logger.warning(
                        f"Position not found on IG ({self._position_empty_count}/{SAFETY_CONSECUTIVE_EMPTY}). "
                        f"Waiting for {SAFETY_CONSECUTIVE_EMPTY - self._position_empty_count} more confirmation(s)."
                    )
                    return
                # Confirmed closed after N consecutive empties
                self._position_empty_count = 0
                await self._handle_position_closed(pos_state)
                return

            # Position confirmed open — reset counter.
            # Return here: this cycle's 1 API call (get_open_positions) replaces
            # the price call, keeping total at exactly 30 calls/min.
            self._position_empty_count = 0
            return

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

        # --- Update momentum tracker ---
        if self.momentum_tracker is None:
            self.momentum_tracker = MomentumTracker(logical_direction, entry)
        self.momentum_tracker.add_price(current_price)

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
        if self.momentum_tracker.is_stale():
            logger.warning("Stale data detected — same price 10+ consecutive readings")
            await self.telegram.send_alert(
                "WARNING: Stale data detected. Same price for 10+ readings.\n"
                "Possible API issue or market halt. Not modifying position."
            )
            return  # Don't act on stale data

        # --- Milestone alerts ---
        milestone_msg = self.momentum_tracker.milestone_alert()
        if milestone_msg:
            await self.telegram.send_position_update(pnl_points, phase, current_price)

        # --- Adverse move alerts: SEVERE safety net (MILD/MODERATE replaced by Opus position evaluator) ---
        should_alert, tier, alert_msg = self.momentum_tracker.should_alert()
        if should_alert and tier == TIER_SEVERE:
            await self.telegram.send_adverse_alert(alert_msg, tier, deal_id)
            # SEVERE: alert only — SL stays fixed at original level (no auto-breakeven)

        # --- Opus position evaluator (every 2 minutes, or on-demand via dashboard/telegram) ---
        self._position_price_buffer.append(current_price)
        if len(self._position_price_buffer) > 10800:  # hard cap at 6hrs (~6hrs × 1800 readings)
            self._position_price_buffer.pop(0)

        # Persist buffer to disk every ~60 cycles (2 min) so restarts only gap-fill
        self._buffer_save_counter += 1
        if self._buffer_save_counter >= 60:
            self._buffer_save_counter = 0
            self._save_price_buffer(pos_state.get("opened_at", ""))

        if self._pos_check_trigger_path.exists():
            try:
                self._pos_check_trigger_path.unlink(missing_ok=True)
            except Exception:
                pass
            self._opus_pos_eval_counter = OPUS_POSITION_EVAL_EVERY_N  # force eval this cycle

        self._opus_pos_eval_counter += 1
        if self._opus_pos_eval_counter >= OPUS_POSITION_EVAL_EVERY_N:
            self._opus_pos_eval_counter = 0
            if self._pos_check_running:
                logger.info("Opus position eval skipped (on-demand check already running)")
            else:
                logger.info("Opus position eval triggered (periodic 2-min)")
                self._pos_check_running = True
                try:
                    sl_level = pos_state.get("stop_level") or 0
                    tp_level = pos_state.get("limit_level") or 0
                    opened_at_str = pos_state.get("opened_at", "")
                    try:
                        opened_at = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
                        time_open_min = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60
                    except Exception:
                        time_open_min = 0
                    try:
                        entry_context = json.loads(pos_state.get("entry_context") or "{}")
                    except Exception:
                        entry_context = {}
                    current_indicators = await self._fetch_current_indicators()

                    eval_result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: self.analyzer.evaluate_open_position(
                            direction=logical_direction,
                            entry=entry,
                            current_price=current_price,
                            stop_loss=sl_level,
                            take_profit=tp_level,
                            phase=phase,
                            time_in_trade_min=time_open_min,
                            recent_prices=list(self._position_price_buffer),
                            lots=pos_state.get("lots", 1.0),
                            entry_context=entry_context,
                            current_indicators=current_indicators,
                        ),
                    )

                    rec = eval_result.get("recommendation", "HOLD")
                    conf = eval_result.get("confidence", 0)

                    await self.telegram.send_position_eval(
                        eval_result=eval_result,
                        direction=logical_direction,
                        entry=entry,
                        current_price=current_price,
                        pnl_pts=pnl_points,
                        phase=phase,
                        deal_id=deal_id,
                        lots=pos_state.get("lots", 1.0),
                    )

                    # Auto-close if Opus says CLOSE_NOW with high confidence
                    if rec == "CLOSE_NOW" and conf >= 70:
                        logger.warning(
                            f"Opus position eval recommends CLOSE_NOW ({conf}%). Auto-closing."
                        )
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: self.ig.close_position(
                                deal_id=deal_id,
                                direction=logical_direction,
                                size=pos_state.get("lots", 1.0),
                            )
                        )
                finally:
                    self._pos_check_running = False

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

        # Exit price for display: use momentum tracker if available, else estimate from PnL
        last_price = 0
        if self.momentum_tracker and self.momentum_tracker._prices:
            last_price = self.momentum_tracker._prices[-1]["price"]
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
        self.storage.set_position_closed()
        self.storage.clear_ai_cooldown()  # Position closed — allow immediate AI escalation on next scan
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
        self._position_price_buffer = []
        self._buffer_save_counter = 0

        # Post-trade learning: update prompt_learnings.json + brier_scores.json
        try:
            trade_data = {
                "pnl": pnl_dollars, "setup_type": pos_state.get("setup_type", "unknown"),
                "session": pos_state.get("session", "unknown"),
                "confidence": pos_state.get("confidence", 0),
                "direction": logical_direction, "duration_minutes": duration,
                "phase_at_close": phase, "result": result,
            }
            ai_analysis = pos_state.get("ai_analysis", "")
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

        # Notify user THEN execute (no confirmation needed)
        await self.telegram.send_scalp_executed(scalp_alert, scalp_result)
        await self._on_trade_confirm(scalp_alert)

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

        # --- C1 fix: Re-check position state under lock ---
        pos = self.storage.get_position_state()
        if pos.get("has_open"):
            logger.warning("Trade aborted: position already open (race condition prevented)")
            await self.telegram.send_alert("Trade skipped — position already open.")
            return

        direction = alert_data.get("direction", "LONG")
        ig_direction = "BUY" if direction == "LONG" else "SELL"
        analyzed_entry = float(alert_data.get("entry", 0))

        # Session-specific rules (Tokyo: minimum lots, tighter TP, higher loss tolerance)
        _is_tokyo = get_current_session()["name"] == "tokyo"

        # --- C2/C3 fix: Lightweight risk re-validation ---
        account = self.storage.get_account_state()
        consec_losses = account.get("consecutive_losses", 0)
        last_loss_time = account.get("last_loss_time")
        from config.settings import MAX_CONSECUTIVE_LOSSES, COOLDOWN_HOURS
        _max_consec = TOKYO_MAX_CONSECUTIVE_LOSSES if _is_tokyo else MAX_CONSECUTIVE_LOSSES
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
        else:
            sl_distance = int(DEFAULT_SL_DISTANCE)
            tp_distance = 400

        # --- Tokyo session mode: minimum lots (AI still decides SL/TP based on volatility) ---
        _final_lots = alert_data.get("lots", 0.01)
        if _is_tokyo and _final_lots > TOKYO_FORCED_LOTS:
            logger.info(f"Tokyo mode: capping lots {_final_lots}→{TOKYO_FORCED_LOTS} (min-risk, AI sets SL/TP)")
            _final_lots = TOKYO_FORCED_LOTS

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

        # Init momentum tracker + reset price buffer for fresh trade history
        self.momentum_tracker = MomentumTracker(direction, actual_entry)
        self._position_price_buffer = []
        self._opus_pos_eval_counter = 0
        self._buffer_save_counter = 0
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
        """Persist price buffer to disk so restarts don't lose history."""
        try:
            data = {
                "opened_at": opened_at_str,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "prices": self._position_price_buffer,
            }
            self._price_buffer_cache_path.write_text(json.dumps(data))
        except Exception as e:
            logger.warning(f"Price buffer save failed: {e}")

    def _load_price_buffer(self, opened_at_str: str) -> tuple:
        """
        Load price buffer from disk cache.
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

    async def _async_fetch_fetch_and_set_buffer(self, opened_at_str: str) -> None:
        """
        On startup with existing position: load cached buffer from disk, then
        fetch only the gap (saved_at → now) from IG and append. Far cheaper than
        re-fetching the full history on every restart.
        """
        try:
            opened_at_dt = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
        except Exception:
            return

        cached_prices, saved_at = self._load_price_buffer(opened_at_str)

        if cached_prices and saved_at:
            # Cache hit — only fetch the gap
            gap_prices = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.ig.get_trade_history_buffer(saved_at)
            )
            self._position_price_buffer = (cached_prices + gap_prices)[-10800:]
            logger.info(
                f"Buffer restored: {len(cached_prices)} cached + {len(gap_prices)} gap → "
                f"{len(self._position_price_buffer)} total"
            )
        else:
            # No cache — full fetch from trade open
            full_prices = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.ig.get_trade_history_buffer(opened_at_dt)
            )
            self._position_price_buffer = full_prices
            logger.info(f"Buffer cold-filled: {len(full_prices)} price points from IG")

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

    async def _on_force_scan(self):
        """Triggered by /force command. Wakes the main loop immediately."""
        await self.telegram.send_alert("Force scan requested. Running next cycle immediately...")
        self._force_scan_event.set()

    async def _on_pos_check(self):
        """Triggered by /poscheck command or dashboard button. Runs Opus position eval immediately."""
        if self._pos_check_running:
            await self.telegram.send_alert("⏳ Position check already running — please wait.")
            return
        pos_state = self.storage.get_position_state()
        if not pos_state.get("has_open"):
            logger.info("Opus position eval triggered (on-demand) — no open position")
            await self.telegram.send_alert("ℹ️ No open position to evaluate.")
            return
        self._pos_check_running = True
        try:
            logger.info("Opus position eval triggered (on-demand)")
            await self.telegram.send_alert("🔍 Running Opus position check…")
        except Exception:
            self._pos_check_running = False
            raise
        try:
            direction = pos_state.get("direction", "LONG")
            logical_direction = "LONG" if direction == "BUY" else ("SHORT" if direction == "SELL" else direction)
            entry = pos_state.get("entry_price", 0)
            sl_level = pos_state.get("stop_level") or 0
            tp_level = pos_state.get("limit_level") or 0
            opened_at_str = pos_state.get("opened_at", "")
            try:
                opened_at = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
                time_open_min = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60
            except Exception:
                time_open_min = 0
            try:
                current_price_data = await asyncio.get_event_loop().run_in_executor(None, self.ig.get_market_info)
                current_price = (
                    current_price_data.get("bid", entry) if logical_direction == "LONG"
                    else current_price_data.get("offer", entry)
                ) if isinstance(current_price_data, dict) else entry
            except Exception:
                current_price = self._current_price or entry
            pnl_pts = (current_price - entry) if logical_direction == "LONG" else (entry - current_price)
            phase = pos_state.get("phase", "initial") or "initial"
            try:
                entry_context = json.loads(pos_state.get("entry_context") or "{}")
            except Exception:
                entry_context = {}
            current_indicators = await self._fetch_current_indicators()

            eval_result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.analyzer.evaluate_open_position(
                    direction=logical_direction,
                    entry=entry,
                    current_price=current_price,
                    stop_loss=sl_level,
                    take_profit=tp_level,
                    phase=phase,
                    time_in_trade_min=time_open_min,
                    recent_prices=list(self._position_price_buffer),
                    lots=pos_state.get("lots", 1.0),
                    entry_context=entry_context,
                    current_indicators=current_indicators,
                ),
            )
            await self.telegram.send_position_eval(
                eval_result=eval_result,
                direction=logical_direction,
                entry=entry,
                current_price=current_price,
                pnl_pts=pnl_pts,
                phase=phase,
                deal_id=pos_state.get("deal_id", ""),
                lots=pos_state.get("lots", 1.0),
            )
        finally:
            self._pos_check_running = False

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
