"""
Risk Manager - The safety net that protects the account.
Every trade must pass through here before execution.
Non-negotiable rules enforced in code, not by willpower.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from config.settings import (
    MAX_MARGIN_PERCENT, MAX_OPEN_POSITIONS, MAX_CONSECUTIVE_LOSSES,
    COOLDOWN_HOURS, DAILY_LOSS_LIMIT_PERCENT, WEEKLY_LOSS_LIMIT_PERCENT,
    MIN_CONFIDENCE, EVENT_BLACKOUT_MINUTES, MIN_RR_RATIO,
    BLOCKED_DAYS, MONTHEND_BLACKOUT_DAYS, SPREAD_ESTIMATE,
    CONTRACT_SIZE, MARGIN_FACTOR, MIN_LOT_SIZE,
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
    ) -> dict:
        """
        Run ALL pre-trade checks. Returns pass/fail with reasons.
        
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
        
        # --- CHECK 1: Confidence Floor ---
        checks["confidence"] = {
            "pass": confidence >= MIN_CONFIDENCE,
            "detail": f"Confidence {confidence}% vs minimum {MIN_CONFIDENCE}%",
        }
        if not checks["confidence"]["pass"]:
            rejection = f"Confidence {confidence}% below {MIN_CONFIDENCE}% floor. HARD RULE."
        
        # --- CHECK 2: Margin ---
        margin = lots * CONTRACT_SIZE * entry * MARGIN_FACTOR
        margin_pct = margin / balance if balance > 0 else 1.0
        checks["margin"] = {
            "pass": margin_pct <= MAX_MARGIN_PERCENT,
            "detail": f"Margin ${margin:.2f} = {margin_pct:.1%} of ${balance:.2f} balance (max {MAX_MARGIN_PERCENT:.0%})",
        }
        if not checks["margin"]["pass"]:
            max_lots = int((balance * MAX_MARGIN_PERCENT) / (CONTRACT_SIZE * entry * MARGIN_FACTOR) * 100) / 100
            rejection = f"Margin {margin_pct:.1%} exceeds 50%. Max lots at this price: {max_lots}"
        
        # --- CHECK 3: R:R Ratio ---
        risk = abs(entry - stop_loss)
        reward = abs(take_profit - entry)
        rr = reward / risk if risk > 0 else 0
        # Adjust for spread
        effective_reward = reward - SPREAD_ESTIMATE
        effective_rr = effective_reward / risk if risk > 0 else 0
        checks["risk_reward"] = {
            "pass": effective_rr >= MIN_RR_RATIO,
            "detail": f"R:R = 1:{rr:.2f} (effective 1:{effective_rr:.2f} after spread). Min 1:{MIN_RR_RATIO}",
        }
        if not checks["risk_reward"]["pass"]:
            rejection = rejection or f"R:R {effective_rr:.2f} below minimum {MIN_RR_RATIO}. Need wider TP or tighter SL."
        
        # --- CHECK 4: Max Positions ---
        state = self.storage.get_position_state()
        open_count = 1 if state.get("has_open_position") else 0
        checks["max_positions"] = {
            "pass": open_count < MAX_OPEN_POSITIONS,
            "detail": f"{open_count} open positions (max {MAX_OPEN_POSITIONS})",
        }
        if not checks["max_positions"]["pass"]:
            rejection = rejection or "Already have an open position. One at a time."
        
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
            rejection = rejection or f"{consec_losses} consecutive losses. Cooling down until {cooldown_end.strftime('%H:%M')}."
        
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
        now = datetime.now()
        is_blocked_day = False
        if now.weekday() in BLOCKED_DAYS:
            # Need to check if specific events are today
            if upcoming_events:
                blocked_keywords = BLOCKED_DAYS[now.weekday()]
                for event in upcoming_events:
                    if any(kw.lower() in event.get("name", "").lower() for kw in blocked_keywords):
                        is_blocked_day = True
                        break
        
        # Month-end check
        import calendar
        last_day = calendar.monthrange(now.year, now.month)[1]
        days_until_monthend = last_day - now.day
        is_monthend = days_until_monthend < MONTHEND_BLACKOUT_DAYS
        
        checks["calendar_block"] = {
            "pass": not is_blocked_day and not is_monthend,
            "detail": (
                f"Friday with blocked data" if is_blocked_day else
                f"Month-end rebalancing zone" if is_monthend else
                "Calendar clear"
            ),
        }
        if not checks["calendar_block"]["pass"]:
            rejection = rejection or checks["calendar_block"]["detail"]
        
        # --- CHECK 10: Dollar Risk ---
        dollar_risk = lots * CONTRACT_SIZE * risk
        max_dollar_risk = balance * 0.10  # Never risk more than 10% on one trade
        checks["dollar_risk"] = {
            "pass": dollar_risk <= max_dollar_risk,
            "detail": f"Risk ${dollar_risk:.2f} on this trade (max ${max_dollar_risk:.2f})",
        }
        if not checks["dollar_risk"]["pass"]:
            rejection = rejection or f"Dollar risk ${dollar_risk:.2f} exceeds 10% of balance."
            
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
    
    def get_safe_lot_size(self, balance: float, price: float) -> float:
        """Calculate the maximum safe lot size given current balance."""
        max_margin = balance * MAX_MARGIN_PERCENT
        margin_per_lot = CONTRACT_SIZE * price * MARGIN_FACTOR
        max_lots = max_margin / margin_per_lot
        return max(MIN_LOT_SIZE, int(max_lots * 100) / 100)
