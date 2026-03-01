"""
Momentum Tracker — rate-of-change detection for open positions.

Tracks price history and detects adverse moves in three tiers:
  MILD     (30-50 pts against): Alert only
  MODERATE (50-80 pts against): Alert + suggest close
  SEVERE   (80+ pts against):   Auto move SL to breakeven, then alert

Also detects stale data (10+ identical readings during an active session).
"""
import logging
from datetime import datetime
from typing import Optional

from config.settings import (
    ADVERSE_MILD_PTS,
    ADVERSE_MODERATE_PTS,
    ADVERSE_SEVERE_PTS,
    STALE_DATA_THRESHOLD,
    ADVERSE_LOOKBACK_READINGS,
    CONTRACT_SIZE,
)

logger = logging.getLogger(__name__)

TIER_NONE = "none"
TIER_MILD = "mild"
TIER_MODERATE = "moderate"
TIER_SEVERE = "severe"


class MomentumTracker:
    """
    Tracks recent price readings and computes adverse move tiers.

    Designed to be instantiated once and kept alive for the duration
    of a position. Call add_price() every monitoring cycle.
    """

    def __init__(self, direction: str, entry_price: float):
        """
        direction: 'LONG' or 'SHORT'
        entry_price: the price the position was opened at
        """
        self.direction = direction.upper()
        self.entry_price = entry_price
        self._prices: list[dict] = []  # [{price, timestamp}]
        self._last_alerted_tier = TIER_NONE

    def add_price(self, price: float):
        """Record a new price reading."""
        self._prices.append({
            "price": price,
            "timestamp": datetime.now().isoformat(),
        })
        # Keep last 120 readings (1 hour at 30s intervals)
        if len(self._prices) > 120:
            self._prices.pop(0)

    def current_pnl_points(self) -> float:
        """P&L in points from the last recorded price."""
        if not self._prices:
            return 0.0
        current = self._prices[-1]["price"]
        if self.direction == "LONG":
            return current - self.entry_price
        else:
            return self.entry_price - current

    def adverse_move_5min(self) -> float:
        """
        How far price moved AGAINST the position in the last 5 readings
        (typically 5 minutes at 1-min monitoring intervals).

        Returns: positive number = points against us, negative = in our favor.
        """
        if len(self._prices) < 2:
            return 0.0

        lookback = min(ADVERSE_LOOKBACK_READINGS, len(self._prices))
        reference = self._prices[-lookback]["price"]
        current = self._prices[-1]["price"]

        if self.direction == "LONG":
            # Adverse for long = price dropping
            return reference - current
        else:
            # Adverse for short = price rising
            return current - reference

    def get_adverse_tier(self) -> str:
        """
        Classify the current adverse move magnitude.

        Returns one of: TIER_NONE, TIER_MILD, TIER_MODERATE, TIER_SEVERE
        """
        move = self.adverse_move_5min()
        if move <= 0:
            return TIER_NONE
        if move >= ADVERSE_SEVERE_PTS:
            return TIER_SEVERE
        if move >= ADVERSE_MODERATE_PTS:
            return TIER_MODERATE
        if move >= ADVERSE_MILD_PTS:
            return TIER_MILD
        return TIER_NONE

    def should_alert(self) -> tuple[bool, str, str]:
        """
        Decide whether to send an alert based on tier changes.

        Only alerts when the tier WORSENS (mild → moderate → severe).
        Prevents spamming repeated alerts at the same tier.

        Returns: (should_alert: bool, tier: str, message: str)
        """
        current_tier = self.get_adverse_tier()
        move = self.adverse_move_5min()
        pnl = self.current_pnl_points()
        current = self._prices[-1]["price"] if self._prices else 0

        tier_order = [TIER_NONE, TIER_MILD, TIER_MODERATE, TIER_SEVERE]
        current_rank = tier_order.index(current_tier)
        last_rank = tier_order.index(self._last_alerted_tier)

        if current_rank <= last_rank and current_tier != TIER_SEVERE:
            # Tier hasn't worsened (or is recovering) — don't repeat alert
            return False, current_tier, ""

        if current_tier == TIER_NONE:
            # Reset tracker when conditions improve
            self._last_alerted_tier = TIER_NONE
            return False, TIER_NONE, ""

        self._last_alerted_tier = current_tier
        direction_word = "dropped" if self.direction == "LONG" else "risen"

        if current_tier == TIER_MILD:
            msg = (
                f"MILD ADVERSE MOVE\n"
                f"Price {direction_word} {move:.0f} pts in last 5 min\n"
                f"Position P&L: {pnl:+.0f} pts | Price: {current:.0f}"
            )
        elif current_tier == TIER_MODERATE:
            msg = (
                f"MODERATE ADVERSE MOVE\n"
                f"Price {direction_word} {move:.0f} pts in last 5 min\n"
                f"Position P&L: {pnl:+.0f} pts | Price: {current:.0f}\n"
                f"Consider closing position."
            )
        else:  # SEVERE
            msg = (
                f"SEVERE ADVERSE MOVE\n"
                f"Price {direction_word} {move:.0f} pts in last 5 min\n"
                f"Position P&L: {pnl:+.0f} pts | Price: {current:.0f}\n"
                f"Auto-protecting: moving SL to breakeven."
            )

        return True, current_tier, msg

    def is_stale(self) -> bool:
        """
        Returns True if the last STALE_DATA_THRESHOLD readings are all identical.
        Indicates a possible API/market data issue.
        """
        if len(self._prices) < STALE_DATA_THRESHOLD:
            return False
        recent = [p["price"] for p in self._prices[-STALE_DATA_THRESHOLD:]]
        return len(set(recent)) == 1

    def reset_alert_state(self):
        """Reset alert tier tracking (e.g., after a position phase change)."""
        self._last_alerted_tier = TIER_NONE

    def milestone_alert(self) -> Optional[str]:
        """
        Returns a milestone message when position hits +150, +200, +250 etc.
        Returns None if no new milestone reached.
        """
        pnl = self.current_pnl_points()
        milestones = [150, 200, 250, 300, 400, 500]

        for milestone in milestones:
            key = f"_milestone_{milestone}_alerted"
            if pnl >= milestone and not getattr(self, key, False):
                setattr(self, key, True)
                current = self._prices[-1]["price"] if self._prices else 0
                return (
                    f"MILESTONE: +{milestone} pts\n"
                    f"Position P&L: {pnl:+.0f} pts | Price: {current:.0f}"
                )
        return None

    def get_summary(self) -> dict:
        """Return a summary dict for logging/Telegram status."""
        if not self._prices:
            return {}
        return {
            "direction": self.direction,
            "entry": self.entry_price,
            "current": self._prices[-1]["price"],
            "pnl_points": round(self.current_pnl_points(), 1),
            "adverse_5min": round(self.adverse_move_5min(), 1),
            "tier": self.get_adverse_tier(),
            "stale": self.is_stale(),
            "readings": len(self._prices),
        }
