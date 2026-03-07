"""
Risk Manager - The safety net that protects the account.
Every trade must pass through here before execution.
Non-negotiable rules enforced in code, not by willpower.
"""
import logging
from datetime import datetime, timedelta, timezone

from config.settings import (
    MAX_MARGIN_PERCENT, MAX_OPEN_POSITIONS, MAX_PORTFOLIO_RISK_PERCENT,
    MAX_CONSECUTIVE_LOSSES,
    COOLDOWN_HOURS, DAILY_LOSS_LIMIT_PERCENT, WEEKLY_LOSS_LIMIT_PERCENT,
    MIN_CONFIDENCE, MIN_CONFIDENCE_SHORT,
    MIN_SCALP_CONFIDENCE, MIN_SCALP_CONFIDENCE_SHORT,
    EVENT_BLACKOUT_MINUTES, MIN_RR_RATIO,
    BLOCKED_DAYS, MONTHEND_BLACKOUT_DAYS, SPREAD_ESTIMATE,
    CONTRACT_SIZE, MARGIN_FACTOR, MIN_LOT_SIZE,
    FRIDAY_BLACKOUT_START_UTC, FRIDAY_BLACKOUT_END_UTC,
    EXTREME_DAY_RANGE_PTS, EXTREME_DAY_MIN_CONFIDENCE,
    RISK_PERCENT, MAX_RISK_PERCENT,
    DRAWDOWN_REDUCE_10PCT, DRAWDOWN_REDUCE_15PCT, DRAWDOWN_STOP_20PCT,
    SL_ATR_MULTIPLIER_MOMENTUM, SL_ATR_MULTIPLIER_MEAN_REVERSION,
    SL_ATR_MULTIPLIER_BREAKOUT, SL_ATR_MULTIPLIER_VWAP, SL_ATR_MULTIPLIER_DEFAULT,
    SL_FLOOR_PTS, TP_ATR_MULTIPLIER_BASE, TP_ATR_MULTIPLIER_MOMENTUM, TP_FLOOR_PTS,
    DEFAULT_SL_DISTANCE,
)

logger = logging.getLogger(__name__)


