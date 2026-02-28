# core/ig_client.py — DIGEST
# Purpose: IG Markets REST API wrapper. Auth, price data, order management.

## Sentinel
POSITIONS_API_ERROR = object()  # NOT None, NOT []. Check with `is POSITIONS_API_ERROR`.
                                # Means API call failed. Empty list [] means no positions.

## class IGClient
__init__(): reads IG_API_KEY/USERNAME/PASSWORD/ACC_NUMBER/IG_ENV from settings. Sets headers=None.

connect() -> bool
  # POST /session, sets self.headers (CST + X-SECURITY-TOKEN). Returns True on success.

ensure_connected() -> bool
  # Checks token validity, reconnects if needed. Call at start of each cycle.

get_market_info() -> Optional[dict]
  # GET /markets/{EPIC}. Returns: bid, offer, mid, market_status, epic, instrument_name

get_prices(resolution: str, num_points: int) -> list[dict]
  # Resolutions: "MINUTE_5", "MINUTE_15", "HOUR_4", "DAY"
  # Returns list of {open, high, low, close, volume, timestamp}

get_all_timeframes() -> dict
  # Returns {daily, h4, m15, m5} — all 4 timeframes at once

open_position(direction, size, stop_level, limit_level) -> Optional[dict]
  # direction = "BUY" or "SELL". Returns deal confirmation dict or None on timeout.
  # Paper mode: calls _paper_open() — simulated fill, no API call.
  # Calls _confirm_deal() internally (3 retries, 5s between).

modify_position(deal_id, stop_level=None, limit_level=None, trailing_stop=False,
                trailing_stop_distance=None, trailing_stop_increment=None) -> bool

close_position(deal_id, direction, size) -> Optional[dict]
  # direction for close = opposite of open direction ("BUY" to close SHORT, "SELL" to close LONG)

get_open_positions() -> list[dict] | POSITIONS_API_ERROR
  # Returns list of position dicts or POSITIONS_API_ERROR sentinel on failure

get_account_info() -> Optional[dict]
  # Returns: balance, available, currency, account_id

get_transaction_history(days=7) -> list[dict]

_confirm_deal(deal_reference, max_retries=3) -> Optional[dict]
  # Polls /confirms/{deal_reference}. Returns None on timeout.

_paper_open(direction, size, stop, limit) -> dict
  # Simulated fill using current market price.

calculate_margin(size, price=None) -> float
  # size * CONTRACT_SIZE * price * MARGIN_FACTOR
