"""
Main entry point for the 2-hour scan cycle.
Runs on GitHub Actions every 2 hours during market hours.

Flow:
1. Load state from SQLite (synced via git)
2. Check if system is active and no open position
3. Fetch price data from IG API
4. Calculate indicators
5. Gather web research (news, calendar, VIX, JPY)
6. Run Sonnet analysis
7. If setup found: run Opus confirmation
8. If confirmed: send Telegram alert with CONFIRM button
9. Save scan results, commit back to git
"""
import asyncio
import logging
import sys
from datetime import datetime

from config.settings import LOG_FORMAT, LOG_LEVEL, TRADING_MODE
from core.ig_client import IGClient
from core.indicators import analyze_timeframe, detect_higher_lows
from ai.analyzer import AIAnalyzer, WebResearcher
from trading.risk_manager import RiskManager
from storage.database import Storage
from notifications.telegram_bot import send_standalone_message, send_standalone_trade_alert

logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
logger = logging.getLogger("scan")


def get_current_session() -> str:
    """Determine current trading session based on Kuwait time (UTC+3)."""
    from config.settings import SESSIONS
    # For simplicity, use UTC and add 3 hours
    from datetime import timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=3)))
    current_time = now.strftime("%H:%M")
    
    for session_name, info in SESSIONS.items():
        if info["start"] <= current_time < info["end"]:
            return session_name
    return "off_hours"


