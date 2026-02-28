# trading/risk_manager.py — DIGEST
# Purpose: 11-check pre-trade validation. All checks must pass. No exceptions.

## class RiskManager
__init__(storage): holds reference to Storage

validate_trade(direction, lots, entry, stop_loss, take_profit, confidence, balance,
               upcoming_events=None) -> dict
  # direction: "LONG"/"SHORT"/"BUY"/"SELL" (BUY→LONG, SELL→SHORT normalized internally)
  # Returns: {approved: bool, checks: {name: {pass, detail}}, rejection_reason, warnings, summary}
  # Checks in order:
  #   0. sl_tp_direction: LONG needs SL<entry<TP. SHORT needs TP<entry<SL. HARD ABORT.
  #   1. confidence: LONG>=70%, SHORT>=75%
  #   2. margin: margin_pct = lots*1*entry*0.005/balance <= 0.50
  #   3. risk_reward: effective_rr = (reward-7)/(risk+7) >= 1.5  (spread=7pts each side)
  #   4. max_positions: storage.get_position_state().has_open_position must be False
  #   5. consecutive_losses: >=2 losses + last_loss within 4hrs = cooldown
  #   6. daily_loss: abs(daily_loss_today) < balance*0.10
  #   7. weekly_loss: abs(weekly_loss) < balance*0.20
  #   8. event_blackout: no HIGH-impact event within 60min
  #   9. calendar_block: Friday 12:00-16:00 UTC or Friday+keywords or month-end
  #   10. dollar_risk: lots*1*risk_pts <= balance*0.10
  #   11. lot_size: lots >= 0.01
  #   +  system_active: account.system_active must be True

get_safe_lot_size(balance, price) -> float
  # max lots under 50% margin. Rounded down to 0.01. Min 0.01.
