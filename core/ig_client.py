"""
IG Markets REST API Client.
Handles authentication, position management, price data, and order execution.
Uses the `trading-ig` library as base, with custom extensions for our bot.

Key features:
- Auto-reauthentication on token expiry
- Rate limit awareness (100 trading / 30 non-trading per min)
- Paper mode support (log trades without executing)
- Full error handling with descriptive messages
"""
import time
import logging
from datetime import datetime, timedelta
from typing import Optional
from trading_ig import IGService
from trading_ig.rest import IGException

from config.settings import (
    IG_API_KEY, IG_USERNAME, IG_PASSWORD, IG_ACC_NUMBER, IG_ENV,
    EPIC, CURRENCY, EXPIRY, TRADING_MODE, CONTRACT_SIZE, MARGIN_FACTOR,
)

logger = logging.getLogger(__name__)


class IGClient:
    """Wrapper around trading-ig with auto-auth and error handling."""
    
    def __init__(self):
        self.ig = None
        self.authenticated = False
        self.last_auth_time = None
        self.paper_mode = TRADING_MODE == "paper"
        self._request_count = 0
        self._last_request_time = 0
    
    def connect(self) -> bool:
        """Authenticate with IG Markets API."""
        try:
            self.ig = IGService(
    IG_USERNAME, IG_PASSWORD, IG_API_KEY,
    acc_type=IG_ENV,
    acc_number=IG_ACC_NUMBER,
                use_rate_limiter=True,
            )
            self.ig.create_session()
            self.authenticated = True
            self.last_auth_time = datetime.now()
            logger.info(f"IG API connected ({IG_ENV} mode)")
            return True
        except Exception as e:
            logger.error(f"IG auth failed: {e}")
            self.authenticated = False
            return False
    
    def ensure_connected(self) -> bool:
        """Re-authenticate if session expired (tokens last ~6 hours)."""
        if not self.authenticated:
            return self.connect()
        # Re-auth every 5 hours to be safe
        if self.last_auth_time and datetime.now() - self.last_auth_time > timedelta(hours=5):
            logger.info("Session expiring, re-authenticating...")
            return self.connect()
        return True
    
    # ==========================================
    # MARKET DATA
    # ==========================================
    
    def get_market_info(self) -> Optional[dict]:
        """Get current market snapshot: price, spread, status, dealing rules."""
        if not self.ensure_connected():
            return None
        try:
            info = self.ig.fetch_market_by_epic(EPIC)
            if info is None:
                return None
            snapshot = info.get("snapshot", {})
            dealing = info.get("dealingRules", {})
            instrument = info.get("instrument", {})
            
            return {
                "bid": snapshot.get("bid"),
                "offer": snapshot.get("offer"),
                "high": snapshot.get("high"),
                "low": snapshot.get("low"),
                "spread": (snapshot.get("offer") or 0) - (snapshot.get("bid") or 0),
                "market_status": snapshot.get("marketStatus"),
                "update_time": snapshot.get("updateTime"),
                "min_stop_distance": (dealing.get("minNormalStopOrLimitDistance") or {}).get("value"),
                "trailing_stops_available": dealing.get("trailingStopsPreference") != "NOT_AVAILABLE",
                "currency": instrument.get("currencies", [{}])[0].get("code", CURRENCY),
            }
        except Exception as e:
            logger.error(f"Failed to get market info: {e}")
            return None
    
    def get_prices(self, resolution: str = "HOUR4", num_points: int = 200) -> list[dict]:
        """
        Fetch historical price data.
        
        Resolutions: SECOND, MINUTE, MINUTE_2, MINUTE_3, MINUTE_5, MINUTE_10,
                     MINUTE_15, MINUTE_30, HOUR, HOUR_2, HOUR_3, HOUR_4, DAY, WEEK, MONTH
        """
        if not self.ensure_connected():
            return []
        try:
            result = self.ig.fetch_historical_prices_by_epic(
                epic=EPIC,
                resolution=resolution,
                numpoints=num_points,
            )
            
            prices_df = result.get("prices", None)
            if prices_df is None or prices_df.empty:
                return []
            
            candles = []
            for idx, row in prices_df.iterrows():
                # trading-ig returns multi-level columns: bid/ask/last + OHLC
                # Use 'bid' prices for analysis (what we'd buy at)
                try:
                    candle = {
                        "timestamp": str(idx),
                        "open": float(row.get(("bid", "Open"), row.get(("last", "Open"), 0))),
                        "high": float(row.get(("bid", "High"), row.get(("last", "High"), 0))),
                        "low": float(row.get(("bid", "Low"), row.get(("last", "Low"), 0))),
                        "close": float(row.get(("bid", "Close"), row.get(("last", "Close"), 0))),
                        "volume": float(row.get(("last", "Volume"), 0)),
                    }
                    candles.append(candle)
                except (KeyError, TypeError) as e:
                    logger.warning(f"Skipping malformed candle at {idx}: {e}")
                    continue
            
            logger.info(f"Fetched {len(candles)} candles ({resolution})")
            return candles
            
        except Exception as e:
            logger.error(f"Failed to fetch prices ({resolution}): {e}")
            return []
    
    def get_all_timeframes(self) -> dict:
        """Fetch price data for all 4 analysis timeframes."""
        return {
            "daily": self.get_prices("DAY", 200),
            "h4": self.get_prices("HOUR4", 200),
            "m15": self.get_prices("MINUTE_15", 200),
            "m5": self.get_prices("MINUTE_5", 100),
        }
    
    # ==========================================
    # POSITION MANAGEMENT
    # ==========================================
    
    def open_position(
        self,
        direction: str,
        size: float,
        stop_level: Optional[float] = None,
        stop_distance: Optional[int] = None,
        limit_level: Optional[float] = None,
        limit_distance: Optional[int] = None,
        trailing_stop: bool = False,
        trailing_stop_distance: Optional[int] = None,
        trailing_stop_increment: Optional[int] = None,
        guaranteed_stop: bool = False,
    ) -> Optional[dict]:
        """
        Open a new position.
        
        Returns dict with deal_id, level, stop, limit on success.
        Returns None on failure.
        """
        if self.paper_mode:
            return self._paper_open(direction, size, stop_level, limit_level)
        
        if not self.ensure_connected():
            return None
        
        try:
            deal_ref = self.ig.create_open_position(
                epic=EPIC,
                direction=direction.upper(),
                size=size,
                currency_code=CURRENCY,
                expiry=EXPIRY,
                order_type="MARKET",
                force_open=True,
                guaranteed_stop=guaranteed_stop,
                stop_level=stop_level,
                stop_distance=stop_distance,
                limit_level=limit_level,
                limit_distance=limit_distance,
                trailing_stop=trailing_stop,
                trailing_stop_distance=trailing_stop_distance,
                trailing_stop_increment=trailing_stop_increment,
            )
            
            # Must confirm the deal
            confirmation = self._confirm_deal(deal_ref)
            if not confirmation:
                return None
            
            if confirmation.get("dealStatus") == "REJECTED":
                reason = confirmation.get("reason", "UNKNOWN")
                logger.error(f"Position REJECTED: {reason}")
                return {"error": True, "reason": reason}
            
            result = {
                "deal_id": confirmation.get("dealId"),
                "direction": direction.upper(),
                "size": size,
                "level": confirmation.get("level"),
                "stop_level": confirmation.get("stopLevel"),
                "limit_level": confirmation.get("limitLevel"),
                "status": confirmation.get("dealStatus"),
                "timestamp": datetime.now().isoformat(),
            }
            
            logger.info(
                f"Position OPENED: {direction} {size} lots @ {result['level']} "
                f"SL={result['stop_level']} TP={result['limit_level']}"
            )
            return result
            
        except Exception as e:
            logger.error(f"Failed to open position: {e}")
            return None
    
    def modify_position(
        self,
        deal_id: str,
        stop_level: Optional[float] = None,
        limit_level: Optional[float] = None,
        trailing_stop: Optional[bool] = None,
        trailing_stop_distance: Optional[int] = None,
        trailing_stop_increment: Optional[int] = None,
    ) -> bool:
        """
        Modify stop loss and/or take profit on an open position.
        Returns True on success.
        """
        if self.paper_mode:
            logger.info(f"[PAPER] Modify {deal_id}: SL={stop_level} TP={limit_level}")
            return True
        
        if not self.ensure_connected():
            return False
        
        try:
            self.ig.update_open_position(
                stop_level=stop_level,
                limit_level=limit_level,
                trailing_stop=trailing_stop,
                trailing_stop_distance=trailing_stop_distance,
                trailing_stop_increment=trailing_stop_increment,
                deal_id=deal_id,
            )
            logger.info(f"Position {deal_id} modified: SL={stop_level} TP={limit_level}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to modify position {deal_id}: {e}")
            return False
    
    def close_position(
        self,
        deal_id: str,
        direction: str,
        size: float,
    ) -> Optional[dict]:
        """
        Close an open position.
        Direction should be opposite of the open position.
        """
        if self.paper_mode:
            logger.info(f"[PAPER] Close {deal_id}: {direction} {size}")
            return {"deal_id": deal_id, "status": "CLOSED_PAPER"}
        
        if not self.ensure_connected():
            return None
        
        try:
            # Close direction is opposite of open
            close_direction = "SELL" if direction.upper() == "BUY" else "BUY"
            
            deal_ref = self.ig.close_open_position(
                deal_id=deal_id,
                direction=close_direction,
                size=size,
                order_type="MARKET",
            )
            
            confirmation = self._confirm_deal(deal_ref)
            if confirmation and confirmation.get("dealStatus") == "ACCEPTED":
                logger.info(f"Position {deal_id} CLOSED")
                return confirmation
            
            logger.error(f"Close rejected: {confirmation}")
            return None
            
        except Exception as e:
            logger.error(f"Failed to close position {deal_id}: {e}")
            return None
    
    def get_open_positions(self) -> list[dict]:
        """Get all open positions."""
        if not self.ensure_connected():
            return []
        try:
            positions = self.ig.fetch_open_positions()
            result = []
            for _, pos in positions.iterrows():
                result.append({
                    "deal_id": pos.get("dealId"),
                    "direction": pos.get("direction"),
                    "size": pos.get("dealSize") or pos.get("size"),
                    "level": pos.get("openLevel") or pos.get("level"),
                    "stop_level": pos.get("stopLevel"),
                    "limit_level": pos.get("limitLevel"),
                    "profit": pos.get("profit"),
                    "currency": pos.get("currency"),
                    "created": pos.get("createdDateUTC") or pos.get("createdDate"),
                    "epic": pos.get("epic"),
                })
            # Filter to our instrument
            return [p for p in result if p.get("epic") == EPIC]
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []
    
    def get_account_info(self) -> Optional[dict]:
        """Get account balance and margin info."""
        if not self.ensure_connected():
            return None
        try:
            accounts = self.ig.fetch_accounts()
            for _, acc in accounts.iterrows():
                if str(acc.get("accountId")) == str(IG_ACC_NUMBER):
                    return {
                        "balance": float(acc.get("balance", 0)),
                        "deposit": float(acc.get("deposit", 0)),
                        "profit_loss": float(acc.get("profitLoss", 0)),
                        "available": float(acc.get("available", 0)),
                        "currency": acc.get("currency", CURRENCY),
                    }
            # If exact match not found, return first account
            if not accounts.empty:
                acc = accounts.iloc[0]
                return {
                    "balance": float(acc.get("balance", 0)),
                    "deposit": float(acc.get("deposit", 0)),
                    "profit_loss": float(acc.get("profitLoss", 0)),
                    "available": float(acc.get("available", 0)),
                    "currency": acc.get("currency", CURRENCY),
                }
            return None
        except Exception as e:
            logger.error(f"Failed to fetch account info: {e}")
            return None
    
    def get_transaction_history(self, days: int = 7) -> list[dict]:
        """Get recent transaction history for journaling."""
        if not self.ensure_connected():
            return []
        try:
            from_date = datetime.now() - timedelta(days=days)
            txns = self.ig.fetch_transaction_history(
                trans_type="ALL",
                from_date=from_date.strftime("%Y-%m-%d"),
            )
            return txns.to_dict("records") if hasattr(txns, "to_dict") else []
        except Exception as e:
            logger.error(f"Failed to fetch transactions: {e}")
            return []
    
    # ==========================================
    # HELPERS
    # ==========================================
    
    def _confirm_deal(self, deal_reference, max_retries: int = 3) -> Optional[dict]:
        """Confirm a deal reference. Retries on failure."""
        for attempt in range(max_retries):
            try:
                time.sleep(0.5 * (attempt + 1))  # Backoff
                confirmation = self.ig.fetch_deal_by_deal_reference(deal_reference)
                return confirmation
            except Exception as e:
                logger.warning(f"Deal confirm attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    logger.error(f"Deal confirmation failed after {max_retries} attempts")
                    return None
        return None
    
    def _paper_open(self, direction, size, stop, limit) -> dict:
        """Simulate opening a position in paper mode."""
        market = self.get_market_info()
        price = market["offer"] if direction.upper() == "BUY" else market["bid"]
        
        result = {
            "deal_id": f"PAPER_{int(time.time())}",
            "direction": direction.upper(),
            "size": size,
            "level": price,
            "stop_level": stop,
            "limit_level": limit,
            "status": "OPEN_PAPER",
            "timestamp": datetime.now().isoformat(),
        }
        logger.info(f"[PAPER] Position opened: {result}")
        return result
    
    def calculate_margin(self, size: float, price: Optional[float] = None) -> float:
        """Calculate margin for a given position size."""
        if price is None:
            market = self.get_market_info()
            price = market["offer"] if market else 59500
        return size * CONTRACT_SIZE * price * MARGIN_FACTOR
