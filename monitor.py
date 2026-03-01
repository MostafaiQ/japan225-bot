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
from datetime import datetime, timezone
from pathlib import Path

from config.settings import (
    LOG_FORMAT, LOG_LEVEL, TRADING_MODE, CONTRACT_SIZE,
    MONITOR_INTERVAL_SECONDS, SCAN_INTERVAL_SECONDS, OFFHOURS_INTERVAL_SECONDS,
    AI_COOLDOWN_MINUTES, PRICE_DRIFT_ABORT_PTS, SAFETY_CONSECUTIVE_EMPTY,
    calculate_margin, calculate_profit, SPREAD_ESTIMATE,
    MIN_CONFIDENCE, MIN_CONFIDENCE_SHORT, BREAKEVEN_BUFFER,
    ADVERSE_SEVERE_PTS, PAPER_TRADING_SESSION_GATE, DAILY_EMA200_CANDLES,
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
        self._position_empty_count = 0  # Consecutive empty responses from IG
        self._force_scan_event = asyncio.Event()
        self._started_at = datetime.now(timezone.utc)
        self._last_scan_time: datetime | None = None
        self._next_scan_in: int | None = None
        self._current_price: float | None = None
        # Paths for dashboard integration
        self._state_path   = Path(__file__).parent / "storage" / "data" / "bot_state.json"
        self._overrides_path = Path(__file__).parent / "storage" / "data" / "dashboard_overrides.json"
        self._trigger_path = Path(__file__).parent / "storage" / "data" / "force_scan.trigger"

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
                "Telegram is online. Bot will retry IG every 5 minutes.\n"
                "Use /status for updates."
            )
            while not connected:
                await asyncio.sleep(300)
                logger.info("Retrying IG connection...")
                if self.ig.connect():
                    connected = True
                    await self.telegram.send_alert("✅ IG reconnected. Bot resuming normal operation.")

        # Startup sync — reconcile DB state with IG reality
        await self.startup_sync()

        self.running = True

        try:
            while self.running:
                await self._main_cycle()
        except asyncio.CancelledError:
            logger.info("Monitor loop cancelled")
        finally:
            await self._shutdown()

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
            self.storage.set_position_open({
                "deal_id": pos.get("deal_id"),
                "direction": pos.get("direction"),
                "lots": pos.get("size"),
                "entry_price": pos.get("level"),
                "stop_level": pos.get("stop_level"),
                "limit_level": pos.get("limit_level"),
                "opened_at": pos.get("created", datetime.now(timezone.utc).isoformat()),
                "confidence": 0,
            })
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
            uptime_secs = int((datetime.now(timezone.utc) - self._started_at).total_seconds())
            h, rem = divmod(uptime_secs, 3600)
            m = rem // 60
            state = {
                "session":        session_name or "—",
                "phase":          phase or ("MONITORING" if self.storage.get_position_state().get("has_open") else "SCANNING"),
                "scanning_paused": self.scanning_paused,
                "last_scan":      self._last_scan_time.isoformat() if self._last_scan_time else None,
                "next_scan_in":   self._next_scan_in,
                "current_price":  self._current_price,
                "uptime":         f"{h}h {m}m",
                "started_at":     self._started_at.isoformat(),
            }
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(self._state_path)
        except Exception as e:
            logger.debug(f"_write_state failed: {e}")

    def _check_force_scan_trigger(self) -> bool:
        """Return True (and delete trigger) if dashboard requested a force scan."""
        try:
            if self._trigger_path.exists():
                self._trigger_path.unlink()
                logger.info("Dashboard force-scan trigger detected.")
                return True
        except Exception:
            pass
        return False

    # ============================================================
    # MAIN CYCLE
    # ============================================================

    async def _main_cycle(self):
        """Dispatches to scanning or monitoring based on position state."""
        try:
            self._reload_overrides()

            if not self.ig.ensure_connected():
                logger.warning("IG reconnection failed. Sleeping 60s.")
                await asyncio.sleep(60)
                return

            pos_state = self.storage.get_position_state()

            if pos_state.get("has_open"):
                await self._monitoring_cycle(pos_state)
                await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
            else:
                interval = await self._scanning_cycle()
                try:
                    await asyncio.wait_for(self._force_scan_event.wait(), timeout=interval)
                    self._force_scan_event.clear()
                except asyncio.TimeoutError:
                    pass

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
            logger.debug("Off-hours — heartbeat only")
            return OFFHOURS_INTERVAL_SECONDS

        # --- Paper trading session gate (Tokyo-only until London/NY validated) ---
        if PAPER_TRADING_SESSION_GATE and not force_scan:
            now_utc = datetime.now(timezone.utc)
            if not (0 <= now_utc.hour < 6):
                logger.debug("Session gate: non-Tokyo session skipped (PAPER_TRADING_SESSION_GATE=True)")
                return OFFHOURS_INTERVAL_SECONDS

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

        # --- Fetch 15M + Daily candles in parallel for pre-screen (2 API calls) ---
        candles_15m, candles_daily = await asyncio.gather(
            asyncio.get_event_loop().run_in_executor(
                None, lambda: self.ig.get_prices("MINUTE_15", 50)
            ),
            asyncio.get_event_loop().run_in_executor(
                None, lambda: self.ig.get_prices("DAY", DAILY_EMA200_CANDLES)
            ),
        )
        if not candles_15m:
            logger.warning("Failed to fetch 15M candles")
            return SCAN_INTERVAL_SECONDS

        tf_15m = analyze_timeframe(candles_15m)
        tf_daily = analyze_timeframe(candles_daily) if candles_daily else {}

        # --- Local pre-screen (zero AI cost) ---
        # detect_setup() requires above_ema200_fallback to be bool (True=bullish, False=bearish).
        # Passing None causes both LONG and SHORT branches to skip → found=False always.
        setup = detect_setup(
            tf_daily=tf_daily,
            tf_4h={},
            tf_15m=tf_15m,
        )

        if not setup["found"]:
            logger.info(f"Pre-screen: no setup. {setup['reasoning'][:80]}")
            return SCAN_INTERVAL_SECONDS

        prescreen_direction = setup["direction"]
        logger.info(f"Pre-screen: {prescreen_direction} setup detected. Checking local confidence...")

        # --- AI cooldown check ---
        if self.storage.is_ai_on_cooldown(AI_COOLDOWN_MINUTES):
            logger.info(f"AI on cooldown ({AI_COOLDOWN_MINUTES} min). Skipping escalation.")
            return SCAN_INTERVAL_SECONDS

        # --- Fetch 4H for full analysis (1 API call; daily already fetched above) ---
        candles_4h = await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.ig.get_prices("HOUR_4", 100)
        )

        tf_4h = analyze_timeframe(candles_4h) if candles_4h else {}
        # tf_daily already set from pre-screen fetch above

        # --- Local confidence score ---
        web_research = {"timestamp": datetime.now().isoformat()}
        try:
            web_research = self.researcher.research()
        except Exception as e:
            logger.warning(f"Web research failed: {e}")

        local_conf = compute_confidence(
            direction=prescreen_direction,
            tf_daily=tf_daily,
            tf_4h=tf_4h,
            tf_15m=tf_15m,
            upcoming_events=web_research.get("economic_calendar", []),
            web_research=web_research,
        )
        logger.info(f"Local confidence: {local_conf['score']}% ({prescreen_direction})")

        # Only escalate to AI if local score is at least 50% (4/8 criteria)
        if local_conf["score"] < 50:
            logger.info(f"Local confidence too low ({local_conf['score']}%). Not escalating.")
            return SCAN_INTERVAL_SECONDS

        # --- Escalate to Sonnet ---
        logger.info(f"Escalating to Sonnet... (local: {local_conf['score']}%)")
        self.storage.set_ai_cooldown(prescreen_direction)

        indicators = {"m15": tf_15m, "h4": tf_4h, "daily": tf_daily}
        recent_scans = self.storage.get_recent_scans(5)
        market_context = self.storage.get_market_context()

        sonnet_result = self.analyzer.scan_with_sonnet(
            indicators=indicators,
            recent_scans=recent_scans,
            market_context=market_context,
            web_research=web_research,
            prescreen_direction=prescreen_direction,
            local_confidence=local_conf,
        )

        sonnet_confidence = sonnet_result.get("confidence", 0)
        sonnet_found = sonnet_result.get("setup_found", False)
        logger.info(f"Sonnet: found={sonnet_found}, confidence={sonnet_confidence}%")

        # Escalate to Opus only if Sonnet >= 70%
        final_result = sonnet_result
        if sonnet_found and sonnet_confidence >= 70:
            logger.info(f"Sonnet at {sonnet_confidence}%. Escalating to Opus...")
            opus_result = self.analyzer.confirm_with_opus(
                indicators=indicators,
                recent_scans=recent_scans,
                market_context=market_context,
                web_research=web_research,
                sonnet_analysis=sonnet_result,
            )
            final_result = opus_result
            logger.info(
                f"Opus: found={opus_result.get('setup_found')}, "
                f"confidence={opus_result.get('confidence')}%"
            )

        # --- Save scan ---
        scan_cost = (
            sonnet_result.get("_cost", 0)
            + (final_result.get("_cost", 0) if final_result is not sonnet_result else 0)
        )
        self.storage.save_scan({
            "timestamp": datetime.now().isoformat(),
            "session": session["name"],
            "price": current_price,
            "indicators": indicators,
            "market_context": web_research,
            "analysis": final_result,
            "setup_found": final_result.get("setup_found", False),
            "confidence": final_result.get("confidence", 0),
            "action_taken": "pending",
            "api_cost": scan_cost,
        })

        # --- Check if setup confirmed and meets threshold ---
        direction = final_result.get("direction", prescreen_direction)
        min_conf = MIN_CONFIDENCE_SHORT if direction == "SHORT" else MIN_CONFIDENCE
        final_confidence = final_result.get("confidence", 0)

        if not final_result.get("setup_found") or final_confidence < min_conf:
            logger.info(
                f"Setup not confirmed by AI. "
                f"Found={final_result.get('setup_found')}, "
                f"Confidence={final_confidence}% (need {min_conf}%)"
            )
            return SCAN_INTERVAL_SECONDS

        # --- Risk validation ---
        balance_info = self.ig.get_account_info()
        balance = balance_info.get("balance", 0) if balance_info else 0
        if balance <= 0:
            logger.error("Could not get account balance")
            return SCAN_INTERVAL_SECONDS

        lots = self.risk.get_safe_lot_size(balance, current_price)
        entry = final_result.get("entry", current_price)
        sl = final_result.get("stop_loss", 0)
        tp = final_result.get("take_profit", 0)

        if not sl or not tp:
            logger.error(f"AI returned null SL or TP: sl={sl}, tp={tp}")
            return SCAN_INTERVAL_SECONDS

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

        self.storage.set_pending_alert(trade_alert)
        await self.telegram.send_trade_alert(trade_alert)
        logger.info(f"Trade alert sent: {direction} @ {entry:.0f}, confidence={final_confidence}%")

        return SCAN_INTERVAL_SECONDS

    # ============================================================
    # MONITORING MODE
    # ============================================================

    async def _monitoring_cycle(self, pos_state: dict):
        """Position monitoring — runs every 60 seconds when a trade is open."""
        deal_id = pos_state.get("deal_id")
        direction = (pos_state.get("direction") or "BUY").upper()
        logical_direction = "LONG" if direction in ("BUY", "LONG") else "SHORT"
        entry = float(pos_state.get("entry_price") or 0)
        phase = pos_state.get("phase", ExitPhase.INITIAL)

        # --- Check if position still exists on IG ---
        live_positions = await asyncio.get_event_loop().run_in_executor(
            None, self.ig.get_open_positions
        )

        if live_positions is POSITIONS_API_ERROR:
            # API failed — do NOT treat as position closed
            logger.warning("IG API error checking positions. Skipping cycle.")
            await self.telegram.send_alert(
                "WARNING: IG API error while checking position. Will retry."
            )
            self._position_empty_count = 0  # Reset — this wasn't an empty response
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

        # Position confirmed open — reset counter
        self._position_empty_count = 0

        # --- Get current price ---
        market = await asyncio.get_event_loop().run_in_executor(
            None, self.ig.get_market_info
        )
        if not market:
            logger.warning("Failed to get market price for monitoring")
            return

        current_price = market.get("bid", 0) if logical_direction == "LONG" else market.get("offer", 0)

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

        # Get last known price
        last_price = 0
        if self.momentum_tracker and self.momentum_tracker._prices:
            last_price = self.momentum_tracker._prices[-1]["price"]

        pnl_points = (
            last_price - entry if logical_direction == "LONG" else entry - last_price
        )
        pnl_dollars = pnl_points * float(pos_state.get("lots") or 0) * CONTRACT_SIZE

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
    # TRADE EXECUTION (called by Telegram on CONFIRM)
    # ============================================================

    async def _on_trade_confirm(self, alert_data: dict):
        """Execute trade after user confirms via Telegram."""
        logger.info("Trade CONFIRMED by user. Executing...")

        direction = alert_data.get("direction", "LONG")
        ig_direction = "BUY" if direction == "LONG" else "SELL"
        analyzed_entry = float(alert_data.get("entry", 0))

        # --- Re-fetch current price — check for drift ---
        market = await asyncio.get_event_loop().run_in_executor(
            None, self.ig.get_market_info
        )
        if not market:
            await self.telegram.send_alert("Execution failed: could not fetch current price.")
            return

        current_price = market.get("offer") if direction == "LONG" else market.get("bid")
        price_drift = abs(current_price - analyzed_entry)

        if price_drift > PRICE_DRIFT_ABORT_PTS:
            await self.telegram.send_alert(
                f"Trade ABORTED: price moved {price_drift:.0f} pts during analysis "
                f"(analyzed at {analyzed_entry:.0f}, now {current_price:.0f}).\n"
                f"Max allowed drift: {PRICE_DRIFT_ABORT_PTS} pts. Re-scanning."
            )
            logger.warning(f"Trade aborted: price drift {price_drift:.0f}pts")
            self.storage.clear_pending_alert()
            return

        # --- Place order ---
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.ig.open_position(
                    direction=ig_direction,
                    size=alert_data.get("lots", 0.01),
                    stop_level=alert_data.get("sl"),
                    limit_level=alert_data.get("tp"),
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

        await self.telegram.send_alert(
            f"Trade #{trade_num} OPENED\n"
            f"{direction} {alert_data.get('lots')} lots @ {actual_entry:.0f}\n"
            f"SL: {actual_sl:.0f} | TP: {actual_tp:.0f}\n"
            f"Drift from analysis: {price_drift:.0f} pts\n"
            f"Monitoring active."
        )
        logger.info(f"Trade #{trade_num} opened: {direction} @ {actual_entry:.0f}")

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

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        loop.run_until_complete(monitor.start())
    except KeyboardInterrupt:
        monitor.running = False
    finally:
        loop.close()


if __name__ == "__main__":
    main()
