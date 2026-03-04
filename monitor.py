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
    MONITOR_INTERVAL_SECONDS, POSITION_CHECK_EVERY_N_CYCLES,
    SCAN_INTERVAL_SECONDS, OFFHOURS_INTERVAL_SECONDS,
    AI_COOLDOWN_MINUTES, HAIKU_MIN_SCORE, PRICE_DRIFT_ABORT_PTS, SAFETY_CONSECUTIVE_EMPTY,
    calculate_margin, calculate_profit, SPREAD_ESTIMATE, DEFAULT_SL_DISTANCE,
    MIN_CONFIDENCE, MIN_CONFIDENCE_SHORT, BREAKEVEN_BUFFER,
    ADVERSE_SEVERE_PTS, DAILY_EMA200_CANDLES, PRE_SCREEN_CANDLES, AI_ESCALATION_CANDLES,
    MINUTE_5_CANDLES,
)
from core.ig_client import IGClient, POSITIONS_API_ERROR
from core.indicators import analyze_timeframe, detect_setup
from core.session import get_current_session, is_no_trade_day, get_scan_interval
from core.momentum import MomentumTracker, TIER_SEVERE, TIER_NONE
from core.confidence import compute_confidence, format_confidence_breakdown
from trading.exit_manager import ExitManager, ExitPhase
from trading.risk_manager import RiskManager
from storage.database import Storage
from notifications.telegram_bot import TelegramBot
from ai.analyzer import AIAnalyzer, WebResearcher

logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
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
        self._trade_execution_lock = asyncio.Lock()  # C1 fix: prevent double position opening
        self._started_at = datetime.now(timezone.utc)
        self._last_scan_time: datetime | None = None
        self._next_scan_at: datetime | None = None
        self._current_session: str | None = None   # persists across write_state calls
        self._current_price: float | None = None
        self._last_scan_detail: dict = {}  # dashboard: shows last scan outcome
        self._ai_reject_until: datetime | None = None  # U4: short cooldown after Sonnet/Opus rejection
        self._dashboard_force_scan: bool = False  # Set by poll task, consumed by _scanning_cycle
        self._last_opus_decision: dict | None = None  # Track Opus direction consistency
        # Paths for dashboard integration
        self._state_path   = Path(__file__).parent / "storage" / "data" / "bot_state.json"
        self._overrides_path = Path(__file__).parent / "storage" / "data" / "dashboard_overrides.json"
        self._trigger_path   = Path(__file__).parent / "storage" / "data" / "force_scan.trigger"
        self._clear_cd_path  = Path(__file__).parent / "storage" / "data" / "clear_cooldown.trigger"
        self._force_open_pending_path = Path(__file__).parent / "storage" / "data" / "force_open_pending.json"
        self._force_open_trigger_path = Path(__file__).parent / "storage" / "data" / "force_open.trigger"

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
            await self._shutdown()

    # ============================================================
    # PENDING AI RECOVERY (after bot restart during analysis)
    # ============================================================

    async def _recover_pending_ai(self):
        """Check if any AI analyses survived the previous bot restart."""
        data_dir = Path(__file__).parent / "storage" / "data"
        try:
            for pending in sorted(data_dir.glob("ai_pending_*.txt")):
                age = time.time() - pending.stat().st_mtime
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
        entry_timeframe = "15m"

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
            reasoning = setup_long.get("reasoning", "") or setup_short.get("reasoning", "")
            logger.info(f"Pre-screen: no setup (both dirs). {reasoning[:80]}")
            self._last_scan_detail = {"outcome": "no_setup", "price": current_price, "details": reasoning[:80]}
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

        recent_scans_ctx = self.storage.get_recent_scans(15)
        recent_trades_ctx = self.storage.get_recent_trades(10)

        entry_tf = tf_5m if entry_timeframe == "5m" else tf_15m
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
        logger.info(f"AI reasoning: {final_result.get('reasoning', 'N/A')[:200]}")

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
            # Gate: skip Opus for quick-rejects (Sonnet conf < 50%). Saves API cost
            # and prevents Opus from trading on clearly bad setups (Trade #3: 65%, #4: 38%).
            if final_confidence < 50:
                logger.info(
                    f"Sonnet quick-reject (conf {final_confidence}% < 50%). "
                    f"Skipping Opus — setup too weak."
                )
                return SCAN_INTERVAL_SECONDS
            logger.info(
                f"Sonnet rejected (conf {final_confidence}%). "
                f"Launching Opus scalp eval with Sonnet's full analysis..."
            )
            try:
                sonnet_reasoning = final_result.get("reasoning", "")
                opus_context = (
                    f"SONNET ANALYSIS: {sonnet_reasoning[:500]}\n"
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
                        f"{sec.get('reasoning', '')[:200]}"
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
                        ai_reasoning=opus_context[:700],
                        parallel_mode=False,
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
                    }
                    _breakdown_setups = {
                        "bear_flag_breakdown", "breakdown_continuation", "multi_tf_bearish",
                        "dead_cat_bounce_short", "vwap_rejection_short",
                        "high_volume_distribution", "lower_lows_bearish",
                        "bollinger_upper_rejection", "ema50_rejection", "bb_mid_rejection",
                        "overbought_reversal", "ema200_rejection", "pivot_r1_rejection",
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
                    "ai_reasoning": ai_reasoning[:300],
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
        }

        self._last_scan_detail = {"outcome": "trade_alert", "direction": direction, "confidence": final_confidence, "price": current_price, "setup_type": setup.get("type")}
        self.storage.set_pending_alert(trade_alert)
        await self.telegram.send_trade_alert(trade_alert)
        logger.info(f"Trade alert sent: {direction} @ {entry:.0f}, confidence={final_confidence}%")

        # Auto-execute after 2 min if user doesn't respond
        asyncio.ensure_future(self._auto_execute_after_timeout(trade_alert, timeout_secs=120))

        return SCAN_INTERVAL_SECONDS

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

        # --- Get current price (every 2s, all non-position-check cycles) ---
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
        self._write_state()

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

        # --- Adverse move alerts ---
        should_alert, tier, alert_msg = self.momentum_tracker.should_alert()
        if should_alert:
            await self.telegram.send_adverse_alert(alert_msg, tier, deal_id)

            # SEVERE: auto-move SL to breakeven to protect
            if tier == TIER_SEVERE and phase == ExitPhase.INITIAL:
                logger.warning(f"SEVERE adverse move. Auto-protecting with breakeven SL.")
                if logical_direction == "LONG":
                    be_stop = entry + BREAKEVEN_BUFFER
                else:
                    be_stop = entry - BREAKEVEN_BUFFER
                success = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.ig.modify_position(deal_id=deal_id, stop_level=be_stop)
                )
                if success:
                    self.storage.update_position_levels(stop_level=be_stop)
                    self.storage.update_position_phase(deal_id, ExitPhase.BREAKEVEN)
                    logger.info(f"Auto-protected: SL moved to {be_stop:.0f}")

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

        action = self.exit_manager.evaluate_position(position_data)
        if action["action"] != "none":
            logger.info(f"Exit action: {action['action']} — {action['details']}")
            success = await self.exit_manager.execute_action(position_data, action)
            if success and action.get("new_stop") is not None:
                self.storage.update_position_levels(
                    stop_level=action.get("new_stop"),
                    limit_level=action.get("new_limit"),
                )
                if action["action"] == "move_be":
                    self.momentum_tracker.reset_alert_state()

        # Manual trailing (if API trailing not available)
        if phase == ExitPhase.RUNNER:
            trail_action = self.exit_manager.manual_trail_update(position_data)
            if trail_action:
                success = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.ig.modify_position(
                        deal_id=deal_id,
                        stop_level=trail_action["new_stop"],
                    )
                )
                if success:
                    self.storage.update_position_levels(stop_level=trail_action["new_stop"])
                    logger.info(trail_action["details"])

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
        if tp and abs(last_price - tp) < abs(last_price - sl):
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

    # ============================================================
    # AUTO-EXECUTE TIMEOUT (confirmed setups — 2 min no response)
    # ============================================================

    async def _auto_execute_after_timeout(self, alert_data: dict, timeout_secs: int = 120):
        """Wait timeout_secs, then auto-execute if alert is still pending (user didn't respond)."""
        try:
            await asyncio.sleep(timeout_secs)
            pending = self.storage.get_pending_alert()
            if not pending:
                # User already confirmed or rejected — nothing to do
                return
            # Check it's the same alert (match timestamp)
            if pending.get("timestamp") != alert_data.get("timestamp"):
                return
            logger.info(
                f"Auto-executing trade after {timeout_secs}s timeout — user did not respond. "
                f"{alert_data.get('direction')} @ {alert_data.get('entry')}"
            )
            await self.telegram.send_alert(
                f"⏱ <b>Auto-executing</b> — no response after {timeout_secs // 60} min.\n"
                f"{alert_data.get('direction')} @ {alert_data.get('entry'):.0f}"
            )
            self.storage.clear_pending_alert()
            await self._on_trade_confirm(alert_data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Auto-execute timeout error: {e}")

    # ============================================================
    # SCALP AUTO-EXECUTION (Opus-gated near-miss trades)
    # ============================================================

    async def _execute_scalp(
        self, scalp_result: dict, direction: str, setup: dict,
        session: dict, current_price: float, local_conf: dict,
        final_confidence: int,
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

        # --- C2/C3 fix: Lightweight risk re-validation ---
        account = self.storage.get_account_state()
        consec_losses = account.get("consecutive_losses", 0)
        last_loss_time = account.get("last_loss_time")
        from config.settings import MAX_CONSECUTIVE_LOSSES, COOLDOWN_HOURS
        if consec_losses >= MAX_CONSECUTIVE_LOSSES and last_loss_time:
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
                    logger.warning(f"Trade aborted: in loss cooldown until {cooldown_end.strftime('%H:%M')}")
                    await self.telegram.send_alert(f"Trade blocked: loss cooldown until {cooldown_end.strftime('%H:%M')}.")
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

        # --- Place order with distance-based SL/TP ---
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.ig.open_position(
                    direction=ig_direction,
                    size=alert_data.get("lots", 0.01),
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

        trade_num = self.storage.open_trade_atomic(
            trade={
                "deal_id": result.get("deal_id"),
                "direction": direction,
                "lots": alert_data.get("lots"),
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
                "lots": alert_data.get("lots"),
                "entry_price": actual_entry,
                "stop_level": actual_sl,
                "limit_level": actual_tp,
                "confidence": alert_data.get("confidence"),
            }
        )

        # Init momentum tracker
        self.momentum_tracker = MomentumTracker(direction, actual_entry)
        self._position_empty_count = 0

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

    async def _on_force_scan(self):
        """Triggered by /force command. Wakes the main loop immediately."""
        await self.telegram.send_alert("Force scan requested. Running next cycle immediately...")
        self._force_scan_event.set()

    # ============================================================
    # SHUTDOWN
    # ============================================================

    async def _shutdown(self):
        logger.info("Shutting down...")
        try:
            await self.telegram.send_alert("Monitor shutting down. Positions remain protected by broker stops.")
            await self.telegram.stop()
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

    def signal_handler(sig, frame):
        logger.info(f"Signal {sig} received. Stopping...")
        monitor.running = False
        # Wake up any sleeping await (scan interval, force_scan_event, etc.)
        loop.call_soon_threadsafe(monitor._force_scan_event.set)

    def force_scan_handler(sig, frame):
        logger.info("SIGUSR1 received — dashboard force scan. Waking main loop.")
        loop.call_soon_threadsafe(monitor._force_scan_event.set)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGUSR1, force_scan_handler)

    try:
        loop.run_until_complete(monitor.start())
    except KeyboardInterrupt:
        monitor.running = False
    finally:
        loop.close()


if __name__ == "__main__":
    main()
