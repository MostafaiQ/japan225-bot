# core/ig_client.py — DIGEST (updated 2026-03-04)
# Purpose: IG Markets REST API wrapper. Auth, price data, order management.
# CANDLE CACHING: delta fetches after first full fetch. Disk-backed cache survives restarts.
# DEAL CONFIRMATION: trading_ig may return full dict or string from open/close. Handles both.

## Sentinel
POSITIONS_API_ERROR = object()  # NOT None, NOT []. Check with `is POSITIONS_API_ERROR`.
                                # Means API call failed. Empty list [] means no positions.

## class IGClient
__init__(): reads IG_API_KEY/USERNAME/PASSWORD/ACC_NUMBER/IG_ENV from settings.
            _candle_cache: dict[str, list] — resolution -> cached candles (disk-backed: candle_cache.json)
            _cache_full_fetch_done: dict[str, bool] — tracks which resolutions have been fully fetched
            Disk cache: _load_disk_cache() on init, _save_disk_cache() after each fetch. 4hr max age.
            Streaming state: _lightstreamer_endpoint (str|None), _ls_client, _streaming_price (float|None),
              _streaming_price_ts (float, time.monotonic())

## LIGHTSTREAMER STREAMING (added 2026-03-04)
connect() now saves lightstreamerEndpoint from session response → _lightstreamer_endpoint.

start_streaming() -> bool
  # Connect LightstreamerClient using existing session tokens (CST/XST from self.ig.session.headers).
  # No re-authentication. Subscribes to CHART:{EPIC}:TICK (BID+OFR → mid stored in _streaming_price).
  # Returns True if subscription started. Logs warning and returns False on any error.
  # Safe to call multiple times — calls stop_streaming() first to clean up previous connection.

stop_streaming() -> None
  # Disconnects LightstreamerClient. Clears _ls_client, _streaming_price, _streaming_price_ts.

get_streaming_price() -> float | None
  # Returns mid-price if last tick was < STREAMING_STALE_SECONDS (10s) ago, else None.
  # Callers use None as signal to fall back to REST get_market_info().

connect() -> bool
  # POST /session, sets CST + X-SECURITY-TOKEN. Returns True on success.

ensure_connected() -> bool
  # Checks token validity, reconnects if needed.

get_market_info() -> Optional[dict]
  # GET /markets/{EPIC}. Returns bid, offer, high, low, spread, market_status, etc.
  # Retries 3× on 503 (15s delay each). If all 3 fail: logout → re-auth → one final attempt.
  # Does NOT use data allowance.

## CANDLE CACHING (added 2026-03-02)
get_prices(resolution, num_points) uses a 2-phase caching strategy:
  1. FIRST CALL: full fetch (num_points candles), cached in _candle_cache[resolution]
  2. SUBSEQUENT CALLS: delta fetch (_DELTA_POINTS candles), merged into cache by timestamp
     - Deduplicates by timestamp, updates latest candle (may still be forming)
     - Cache trimmed to 2× num_points to prevent memory growth
  3. TIME-GATING: won't refetch faster than the candle interval
     - 5M: every ~4min, 15M: every ~14min, Daily: once per day
     - Returns cached data between gates (zero API cost)
  4. ALLOWANCE BACKOFF: on "exceeded-account-historical-data-allowance" error,
     sets 1-hour backoff, returns cached data during backoff

_DELTA_POINTS = {MINUTE_5: 2, MINUTE_15: 2, MINUTE_30: 3, HOUR_2: 3, HOUR_4: 3, DAY: 3}
_data_allowance_blocked_until: class-level float timestamp for backoff
_last_delta_ts: class-level dict tracking last delta fetch time per resolution

## Budget (weekly 10,000 data point allowance)
Startup: ~320 points (5M:100 + 15M:220 = 320, Daily fetched sequentially after)
Per day: ~620 points (5M delta every 4min + 15M every 14min + Daily once)
Weekly: ~3,400 points = 34% of allowance. Safe.

## compute_atr (core/indicators.py — standalone, 2026-03-04)
compute_atr(candles, period=14) -> float
  # ATR(14) from list of {high, low, close} dicts. Returns 0.0 if < period+1 candles (not established).
  # Called in _on_trade_confirm_inner() with self.ig._candle_cache["MINUTE_15"] as input.

get_prices(resolution: str, num_points: int) -> list[dict]
  # Pass IG-style strings: "MINUTE_5", "MINUTE_15", "HOUR_4", "DAY"
  # INTERNALLY converted via _PANDAS_RESOLUTIONS to Pandas strings ("15min", "4h", "D")
  # Returns list of {open, high, low, close, volume, timestamp}
  # RATE LIMIT RETRY: catches ApiExceededException, retries up to 3× with 30s/60s backoff

get_all_timeframes() -> dict
  # Returns {daily, h4, m15, m5}

open_position(direction, size, stop_level, limit_level) -> Optional[dict]
  # direction = "BUY" or "SELL". level=None, quote_id=None for MARKET orders.
  # DEAL CONFIRMATION: trading_ig may return full confirmation dict (with dealId) or string deal ref.
  #   If dict with dealId → already confirmed, use directly.
  #   If dict with dealReference → extract string, call _confirm_deal().
  #   If string → call _confirm_deal() as before.

modify_position(deal_id, stop_level, limit_level, trailing_stop, ...) -> bool

close_position(deal_id, direction, size) -> Optional[dict]
  # Same deal confirmation logic as open_position (handles dict or string return).

get_open_positions() -> list[dict] | POSITIONS_API_ERROR

get_account_info() -> Optional[dict]

get_transaction_history(days=7) -> list[dict]
