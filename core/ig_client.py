"""
IG Markets REST API Client.
Handles authentication, position management, price data, and order execution.
Uses the `trading-ig` library as base, with custom extensions for our bot.

Key features:
- Auto-reauthentication on token expiry
- Rate limit awareness (100 trading / 30 non-trading per min)
- Candle caching with delta fetches (saves ~99% data allowance)
- Disk-backed candle cache survives restarts
- Full error handling with descriptive messages
"""
import json as _json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from trading_ig import IGService
from trading_ig.rest import IGException

from config.settings import (
    IG_API_KEY, IG_USERNAME, IG_PASSWORD, IG_ACC_NUMBER, IG_ENV,
    EPIC, CURRENCY, EXPIRY, CONTRACT_SIZE, MARGIN_FACTOR,
    STORAGE_DIR, STREAMING_STALE_SECONDS,
)

_CACHE_FILE = STORAGE_DIR / "candle_cache.json"

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
        self._load_disk_cache()
        # Lightstreamer streaming state
        self._lightstreamer_endpoint: str | None = None
        self._ls_client = None
        self._streaming_price: float | None = None
        self._streaming_price_ts: float = 0.0  # time.monotonic() of last tick
        # Tick density (CHART:5MINUTE CONS_TICK_COUNT) — order flow proxy
        self._tick_candles: list = []  # last 15 5M candles: {tick_count, range, density}

    def _load_disk_cache(self) -> None:
        """Load candle cache from disk (survives restarts)."""
        try:
            if _CACHE_FILE.exists():
                data = _json.loads(_CACHE_FILE.read_text())
                age_s = time.time() - data.get("saved_at", 0)
                if age_s < 14400:  # 4 hours max age
                    self._candle_cache = data.get("candles", {})
                    for res in self._candle_cache:
                        self._cache_full_fetch_done[res] = True
                    logger.info(f"Loaded disk cache ({len(self._candle_cache)} resolutions, {age_s/60:.0f}min old)")
                else:
                    logger.info("Disk cache too old (>4h), starting fresh")
        except Exception as e:
            logger.warning(f"Failed to load disk cache: {e}")

    def _save_disk_cache(self) -> None:
        """Persist candle cache to disk."""
        try:
            data = {"saved_at": time.time(), "candles": self._candle_cache}
            _CACHE_FILE.write_text(_json.dumps(data))
        except Exception as e:
            logger.warning(f"Failed to save disk cache: {e}")
    
    def connect(self) -> bool:
        """Authenticate with IG Markets API."""
        try:
            self.ig = IGService(
    IG_USERNAME, IG_PASSWORD, IG_API_KEY,
    acc_type=IG_ENV,
    acc_number=IG_ACC_NUMBER,
                use_rate_limiter=True,
            )
            session_resp = self.ig.create_session()
            self._lightstreamer_endpoint = (session_resp or {}).get("lightstreamerEndpoint")
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
        Also catches empty error messages — trading_ig swallows the 401 details
        during its internal token refresh, leaving an empty exception.
        """
        err = str(e).strip()
        if not err or any(code in err for code in ("401", "403", "invalid session", "Invalid session", "security-token")):
            logger.warning(f"Auth error detected (stale token) — forcing re-auth: {e!r}")
            self.authenticated = False
            return True
        return False
    
    # ==========================================
    # LIGHTSTREAMER STREAMING
    # ==========================================

    def start_streaming(self) -> bool:
        """Start Lightstreamer price tick subscription.
        Uses existing REST session tokens — no re-authentication needed.
        Returns True if subscription started, False if streaming unavailable.
        Fallback: caller uses get_streaming_price() → None → REST polling.
        """
        try:
            from lightstreamer.client import LightstreamerClient, Subscription, SubscriptionListener

            if not self._lightstreamer_endpoint:
                logger.warning("No lightstreamer endpoint saved — streaming unavailable")
                return False

            # Stop any existing connection cleanly
            self.stop_streaming()

            cst = self.ig.session.headers.get("CST", "")
            xst = self.ig.session.headers.get("X-SECURITY-TOKEN", "")
            if not cst or not xst:
                logger.warning("No session tokens for streaming — REST polling will be used")
                return False

            ls_password = f"CST-{cst}|XST-{xst}"
            self._ls_client = LightstreamerClient(self._lightstreamer_endpoint, None)
            self._ls_client.connectionDetails.setUser(IG_ACC_NUMBER)
            self._ls_client.connectionDetails.setPassword(ls_password)
            self._ls_client.connect()

            client_ref = self

            class _TickListener(SubscriptionListener):
                def onItemUpdate(self, update):
                    fields = update.getChangedFields()
                    bid = fields.get("BID")
                    ofr = fields.get("OFR")
                    if bid and ofr:
                        try:
                            mid = (float(bid) + float(ofr)) / 2
                            client_ref._streaming_price = mid
                            client_ref._streaming_price_ts = time.monotonic()
                        except (ValueError, TypeError):
                            pass

            sub = Subscription(
                mode="DISTINCT",
                items=[f"CHART:{EPIC}:TICK"],
                fields=["BID", "OFR"],
            )
            sub.addListener(_TickListener())
            self._ls_client.subscribe(sub)

            # ── CHART:5MINUTE — tick density / order flow proxy ───────────────
            class _5MinListener(SubscriptionListener):
                def onItemUpdate(self, update):
                    fields = update.getChangedFields()
                    tc_str  = fields.get("CONS_TICK_COUNT")
                    bh_str  = fields.get("BID_HIGH")
                    bl_str  = fields.get("BID_LOW")
                    oh_str  = fields.get("OFR_HIGH")
                    ol_str  = fields.get("OFR_LOW")
                    utm_str = fields.get("UTM")
                    if not (tc_str and bh_str and bl_str):
                        return
                    try:
                        tc    = int(float(tc_str))
                        mid_h = (float(bh_str) + float(oh_str)) / 2 if oh_str else float(bh_str)
                        mid_l = (float(bl_str) + float(ol_str)) / 2 if ol_str else float(bl_str)
                        rng   = round(mid_h - mid_l, 1)
                        density = round(tc / rng, 2) if rng > 1 else 0.0
                        client_ref._tick_candles.append({
                            "tick_count": tc,
                            "range": rng,
                            "density": density,
                            "ts": int(utm_str) if utm_str else None,
                        })
                        client_ref._tick_candles = client_ref._tick_candles[-15:]
                    except (ValueError, TypeError):
                        pass

            sub5 = Subscription(
                mode="MERGE",
                items=[f"CHART:{EPIC}:5MINUTE"],
                fields=["CONS_TICK_COUNT", "BID_HIGH", "BID_LOW", "OFR_HIGH", "OFR_LOW", "UTM"],
            )
            sub5.addListener(_5MinListener())
            self._ls_client.subscribe(sub5)
            logger.info("Lightstreamer streaming started (CHART tick + 5MIN tick density)")
            return True
        except Exception as e:
            logger.warning(f"Streaming start failed (will use REST polling): {e}")
            self._ls_client = None
            return False

    def stop_streaming(self) -> None:
        """Disconnect Lightstreamer and clear streaming state."""
        try:
            if self._ls_client:
                self._ls_client.disconnect()
        except Exception:
            pass
        finally:
            self._ls_client = None
            self._streaming_price = None
            self._streaming_price_ts = 0.0

    def get_streaming_price(self) -> float | None:
        """Return streaming mid-price if fresh, else None (caller uses REST fallback)."""
        if self._streaming_price and (time.monotonic() - self._streaming_price_ts) < STREAMING_STALE_SECONDS:
            return self._streaming_price
        return None

    def get_tick_density(self, recent_n: int = 3) -> dict:
        """
        Order flow proxy from CHART:5MINUTE CONS_TICK_COUNT.
        HIGH_ABSORPTION: high ticks + small range = contested level, price going nowhere.
        HIGH_EXPANSION:  high ticks + large range = institutional conviction behind the move.
        NORMAL: typical activity.
        Returns {"signal": str|None, "latest": float|None, "candles": list}
        """
        if not self._tick_candles:
            return {"signal": None, "latest": None, "candles": []}
        recent = self._tick_candles[-recent_n:]
        avg_ticks = sum(c["tick_count"] for c in recent) / len(recent)
        avg_range = sum(c["range"] for c in recent) / len(recent)
        latest    = recent[-1]["density"] if recent else None
        if avg_ticks > 250 and avg_range < 80:
            signal = "HIGH_ABSORPTION"
        elif avg_ticks > 200 and avg_range > 150:
            signal = "HIGH_EXPANSION"
        else:
            signal = "NORMAL"
        return {"signal": signal, "latest": latest, "candles": recent}

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
                if self._check_auth_error(e):
                    logger.info("Reconnecting after auth error on get_market_info...")
                    if self.connect():
                        try:
                            info = self.ig.fetch_market_by_epic(EPIC)
                            if info is None:
                                return None
                            snapshot = info.get("snapshot", {})
                            dealing = info.get("dealingRules", {})
                            instrument = info.get("instrument", {})
                            logger.info("Market info recovered after re-auth")
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
                        except Exception as e2:
                            logger.error(f"Market info still failed after re-auth: {e2}")
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
            "DAY": 3600,         # hourly — current candle H/L must update intraday
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

                self._save_disk_cache()
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

    def get_trade_history_buffer(self, since: datetime) -> list:
        """
        Fetch 1-min OHLC candles from `since` until now and return a flat
        price list (O,H,L,C per candle) suitable for _position_price_buffer.
        Pass `opened_at` for full history, or last-saved timestamp for gap-fill.
        Returns [] on any failure so caller can fall back gracefully.
        """
        from math import ceil

        try:
            now = datetime.utcnow()
            if since.tzinfo is not None:
                now = datetime.now(since.tzinfo)
            mins = (now - since).total_seconds() / 60
            mins = min(max(1, ceil(mins)), 360)  # cap at 6hr = 360 candles

            result = self.ig.fetch_historical_prices_by_epic(
                epic=EPIC,
                resolution="1min",
                numpoints=mins + 5,  # +5 buffer for timing edges
            )
            prices_df = result.get("prices", None)
            if prices_df is None or prices_df.empty:
                return []

            flat = []
            for _, row in prices_df.iterrows():
                try:
                    o = float(row.get(("bid", "Open"),  row.get(("last", "Open"),  0)))
                    h = float(row.get(("bid", "High"),  row.get(("last", "High"),  0)))
                    l = float(row.get(("bid", "Low"),   row.get(("last", "Low"),   0)))
                    c = float(row.get(("bid", "Close"), row.get(("last", "Close"), 0)))
                    if c > 0:
                        flat.extend([o, h, l, c])
                except (KeyError, TypeError):
                    continue

            logger.info(f"Fetched {mins} MINUTE candles since {since.strftime('%H:%M')} → {len(flat)} price points")
            return flat
        except Exception as e:
            logger.warning(f"get_trade_history_buffer failed: {e}")
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
                level=None,
                quote_id=None,
                force_open=True,
                guaranteed_stop=guaranteed_stop,
                stop_level=stop_level,
                stop_distance=stop_distance,
                limit_level=limit_level,
                limit_distance=limit_distance,
                trailing_stop=trailing_stop,
                trailing_stop_increment=trailing_stop_increment,
            )

            # trading_ig may return the full confirmation dict or just a deal reference string
            if isinstance(deal_ref, dict) and "dealId" in deal_ref:
                # Already confirmed by the library — use directly
                logger.info(f"Deal already confirmed by library: {deal_ref.get('dealId')}")
                confirmation = deal_ref
            elif isinstance(deal_ref, dict) and "dealReference" in deal_ref:
                # Dict but needs confirmation via dealReference
                confirmation = self._confirm_deal(deal_ref["dealReference"])
            else:
                # String deal reference — confirm as before
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
                epic=EPIC,
                expiry="-",
                level=None,
                order_type="MARKET",
                quote_id=None,
                size=size,
            )

            # trading_ig may return full confirmation dict or just a deal reference string
            if isinstance(deal_ref, dict) and "dealStatus" in deal_ref:
                confirmation = deal_ref
            elif isinstance(deal_ref, dict) and "dealReference" in deal_ref:
                confirmation = self._confirm_deal(deal_ref["dealReference"])
            else:
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
    
