"""
IG Markets REST API Client.
Handles authentication, position management, price data, and order execution.
Uses the `trading-ig` library as base, with custom extensions for our bot.

Key features:
- Auto-reauthentication on token expiry
- Rate limit awareness (100 trading / 30 non-trading per min)
- Candle caching with delta fetches (saves ~99% data allowance)
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
    EPIC, CURRENCY, EXPIRY, CONTRACT_SIZE, MARGIN_FACTOR,
)

# Sentinel returned by get_open_positions() on API failure.
# Callers must check: if result is POSITIONS_API_ERROR: handle failure
POSITIONS_API_ERROR = object()

logger = logging.getLogger(__name__)


class IGClient:
    """Wrapper around trading-ig with auto-auth and error handling."""
    
    def __init__(self):
        self.ig = None
        self.authenticated = False
        self.last_auth_time = None
        self._request_count = 0
        self._last_request_time = 0
        self._candle_cache: dict[str, list[dict]] = {}  # resolution -> candles
        self._cache_full_fetch_done: dict[str, bool] = {}  # resolution -> True after first full fetch
    
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

    def _check_auth_error(self, e: Exception) -> bool:
        """
        If the exception is an auth failure (401/403/invalid session token),
        mark the session as expired so the next ensure_connected() reconnects.
        Returns True if this was an auth error (caller should retry after reconnect).
        Happens when IG expires the token after 12h inactivity (e.g. over weekend).
        """
        err = str(e)
        if any(code in err for code in ("401", "403", "invalid session", "Invalid session")):
            logger.warning(f"Auth error detected (stale token) — forcing re-auth: {e}")
            self.authenticated = False
            return True
        return False
    
    # ==========================================
    # MARKET DATA
    # ==========================================
    
    def get_market_info(self) -> Optional[dict]:
        """Get current market snapshot: price, spread, status, dealing rules.

        Retries up to 3 times on 503 (IG has a ~30-60s unavailable window at
        session open before the cash CFD becomes tradeable).
        If all 3 fail with 503, logs out, re-authenticates, and tries once more.
        """
        if not self.ensure_connected():
            return None
        all_503 = False
        for attempt in range(3):
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
                if "503" in str(e):
                    if attempt < 2:
                        logger.warning(
                            f"Market info 503 (session startup delay, attempt {attempt + 1}/3) — retrying in 15s"
                        )
                        time.sleep(15)
                        continue
                    all_503 = True
                    break
                self._check_auth_error(e)
                logger.error(f"Failed to get market info: {e}")
                return None

        if all_503:
            logger.warning("Market info 503 persisted after 3 attempts — logging out and re-authenticating")
            try:
                self.ig.logout()
            except Exception:
                pass
            self.authenticated = False
            if not self.connect():
                logger.error("Re-authentication failed after 503 exhaustion")
                return None
            try:
                info = self.ig.fetch_market_by_epic(EPIC)
                if info is None:
                    return None
                snapshot = info.get("snapshot", {})
                dealing = info.get("dealingRules", {})
                instrument = info.get("instrument", {})
                logger.info("Market info recovered after re-authentication")
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
                logger.error(f"Market info still failing after re-auth: {e}")
                return None

        return None
    
    # trading_ig conv_resol() expects Pandas-compatible offset strings (e.g. "15min", "4h", "D").
    # Passing IG-style strings ("MINUTE_15", "DAY") causes ValueError in Pandas 2.x.
    _PANDAS_RESOLUTIONS = {
        "MINUTE_5":  "5min",
        "MINUTE_15": "15min",
        "MINUTE_30": "30min",
        "HOUR_2":    "2h",
        "HOUR_3":    "3h",
        "HOUR_4":    "4h",
        "DAY":       "D",
        "WEEK":      "W",
    }

    _data_allowance_blocked_until: float = 0  # timestamp; class-level backoff
    _last_delta_ts: dict = {}  # resolution -> timestamp of last delta fetch

    # Delta fetch sizes: only fetch new candles after first full fetch
    _DELTA_POINTS = {
        "MINUTE_5": 2,    # 2 candles = 10min buffer (every scan)
        "MINUTE_15": 2,   # 2 candles = 30min buffer (gated to every 15min)
        "MINUTE_30": 3,
        "HOUR_2": 3,
        "HOUR_4": 3,
        "DAY": 3,
    }

    def get_prices(self, resolution: str = "HOUR_4", num_points: int = 200) -> list[dict]:
        """
        Fetch historical price data with caching.

        First call: full fetch (num_points candles). Subsequent calls: delta fetch
        (only new candles), merged with cache. Saves ~99% of data allowance.
        """
        import time as _time
        if _time.time() < IGClient._data_allowance_blocked_until:
            # Return cached data if available during backoff
            cached = self._candle_cache.get(resolution, [])
            return cached[-num_points:] if cached else []
        if not self.ensure_connected():
            return []

        # Determine fetch size: full on first call, delta on subsequent
        # Time-gating: don't refetch faster than the candle interval
        _MIN_DELTA_SECS = {
            "MINUTE_5": 240,     # every ~4min (just under candle close)
            "MINUTE_15": 840,    # every ~14min
            "MINUTE_30": 1740,
            "HOUR_2": 7000,
            "HOUR_4": 14000,
            "DAY": 86400,        # once per day
        }
        if self._cache_full_fetch_done.get(resolution):
            min_interval = _MIN_DELTA_SECS.get(resolution, 300)
            last_ts = IGClient._last_delta_ts.get(resolution, 0)
            if _time.time() - last_ts < min_interval:
                cached = self._candle_cache.get(resolution, [])
                if cached:
                    return cached[-num_points:]
            fetch_n = self._DELTA_POINTS.get(resolution, 5)
        else:
            fetch_n = num_points

        from trading_ig.rest import ApiExceededException

        for attempt in range(3):
            try:
                pandas_res = self._PANDAS_RESOLUTIONS.get(resolution, resolution)
                result = self.ig.fetch_historical_prices_by_epic(
                    epic=EPIC,
                    resolution=pandas_res,
                    numpoints=fetch_n,
                )

                prices_df = result.get("prices", None)
                if prices_df is None or prices_df.empty:
                    cached = self._candle_cache.get(resolution, [])
                    return cached[-num_points:] if cached else []

                new_candles = []
                for idx, row in prices_df.iterrows():
                    try:
                        candle = {
                            "timestamp": str(idx),
                            "open": float(row.get(("bid", "Open"), row.get(("last", "Open"), 0))),
                            "high": float(row.get(("bid", "High"), row.get(("last", "High"), 0))),
                            "low": float(row.get(("bid", "Low"), row.get(("last", "Low"), 0))),
                            "close": float(row.get(("bid", "Close"), row.get(("last", "Close"), 0))),
                            "volume": float(row.get(("last", "Volume"), 0)),
                        }
                        new_candles.append(candle)
                    except (KeyError, TypeError) as e:
                        logger.warning(f"Skipping malformed candle at {idx}: {e}")
                        continue

                IGClient._last_delta_ts[resolution] = _time.time()

                if not self._cache_full_fetch_done.get(resolution):
                    self._candle_cache[resolution] = new_candles
                    self._cache_full_fetch_done[resolution] = True
                    logger.info(f"Fetched {len(new_candles)} candles ({resolution}) [full, cached]")
                else:
                    cached = self._candle_cache.get(resolution, [])
                    existing_ts = {c["timestamp"] for c in cached}
                    added = 0
                    for c in new_candles:
                        if c["timestamp"] not in existing_ts:
                            cached.append(c)
                            existing_ts.add(c["timestamp"])
                            added += 1
                        else:
                            for i in range(len(cached) - 1, -1, -1):
                                if cached[i]["timestamp"] == c["timestamp"]:
                                    cached[i] = c
                                    break
                    max_cache = num_points * 2
                    if len(cached) > max_cache:
                        cached = cached[-max_cache:]
                    self._candle_cache[resolution] = cached
                    if added > 0:
                        logger.info(f"Delta fetch: +{added} new candles ({resolution}), cache={len(cached)}")

                return self._candle_cache[resolution][-num_points:]

            except ApiExceededException:
                if attempt < 2:
                    wait = 30 * (attempt + 1)
                    logger.warning(f"Rate limit on {resolution} (attempt {attempt+1}/3) — retrying in {wait}s")
                    _time.sleep(wait)
                    continue
                logger.error(f"Rate limit on {resolution} after 3 attempts")
                cached = self._candle_cache.get(resolution, [])
                return cached[-num_points:] if cached else []
            except Exception as e:
                self._check_auth_error(e)
                err_str = str(e)
                if "exceeded-account-historical-data-allowance" in err_str:
                    IGClient._data_allowance_blocked_until = _time.time() + 3600
                    logger.error(f"IG data allowance exceeded — using cache for 1 hour")
                    cached = self._candle_cache.get(resolution, [])
                    return cached[-num_points:] if cached else []
                logger.error(f"Failed to fetch prices ({resolution}): {type(e).__name__}: {e}")
                cached = self._candle_cache.get(resolution, [])
                return cached[-num_points:] if cached else []
        return []
    
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
    
    def get_open_positions(self):
        """
        Get all open positions for our instrument.

        Returns:
            list[dict]         - positions list (may be empty if none open)
            POSITIONS_API_ERROR - sentinel if the API call itself failed

        CALLERS MUST CHECK: `if result is POSITIONS_API_ERROR`
        Never treat API_ERROR as "no position open".
        """
        if not self.ensure_connected():
            logger.error("get_open_positions: connection unavailable")
            return POSITIONS_API_ERROR
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
            self._check_auth_error(e)
            logger.error(f"Failed to fetch positions: {e}")
            return POSITIONS_API_ERROR
    
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
        """
        Confirm a deal reference. Retries with exponential backoff.
        IG docs: confirmations can take 2-10 seconds during volatile markets.
        Backoff: 2s, 4s, 8s = 14 seconds total before giving up.

        IMPORTANT: If this returns None, the order may still have been placed
        at IG. Callers must NOT assume the position is closed on None return.
        """
        wait_times = [2, 4, 8]
        for attempt in range(max_retries):
            try:
                time.sleep(wait_times[attempt])
                confirmation = self.ig.fetch_deal_by_deal_reference(deal_reference)
                if confirmation:
                    return confirmation
                # Empty response — IG not ready yet, keep retrying
                logger.warning(f"Deal confirm attempt {attempt + 1}: empty response, retrying...")
            except Exception as e:
                logger.warning(f"Deal confirm attempt {attempt + 1} failed: {e}")
        logger.error(
            f"Deal confirmation failed after {max_retries} attempts for ref {deal_reference}. "
            "Order may still be open at broker — check IG manually."
        )
        return None
    
