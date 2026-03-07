# trading/risk_manager.py — DIGEST
# Purpose: 12-check pre-trade validation + risk-based lot sizing. All checks must pass.
# Updated 2026-03-07: risk-based sizing, multi-position, ATR SL helper, portfolio risk cap.

## class RiskManager
__init__(storage): holds reference to Storage

validate_trade(direction, lots, entry, stop_loss, take_profit, confidence, balance,
               upcoming_events=None, indicators_snapshot=None, is_scalp=False) -> dict
  # direction: "LONG"/"SHORT"/"BUY"/"SELL" (BUY→LONG, SELL→SHORT normalized internally)
  # Returns: {approved: bool, checks: {name: {pass, detail}}, rejection_reason, warnings, summary}
  # Checks in order:
  #   0. sl_tp_direction: LONG needs SL<entry<TP. SHORT needs TP<entry<SL. HARD ABORT.
  #   1. confidence: LONG>=70%, SHORT>=75% (scalp: 60/65)
  #   2. margin: lots*1*entry*0.005/balance <= MAX_MARGIN_PERCENT (5%)
  #   3. risk_reward: effective_rr = (reward-7)/(risk+7) >= 1.5
  #   3B. extreme_day: daily range > 1000pts counter-trend requires confidence >= 85%
  #   4. max_positions: storage.get_open_positions_count() < MAX_OPEN_POSITIONS (3)
  #   4B. portfolio_risk: total open risk (new + existing) <= MAX_PORTFOLIO_RISK_PERCENT (8%) of balance
  #   5. consecutive_losses: >=2 losses + last_loss within 1hr = cooldown
  #   6. daily_loss: abs(daily_loss_today) < balance*1.0 (effectively disabled)
  #   7. weekly_loss: abs(weekly_loss) < balance*0.50
  #   8. event_blackout: no HIGH-impact event within 60min
  #   9. calendar_block: Friday 12:00-16:00 UTC or Friday+keywords or month-end
  #   10. dollar_risk: lots*1*risk_pts <= balance * MAX_RISK_PERCENT (3%) — enforced hard cap
  #   11. lot_size: lots >= 0.01
  #   +  system_active: account.system_active must be True

get_safe_lot_size(balance, price, sl_distance, confidence=None, peak_balance=None) -> float
  # Risk-based sizing: lots = (risk_pct% × balance) / (sl_distance × $1/pt)
  # risk_pct = RISK_PERCENT (2.0%) by default
  # Drawdown protection (if peak_balance provided):
  #   >= 10% drawdown → 0.5% risk; >= 15% → 0.25%; >= 20% → return MIN_LOT_SIZE (halt)
  # Confidence scaling: +15% max at high confidence
  # Hard cap by margin: max lots = (balance * 5%) / (price * 0.005)
  # Always >= MIN_LOT_SIZE (0.01), rounded to 0.01

get_dynamic_sl(atr, setup_type=None, fallback_pts=None) -> float
  # Returns SL distance in points based on ATR × setup-type multiplier.
  # Multipliers: breakout=1.5, momentum/continuation=1.2, vwap/ema9=1.3, bounce/reversal=1.8, default=1.5
  # Always >= SL_FLOOR_PTS (120). Falls back to fallback_pts or DEFAULT_SL_DISTANCE if ATR unavailable.