async def run_scan():
    """Execute a single scan cycle."""
    scan_start = datetime.now()
    total_cost = 0.0
    
    logger.info("=" * 60)
    logger.info("SCAN STARTED")
    logger.info("=" * 60)
    
    # --- Initialize components ---
    storage = Storage()
    ig = IGClient()
    analyzer = AIAnalyzer()
    researcher = WebResearcher()
    risk = RiskManager(storage)
    
    session = get_current_session()
    logger.info(f"Session: {session}")
    
    # --- Pre-checks ---
    account_state = storage.get_account_state()
    
    if not account_state.get("system_active", True):
        logger.info("System is PAUSED. Skipping scan.")
        await send_standalone_message("Scan skipped - system paused.")
        return
    
    position_state = storage.get_position_state()
    if position_state.get("has_open"):
        logger.info("Position already open. Scan will check for management only.")
        # Position management is handled by the monitor process
        await send_standalone_message(
            f"Scan | {session} | Position open, monitoring active."
        )
        return
    
    # --- Connect to IG ---
    if not ig.connect():
        logger.error("Failed to connect to IG API")
        await send_standalone_message("SCAN FAILED: IG API connection error")
        return
    
    # --- Get market info ---
    market = ig.get_market_info()
    if not market:
        logger.error("Failed to get market info")
        await send_standalone_message("SCAN FAILED: Market data unavailable")
        return
    
    if market.get("market_status") != "TRADEABLE":
        logger.info(f"Market status: {market.get('market_status')}. Skipping.")
        await send_standalone_message(f"Market closed ({market.get('market_status')})")
        return
    
    current_price = market.get("bid", 0)
    logger.info(f"Current price: {current_price}")
    
    # --- Fetch price data ---
    logger.info("Fetching price data across 4 timeframes...")
    all_prices = ig.get_all_timeframes()
    
    if not all_prices.get("m15"):
        logger.error("Failed to fetch 15M price data")
        await send_standalone_message("SCAN FAILED: Price data unavailable")
        return
    
    # --- Calculate indicators ---
    logger.info("Calculating indicators...")
    indicators = {}
    for tf_name, candles in all_prices.items():
        if candles:
            indicators[tf_name] = analyze_timeframe(candles)
    
    # --- Web research ---
    logger.info("Gathering market context...")
    web_research = researcher.research()
    researcher.close()
    
    # --- Load context ---
    recent_scans = storage.get_recent_scans(5)
    market_context = storage.get_market_context()
    
    # --- Sonnet analysis ---
    logger.info("Running Sonnet scan analysis...")
    sonnet_result = analyzer.scan_with_sonnet(
        indicators=indicators,
        recent_scans=recent_scans,
        market_context=market_context,
        web_research=web_research,
    )
    total_cost += sonnet_result.get("_cost", 0)
    
    setup_found = sonnet_result.get("setup_found", False)
    confidence = sonnet_result.get("confidence", 0)
    
    logger.info(f"Sonnet result: setup={'YES' if setup_found else 'NO'}, confidence={confidence}%")
    
    # --- Opus confirmation (only if Sonnet found something) ---
    opus_result = None
    if setup_found and confidence >= 60:  # Only escalate if Sonnet is at least somewhat confident
        logger.info("Setup detected! Running Opus confirmation...")
        opus_result = analyzer.confirm_with_opus(
            indicators=indicators,
            recent_scans=recent_scans,
            market_context=market_context,
            web_research=web_research,
            sonnet_analysis=sonnet_result,
        )
        total_cost += opus_result.get("_cost", 0)
        
        # Opus verdict overrides
        setup_found = opus_result.get("setup_found", False)
        confidence = opus_result.get("confidence", 0)
        logger.info(f"Opus result: setup={'YES' if setup_found else 'NO'}, confidence={confidence}%")
    
    # --- Risk validation (if setup confirmed) ---
    action_taken = "no_trade"
    
    if setup_found and confidence >= 70:
        final = opus_result or sonnet_result
        balance = account_state.get("balance", 0)
        
        # Calculate lot size
        lots = risk.get_safe_lot_size(balance, current_price)
        entry = final.get("entry", current_price)
        sl = final.get("stop_loss", entry - 200)
        tp = final.get("take_profit", entry + 400)
        
        # Risk validation
        validation = risk.validate_trade(
            direction=final.get("direction", "LONG"),
            lots=lots,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            balance=balance,
            upcoming_events=web_research.get("economic_calendar", []),
        )
        
        if validation["approved"]:
            # Calculate trade details for alert
            risk_pts = abs(entry - sl)
            reward_pts = abs(tp - entry)
            rr = reward_pts / risk_pts if risk_pts > 0 else 0
            
            from config.settings import calculate_margin, calculate_profit
            
            trade_alert = {
                "direction": final.get("direction", "LONG"),
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "lots": lots,
                "confidence": confidence,
                "rr_ratio": rr,
                "margin": calculate_margin(lots, entry),
                "dollar_risk": calculate_profit(lots, risk_pts),
                "dollar_reward": calculate_profit(lots, reward_pts),
                "setup_type": final.get("setup_type", "unknown"),
                "session": session,
                "reasoning": final.get("reasoning", ""),
                "timestamp": datetime.now().isoformat(),
                "confidence_breakdown": final.get("confidence_breakdown", {}),
                "ai_analysis": final.get("reasoning", ""),
            }
            
            # Store pending alert and send to Telegram
            storage.set_pending_alert(trade_alert)
            await send_standalone_trade_alert(trade_alert)
            
            action_taken = "alert_sent"
            logger.info(f"TRADE ALERT SENT: {final.get('direction')} @ {entry}")
        else:
            action_taken = f"rejected: {validation['rejection_reason']}"
            logger.info(f"Trade rejected by risk manager: {validation['rejection_reason']}")
            await send_standalone_message(
                f"Setup found but rejected:\n{validation['rejection_reason']}"
            )
    
    # --- Save scan results ---
    rsi_15m = indicators.get("m15", {}).get("rsi")
    scan_record = {
        "timestamp": scan_start.isoformat(),
        "session": session,
        "price": current_price,
        "indicators": indicators,
        "market_context": web_research,
        "analysis": opus_result or sonnet_result,
        "setup_found": setup_found and confidence >= 70,
        "confidence": confidence,
        "action_taken": action_taken,
        "api_cost": total_cost,
    }
    storage.save_scan(scan_record)
    
    # Update market context with trend observation
    storage.update_market_context(
        trend_observation=sonnet_result.get("trend_observation", ""),
        macro_snapshot=web_research,
    )
    
    # --- Scan summary to Telegram ---
    summary = {
        "session": session,
        "price": current_price,
        "rsi": f"{rsi_15m:.1f}" if rsi_15m else "N/A",
        "setup_found": setup_found and confidence >= 70,
    }
    await send_standalone_message(
        f"Scan | {session} | Price {current_price:.0f} | "
        f"RSI {summary['rsi']} | "
        f"{'SETUP FOUND' if summary['setup_found'] else 'No setup'} | "
        f"Cost: ${total_cost:.3f}"
    )
    
    elapsed = (datetime.now() - scan_start).total_seconds()
    logger.info(f"Scan completed in {elapsed:.1f}s | Cost: ${total_cost:.4f}")


def main():
    """Entry point."""
    try:
        asyncio.run(run_scan())
    except KeyboardInterrupt:
        logger.info("Scan interrupted")
    except Exception as e:
        logger.error(f"Scan failed with error: {e}", exc_info=True)
        # Try to send error alert
        try:
            asyncio.run(send_standalone_message(f"SCAN ERROR: {str(e)[:200]}"))
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
