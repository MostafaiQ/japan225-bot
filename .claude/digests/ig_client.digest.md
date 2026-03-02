# core/ig_client.py — DIGEST (updated 2026-03-02)
# Purpose: IG Markets REST API wrapper. Auth, price data, order management.
# CANDLE CACHING: delta fetches after first full fetch. Saves ~99% of data allowance.

## Sentinel
POSITIONS_API_ERROR = object()  # NOT None, NOT []. Check with `is POSITIONS_API_ERROR`.
                                # Means API call failed. Empty list [] means no positions.

## class IGClient
__init__(): reads IG_API_KEY/USERNAME/PASSWORD/ACC_NUMBER/IG_ENV from settings.
            _candle_cache: dict[str, list] — resolution -> cached candles
            _cache_full_fetch_done: dict[str, bool] — tracks which resolutions have been fully fetched

connect() -> bool
  # POST /session, sets CST + X-SECURITY-TOKEN. Returns True on success.

ensure_connected() -> bool
  # Checks token validity, reconnects if needed.

get_market_info() -> Optional[dict]
  # GET /markets/{EPIC}. Returns bid, offer, high, low, spread, market_status, etc.
  # Retries 3× on 503 (15s delay each). Does NOT use data allowance.

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

get_prices(resolution: str, num_points: int) -> list[dict]
  # Pass IG-style strings: "MINUTE_5", "MINUTE_15", "HOUR_4", "DAY"
  # INTERNALLY converted via _PANDAS_RESOLUTIONS to Pandas strings ("15min", "4h", "D")
  # Returns list of {open, high, low, close, volume, timestamp}

get_all_timeframes() -> dict
  # Returns {daily, h4, m15, m5}

open_position(direction, size, stop_level, limit_level) -> Optional[dict]
  # direction = "BUY" or "SELL". Paper mode: _paper_open() — no API call.

modify_position(deal_id, stop_level, limit_level, trailing_stop, ...) -> bool

close_position(deal_id, direction, size) -> Optional[dict]

get_open_positions() -> list[dict] | POSITIONS_API_ERROR

get_account_info() -> Optional[dict]

get_transaction_history(days=7) -> list[dict]