class RiskManager:
    """Enforces all risk management rules. If this says no, it's NO."""
    
    def __init__(self, storage):
        self.storage = storage  # Reference to state storage
    
    def validate_trade(
        self,
        direction: str,
        lots: float,
        entry: float,
        stop_loss: float,
        take_profit: float,
        confidence: int,
        balance: float,
        upcoming_events: list[dict] = None,
        indicators_snapshot: dict = None,
        is_scalp: bool = False,
    ) -> dict:
        """
        Run ALL pre-trade checks. Returns pass/fail with reasons.
        Direction must be 'LONG' or 'SHORT' (or 'BUY'/'SELL').

        Returns:
            {
                "approved": bool,
                "checks": {check_name: {"pass": bool, "detail": str}},
                "rejection_reason": str or None,
                "warnings": list[str],
            }
        """
        checks = {}
        warnings = []
        rejection = None

        # Normalise direction to LONG/SHORT
        direction = direction.upper()
        if direction == "BUY":
            direction = "LONG"
        elif direction == "SELL":
            direction = "SHORT"

        # --- CHECK 0: SL/TP Direction Validation (HARD SAFETY) ---
        # For LONG: SL must be below entry, TP must be above entry.
        # For SHORT: SL must be above entry, TP must be below entry.
        sl_tp_valid = True
        if direction == "LONG":
            if not (stop_loss < entry < take_profit):
                sl_tp_valid = False
                rejection = (
                    f"DIRECTION MISMATCH for LONG: need SL({stop_loss:.0f}) < "
                    f"entry({entry:.0f}) < TP({take_profit:.0f}). Aborting."
                )
        elif direction == "SHORT":
            if not (take_profit < entry < stop_loss):
                sl_tp_valid = False
                rejection = (
                    f"DIRECTION MISMATCH for SHORT: need TP({take_profit:.0f}) < "
                    f"entry({entry:.0f}) < SL({stop_loss:.0f}). Aborting."
                )
        checks["sl_tp_direction"] = {
            "pass": sl_tp_valid,
            "detail": "SL/TP positions valid for direction" if sl_tp_valid else rejection,
        }

        # --- CHECK 1: Confidence Floor (direction-specific) ---
        if is_scalp:
            min_conf = MIN_SCALP_CONFIDENCE_SHORT if direction == "SHORT" else MIN_SCALP_CONFIDENCE
        else:
            min_conf = MIN_CONFIDENCE_SHORT if direction == "SHORT" else MIN_CONFIDENCE
        checks["confidence"] = {
            "pass": confidence >= min_conf,
            "detail": f"Confidence {confidence}% vs minimum {min_conf}% ({direction})",
        }
        if not checks["confidence"]["pass"]:
            rejection = rejection or f"Confidence {confidence}% below {min_conf}% floor for {direction}. HARD RULE."
        
        # --- CHECK 2: Margin ---
        margin = lots * CONTRACT_SIZE * entry * MARGIN_FACTOR
        margin_pct = margin / balance if balance > 0 else 1.0
        checks["margin"] = {
            "pass": margin_pct <= MAX_MARGIN_PERCENT,
            "detail": f"Margin ${margin:.2f} = {margin_pct:.1%} of ${balance:.2f} balance (max {MAX_MARGIN_PERCENT:.0%})",
        }
        if not checks["margin"]["pass"]:
            max_lots = int((balance * MAX_MARGIN_PERCENT) / (CONTRACT_SIZE * entry * MARGIN_FACTOR) * 100) / 100
            rejection = f"Margin {margin_pct:.1%} exceeds {MAX_MARGIN_PERCENT:.0%}. Max lots at this price: {max_lots}"
        
        # --- CHECK 3: R:R Ratio ---
        # Spread is paid twice: widens risk on entry, reduces reward on exit.
        risk = abs(entry - stop_loss)
        reward = abs(take_profit - entry)
        rr = reward / risk if risk > 0 else 0
        effective_risk = risk + SPREAD_ESTIMATE
        effective_reward = reward - SPREAD_ESTIMATE
        effective_rr = effective_reward / effective_risk if effective_risk > 0 else 0
        checks["risk_reward"] = {
            "pass": effective_rr >= MIN_RR_RATIO,
            "detail": (
                f"Gross R:R = 1:{rr:.2f} | Effective 1:{effective_rr:.2f} "
                f"(risk +{SPREAD_ESTIMATE}pt, reward -{SPREAD_ESTIMATE}pt spread). Min 1:{MIN_RR_RATIO}"
            ),
        }
        if not checks["risk_reward"]["pass"]:
            rejection = rejection or f"Effective R:R {effective_rr:.2f} below minimum {MIN_RR_RATIO}. Need wider TP or tighter SL."

        # --- CHECK 3B: Extreme Day Volatility Gate (crash or rally, direction-aware) ---
        daily_tf = (indicators_snapshot or {}).get("daily", {})
        daily_high = daily_tf.get("high", 0)
        daily_low = daily_tf.get("low", 0)
        daily_price = daily_tf.get("price", daily_tf.get("close", 0))
        daily_range = daily_high - daily_low if daily_high and daily_low else 0
        extreme_day = daily_range > EXTREME_DAY_RANGE_PTS
        midpoint = (daily_high + daily_low) / 2 if daily_high and daily_low else 0
        is_crash_day = extreme_day and daily_price and daily_price < midpoint   # price in lower half = crash
        is_rally_day = extreme_day and daily_price and daily_price >= midpoint  # price in upper half = rally
        extreme_day_ok = True
        counter_trend = False
        if extreme_day:
            # Only block COUNTER-trend direction at 85% confidence.
            # With-trend trades (SHORT on crash, LONG on rally) use normal threshold.
            counter_trend = (is_crash_day and direction == "LONG") or (is_rally_day and direction == "SHORT")
            if counter_trend and confidence < EXTREME_DAY_MIN_CONFIDENCE:
                extreme_day_ok = False
        day_label = "CRASH DAY" if is_crash_day else ("RALLY DAY" if is_rally_day else "extreme day")
        checks["extreme_day"] = {
            "pass": extreme_day_ok,
            "detail": (
                f"Intraday range {daily_range:.0f}pts"
                + (f" — {day_label}, counter-trend {direction} requires {EXTREME_DAY_MIN_CONFIDENCE}% confidence" if extreme_day and counter_trend else
                   f" — {day_label}, with-trend {direction} allowed" if extreme_day else "")
            ),
        }
        if not extreme_day_ok:
            rejection = rejection or (
                f"{day_label} (range {daily_range:.0f}pts). Counter-trend {direction} needs "
                f"{EXTREME_DAY_MIN_CONFIDENCE}% confidence, got {confidence}%."
            )

        # --- CHECK 4: Max Positions ---
        open_count = self.storage.get_open_positions_count()
        checks["max_positions"] = {
            "pass": open_count < MAX_OPEN_POSITIONS,
            "detail": f"{open_count}/{MAX_OPEN_POSITIONS} positions open",
        }
        if not checks["max_positions"]["pass"]:
            rejection = rejection or f"Max positions reached ({open_count}/{MAX_OPEN_POSITIONS}). Wait for a position to close."

        # --- CHECK 4B: Portfolio Risk Cap ---
        new_trade_risk = lots * abs(entry - stop_loss) * CONTRACT_SIZE
        total_risk = new_trade_risk
        for pos in self.storage.get_open_positions():
            pos_risk = pos["lots"] * abs(pos["entry_price"] - pos["stop_loss"]) * CONTRACT_SIZE
            total_risk += pos_risk
        portfolio_risk_pct = total_risk / balance if balance > 0 else 1.0
        portfolio_ok = portfolio_risk_pct <= MAX_PORTFOLIO_RISK_PERCENT
        checks["portfolio_risk"] = {
            "pass": portfolio_ok,
            "detail": (
                f"Portfolio risk ${total_risk:.2f} = {portfolio_risk_pct:.1%} of ${balance:.2f} "
                f"(max {MAX_PORTFOLIO_RISK_PERCENT:.0%})"
            ),
        }
        if not portfolio_ok:
            rejection = rejection or (
                f"Portfolio risk {portfolio_risk_pct:.1%} would exceed {MAX_PORTFOLIO_RISK_PERCENT:.0%} cap."
            )
        
        # --- CHECK 5: Consecutive Losses ---
        account = self.storage.get_account_state()
        consec_losses = account.get("consecutive_losses", 0)
        last_loss_time = account.get("last_loss_time")
        
        in_cooldown = False
        if consec_losses >= MAX_CONSECUTIVE_LOSSES and last_loss_time:
            cooldown_end = datetime.fromisoformat(last_loss_time) + timedelta(hours=COOLDOWN_HOURS)
            in_cooldown = datetime.now() < cooldown_end
        
        checks["consecutive_losses"] = {
            "pass": not in_cooldown,
            "detail": f"{consec_losses} consecutive losses. Cooldown: {'ACTIVE' if in_cooldown else 'clear'}",
        }
        if not checks["consecutive_losses"]["pass"]:
            from config.settings import DISPLAY_TZ
            cd_display = cooldown_end.replace(tzinfo=timezone.utc).astimezone(DISPLAY_TZ).strftime('%H:%M')
            rejection = rejection or f"{consec_losses} consecutive losses. Cooling down until {cd_display}."
        
        # --- CHECK 6: Daily Loss Limit ---
        daily_loss = account.get("daily_loss_today", 0)
        daily_limit = balance * DAILY_LOSS_LIMIT_PERCENT
        checks["daily_loss"] = {
            "pass": abs(daily_loss) < daily_limit,
            "detail": f"Daily loss ${abs(daily_loss):.2f} vs limit ${daily_limit:.2f}",
        }
        if not checks["daily_loss"]["pass"]:
            rejection = rejection or f"Daily loss limit reached (${abs(daily_loss):.2f}). Done for today."
        
        # --- CHECK 7: Weekly Loss Limit ---
        weekly_loss = account.get("weekly_loss", 0)
        weekly_limit = balance * WEEKLY_LOSS_LIMIT_PERCENT
        checks["weekly_loss"] = {
            "pass": abs(weekly_loss) < weekly_limit,
            "detail": f"Weekly loss ${abs(weekly_loss):.2f} vs limit ${weekly_limit:.2f}",
        }
        if not checks["weekly_loss"]["pass"]:
            rejection = rejection or "Weekly loss limit reached. System paused until Monday."
        
        # --- CHECK 8: Event Blackout ---
        event_clear = True
        if upcoming_events:
            now = datetime.now()
            for event in upcoming_events:
                event_time = event.get("time")
                if isinstance(event_time, str):
                    try:
                        event_dt = datetime.fromisoformat(event_time)
                        minutes_until = (event_dt - now).total_seconds() / 60
                        if 0 < minutes_until < EVENT_BLACKOUT_MINUTES and event.get("impact") == "HIGH":
                            event_clear = False
                            warnings.append(f"High-impact event in {minutes_until:.0f} min: {event.get('name', 'Unknown')}")
                    except ValueError:
                        pass
        
        checks["event_blackout"] = {
            "pass": event_clear,
            "detail": "No high-impact events within blackout window" if event_clear else "Event too close",
        }
        if not checks["event_blackout"]["pass"]:
            rejection = rejection or f"High-impact event within {EVENT_BLACKOUT_MINUTES} minutes. Standing aside."
        
        # --- CHECK 9: Friday / Month-End ---
        import calendar as cal_module
        now = datetime.now()
        now_utc = datetime.now(timezone.utc)
        is_blocked_day = False
        block_reason = ""

        if now.weekday() == 4:  # Friday
            utc_time = now_utc.strftime("%H:%M")
            # Default block during the NFP/high-impact data window (12:00-16:00 UTC)
            in_friday_window = FRIDAY_BLACKOUT_START_UTC <= utc_time <= FRIDAY_BLACKOUT_END_UTC
            if in_friday_window:
                is_blocked_day = True
                block_reason = f"Friday high-impact window ({FRIDAY_BLACKOUT_START_UTC}-{FRIDAY_BLACKOUT_END_UTC} UTC)"
            # Also block if specific events found in calendar (even outside default window)
            if upcoming_events and not is_blocked_day:
                blocked_keywords = BLOCKED_DAYS.get(4, [])
                for event in upcoming_events:
                    if any(kw.lower() in event.get("name", "").lower() for kw in blocked_keywords):
                        is_blocked_day = True
                        block_reason = f"Friday with {event.get('name', 'high-impact event')}"
                        break

        # Month-end check — use trading days (consistent with session.py)
        last_day = cal_module.monthrange(now.year, now.month)[1]
        days_left = last_day - now.day
        trading_days_left = sum(
            1 for i in range(1, days_left + 1)
            if (now.date().replace(day=now.day + i)).weekday() < 5
        )
        is_monthend = trading_days_left <= MONTHEND_BLACKOUT_DAYS
        if is_monthend:
            block_reason = f"Month-end rebalancing ({trading_days_left} trading days to EOM)"

        checks["calendar_block"] = {
            "pass": not is_blocked_day and not is_monthend,
            "detail": block_reason if (is_blocked_day or is_monthend) else "Calendar clear",
        }
        if not checks["calendar_block"]["pass"]:
            rejection = rejection or checks["calendar_block"]["detail"]
        
        # --- CHECK 10: Dollar Risk (enforces MAX_RISK_PERCENT hard cap) ---
        dollar_risk = lots * CONTRACT_SIZE * risk
        dollar_risk_pct = dollar_risk / balance * 100 if balance > 0 else 100
        max_dollar_risk = (MAX_RISK_PERCENT / 100) * balance
        dollar_risk_ok = dollar_risk <= max_dollar_risk
        checks["dollar_risk"] = {
            "pass": dollar_risk_ok,
            "detail": f"Risk ${dollar_risk:.2f} ({dollar_risk_pct:.1f}% of balance, max {MAX_RISK_PERCENT}%)",
        }
        if not dollar_risk_ok:
            rejection = rejection or (
                f"Dollar risk ${dollar_risk:.2f} ({dollar_risk_pct:.1f}%) exceeds {MAX_RISK_PERCENT}% hard cap."
            )
            
        # --- CHECK 11: Lot Size Valid ---
        checks["lot_size"] = {
            "pass": lots >= MIN_LOT_SIZE,
            "detail": f"Lot size {lots} (min {MIN_LOT_SIZE})",
        }
        
        # --- SYSTEM ACTIVE ---
        system_active = account.get("system_active", True)
        checks["system_active"] = {
            "pass": system_active,
            "detail": "System active" if system_active else "System PAUSED by /stop command",
        }
        if not checks["system_active"]["pass"]:
            rejection = rejection or "System is paused. Use /resume to reactivate."
        
        # --- FINAL VERDICT ---
        all_passed = all(c["pass"] for c in checks.values())
        
        return {
            "approved": all_passed,
            "checks": checks,
            "rejection_reason": rejection if not all_passed else None,
            "warnings": warnings,
            "summary": {
                "margin": f"${margin:.2f} ({margin_pct:.1%})",
                "risk_reward": f"1:{rr:.2f}",
                "dollar_risk": f"${lots * CONTRACT_SIZE * risk:.2f}",
                "dollar_reward": f"${lots * CONTRACT_SIZE * reward:.2f}",
            },
        }
    
    def get_safe_lot_size(
        self,
        balance: float,
        price: float,
        sl_distance: float,
        confidence: int = None,
        peak_balance: float = None,
    ) -> float:
        """Calculate lot size using risk-based sizing (RISK_PERCENT % of balance).

        Formula: lots = (risk_pct% × balance) / (sl_distance × $1/pt)
        Hard caps: margin ≤ MAX_MARGIN_PERCENT of balance, risk ≤ MAX_RISK_PERCENT.
        Drawdown protection reduces risk_pct when balance falls below peak.
        """
        # 1. Drawdown protection
        risk_pct = RISK_PERCENT  # default 2.0%
        if peak_balance and peak_balance > 0:
            drawdown = (peak_balance - balance) / peak_balance
            if drawdown >= 0.20 and DRAWDOWN_STOP_20PCT:
                logger.warning(
                    f"Drawdown {drawdown:.1%} ≥ 20% — using minimum lot size (drawdown halt)"
                )
                return MIN_LOT_SIZE
            elif drawdown >= 0.15:
                risk_pct = DRAWDOWN_REDUCE_15PCT   # 0.25%
            elif drawdown >= 0.10:
                risk_pct = DRAWDOWN_REDUCE_10PCT   # 0.5%

        # 2. SL distance floor
        sl_distance = max(sl_distance, SL_FLOOR_PTS)

        # 3. Base dollar risk
        dollar_risk = (risk_pct / 100.0) * balance

        # 4. Confidence scaling: +15% max for high-conviction setups
        if confidence is not None:
            confidence_mult = min(1.15, 1.0 + (confidence - MIN_CONFIDENCE) / 200.0)
            dollar_risk *= confidence_mult

        # 5. Lots from dollar risk
        lots = dollar_risk / (sl_distance * CONTRACT_SIZE)

        # 6. Cap by per-position margin ceiling
        max_lots_margin = (balance * MAX_MARGIN_PERCENT) / (price * MARGIN_FACTOR) if price > 0 else lots
        lots = min(lots, max_lots_margin)

        return max(MIN_LOT_SIZE, round(lots, 2))

    def get_dynamic_sl(
        self,
        atr: float,
        setup_type: str = None,
        fallback_pts: float = None,
    ) -> float:
        """Compute dynamic SL distance (in points) based on ATR and setup type.

        Returns SL distance always ≥ SL_FLOOR_PTS.
        Falls back to fallback_pts (or DEFAULT_SL_DISTANCE) when ATR is unavailable.
        """
        if not atr or atr <= 0:
            return max(fallback_pts or DEFAULT_SL_DISTANCE, SL_FLOOR_PTS)

        if setup_type:
            st = setup_type.lower()
            if "breakout" in st:
                multiplier = SL_ATR_MULTIPLIER_BREAKOUT
            elif "momentum" in st or "continuation" in st:
                multiplier = SL_ATR_MULTIPLIER_MOMENTUM
            elif "vwap" in st or "ema9" in st:
                multiplier = SL_ATR_MULTIPLIER_VWAP
            elif any(x in st for x in ("bounce", "reversal", "oversold", "reversion", "rejection")):
                multiplier = SL_ATR_MULTIPLIER_MEAN_REVERSION
            else:
                multiplier = SL_ATR_MULTIPLIER_DEFAULT
        else:
            multiplier = SL_ATR_MULTIPLIER_DEFAULT

        return max(multiplier * atr, SL_FLOOR_PTS)
