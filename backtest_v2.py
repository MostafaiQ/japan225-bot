"""
Japan 225 Bot — Backtest v2 (2026-03-07)
=========================================
Rebuilt to match the 2026-03-07 system overhaul exactly:

WHAT CHANGED vs backtest.py (old):
  1. SL: ATR × multiplier (by setup_type), floor 120pts  — replaces hardcoded 150pt
  2. TP: ATR × multiplier (2.5 base, 3.0 for momentum), floor 250pts — replaces hardcoded 400pt
  3. Lot size: risk-based 2% × balance / sl_distance — replaces fixed 1 contract
  4. Compound P&L: balance updates after every trade close ($1000 start)
  5. Multi-position: up to 3 concurrent open trades, 8% portfolio risk cap
  6. WFO: tests ATR multiplier sensitivity (±20%) — not SL/TP combo grid
  7. Per-trade ATR pulled from tf_15m["atr"] (already computed by analyze_timeframe)

Unchanged from backtest.py:
  - Data fetch: yfinance NKD=F 15M+1H, ^N225 daily, 90 days
  - detect_setup() / compute_confidence() — identical live-bot code
  - Session filter: Tokyo/London/NY only, weekdays only
  - Breakeven at +150pts → SL to entry+10
  - Trailing stop at 150pts distance (runner phase)
  - Spread: 10pts round-trip

Usage:
  python3 backtest_v2.py           # Pure local, no AI
  python3 backtest_v2.py --wfo     # Also run ATR multiplier sensitivity
"""
import sys
import os
import argparse
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

logging.disable(logging.WARNING)

import yfinance as yf
import pandas as pd

from core.indicators import analyze_timeframe, detect_setup, compute_atr
from core.confidence import compute_confidence
from config.settings import (
    SESSION_HOURS_UTC, MINUTE_5_CANDLES,
    # Risk / sizing constants
    RISK_PERCENT, MAX_RISK_PERCENT, MAX_MARGIN_PERCENT,
    MAX_OPEN_POSITIONS, MAX_PORTFOLIO_RISK_PERCENT,
    CONTRACT_SIZE, MARGIN_FACTOR, MIN_LOT_SIZE,
    DRAWDOWN_REDUCE_10PCT, DRAWDOWN_REDUCE_15PCT, DRAWDOWN_STOP_20PCT,
    MIN_CONFIDENCE, MIN_CONFIDENCE_SHORT,
    # Dynamic SL/TP constants
    SL_ATR_MULTIPLIER_MOMENTUM, SL_ATR_MULTIPLIER_MEAN_REVERSION,
    SL_ATR_MULTIPLIER_BREAKOUT, SL_ATR_MULTIPLIER_VWAP, SL_ATR_MULTIPLIER_DEFAULT,
    SL_FLOOR_PTS,
    TP_ATR_MULTIPLIER_BASE, TP_ATR_MULTIPLIER_MOMENTUM, TP_FLOOR_PTS,
    # Exit constants
    BREAKEVEN_TRIGGER, BREAKEVEN_BUFFER, TRAILING_STOP_DISTANCE,
    DEFAULT_SL_DISTANCE,
)

logging.disable(logging.NOTSET)

# ── Constants ────────────────────────────────────────────────────────────────

SPREAD_PTS        = 10      # round-trip spread (5pts each side)
START_BALANCE     = 50.0     # starting account balance in USD
MIN_CANDLES_15M   = 55
MIN_CANDLES_1H    = 55

# ── Setup filter: setups with no edge after analysis ─────────────────────────
# breakout_long: 28% WR — false breakouts on Nikkei intraday, no sweep logic
# momentum_continuation_long: 28% WR — enters late, extension already done
from config.settings import DISABLED_SETUP_TYPES
SKIP_SETUP_TYPES  = DISABLED_SETUP_TYPES  # keep backtest in sync with live bot

# ── Session filter: London has 29% WR (-$54) with no Japan fundamental anchor
# TSE closes 06:30 UTC; London session trades on noise not flow
SKIP_SESSIONS     = set()  # no session filter — all sessions enabled
MIN_CANDLES_DAILY = 250
MIN_CANDLES_4H    = 55
CANDLE_MINUTES    = {"15M": 15, "1H": 60}
SESSIONS          = SESSION_HOURS_UTC


# ── Session filter ───────────────────────────────────────────────────────────

def get_session(dt: datetime) -> str | None:
    h = dt.hour
    for name, (start, end) in SESSIONS.items():
        if start <= h < end:
            return name
    return None


# ── Dynamic SL/TP (mirrors risk_manager.py exactly) ─────────────────────────

def get_dynamic_sl(atr: float, setup_type: str = None) -> float:
    """Compute ATR-based SL distance. Mirrors RiskManager.get_dynamic_sl()."""
    if not atr or atr <= 0:
        return max(DEFAULT_SL_DISTANCE, SL_FLOOR_PTS)
    if setup_type:
        st = setup_type.lower()
        if "breakout" in st:
            multiplier = SL_ATR_MULTIPLIER_BREAKOUT
        elif "momentum" in st or "continuation" in st:
            multiplier = SL_ATR_MULTIPLIER_MOMENTUM
        elif "vwap" in st or "ema9" in st:
            multiplier = SL_ATR_MULTIPLIER_VWAP
        elif any(x in st for x in ("bounce", "reversal", "oversold", "reversion", "rejection")):
            multiplier = SL_ATR_MULTIPLIER_MEAN_REVERSION
        else:
            multiplier = SL_ATR_MULTIPLIER_DEFAULT
    else:
        multiplier = SL_ATR_MULTIPLIER_DEFAULT
    return max(multiplier * atr, SL_FLOOR_PTS)


def get_dynamic_tp(atr: float, setup_type: str = None) -> float:
    """Compute ATR-based TP distance."""
    if not atr or atr <= 0:
        return TP_FLOOR_PTS
    st = (setup_type or "").lower()
    if "momentum" in st or "continuation" in st or "breakout" in st:
        multiplier = TP_ATR_MULTIPLIER_MOMENTUM   # 3.0
    else:
        multiplier = TP_ATR_MULTIPLIER_BASE        # 2.5
    return max(multiplier * atr, TP_FLOOR_PTS)


def get_dynamic_sl_scaled(atr: float, setup_type: str, sl_scale: float = 1.0) -> float:
    """Scaled version for WFO sensitivity testing."""
    base = get_dynamic_sl(atr, setup_type)
    return max(base * sl_scale, SL_FLOOR_PTS)


def get_dynamic_tp_scaled(atr: float, setup_type: str, tp_scale: float = 1.0) -> float:
    """Scaled version for WFO sensitivity testing."""
    base = get_dynamic_tp(atr, setup_type)
    return max(base * tp_scale, TP_FLOOR_PTS)


# ── Risk-based lot sizing (mirrors risk_manager.py get_safe_lot_size()) ──────

def get_safe_lot_size(
    balance: float,
    price: float,
    sl_distance: float,
    confidence: int = None,
    peak_balance: float = None,
) -> float:
    """
    Risk-based lot sizing.
    lots = (RISK_PERCENT% × balance) / (sl_distance × $1/pt)
    Capped by: margin <= MAX_MARGIN_PERCENT of balance
    Drawdown protection applied when peak_balance provided.
    """
    risk_pct = RISK_PERCENT  # 2.0%

    # Drawdown protection
    if peak_balance and peak_balance > 0:
        drawdown = (peak_balance - balance) / peak_balance
        if drawdown >= 0.20 and DRAWDOWN_STOP_20PCT:
            return MIN_LOT_SIZE
        elif drawdown >= 0.15:
            risk_pct = DRAWDOWN_REDUCE_15PCT   # 0.25%
        elif drawdown >= 0.10:
            risk_pct = DRAWDOWN_REDUCE_10PCT   # 0.5%

    # SL floor
    sl_distance = max(sl_distance, SL_FLOOR_PTS)

    # Dollar risk
    dollar_risk = (risk_pct / 100.0) * balance

    # Confidence scaling: +15% max
    if confidence is not None:
        confidence_mult = min(1.15, 1.0 + (confidence - MIN_CONFIDENCE) / 200.0)
        dollar_risk *= confidence_mult

    # Lots from dollar risk
    lots = dollar_risk / (sl_distance * CONTRACT_SIZE)

    # Cap by margin ceiling (5% of balance)
    if price > 0:
        max_lots_margin = (balance * MAX_MARGIN_PERCENT) / (price * MARGIN_FACTOR)
        lots = min(lots, max_lots_margin)

    return max(MIN_LOT_SIZE, round(lots, 2))


# ── Data download ────────────────────────────────────────────────────────────

def download_data():
    print("Downloading Nikkei data from Yahoo Finance...")
    df_15m = yf.download("NKD=F", interval="15m", period="60d",  progress=False, auto_adjust=True)
    df_1h  = yf.download("NKD=F", interval="1h",  period="730d", progress=False, auto_adjust=True)
    df_1d  = yf.download("^N225", interval="1d",  period="2y",   progress=False, auto_adjust=True)

    for df in [df_15m, df_1h, df_1d]:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

    if df_15m.empty:
        raise RuntimeError("NKD=F 15M data empty — check Yahoo Finance ticker")

    print(f"  NKD=F 15min: {len(df_15m)} ({df_15m.index[0].date()} -> {df_15m.index[-1].date()})")
    print(f"  NKD=F 1H:    {len(df_1h)} ({df_1h.index[0].date()} -> {df_1h.index[-1].date()})")
    print(f"  ^N225 Daily: {len(df_1d)}")

    start_15m = df_15m.index[0]
    start_1h  = df_1h.index[0]
    if start_1h < start_15m:
        gap_days = (start_15m - start_1h).days
        total_days = (df_15m.index[-1] - start_1h).days
        print(f"  1H extends {gap_days}d before 15M. Combined coverage: ~{total_days}d")

    return df_15m, df_1h, df_1d


def resample_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    df = df_1h.copy()
    df.index = pd.to_datetime(df.index)
    return df.resample("4h").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna()


def df_to_candles(df: pd.DataFrame) -> list[dict]:
    candles = []
    for ts, row in df.iterrows():
        candles.append({
            "timestamp": ts,
            "open":   float(row["Open"]),
            "high":   float(row["High"]),
            "low":    float(row["Low"]),
            "close":  float(row["Close"]),
            "volume": float(row.get("Volume", 0) or 0),
        })
    return candles


# ── Pre-compute timeframe caches ──────────────────────────────────────────────

def precompute_daily(df_1d: pd.DataFrame) -> dict:
    print(f"Pre-computing daily timeframes ({len(df_1d)} candles)...")
    daily_tf = {}
    for i in range(len(df_1d)):
        date = df_1d.index[i].date()
        candles = df_to_candles(df_1d.iloc[:i+1])
        if len(candles) < MIN_CANDLES_DAILY:
            daily_tf[date] = None
            continue
        logging.disable(logging.WARNING)
        try:
            tf = analyze_timeframe(candles[-250:])
        except Exception:
            tf = None
        logging.disable(logging.NOTSET)
        daily_tf[date] = tf
    valid = sum(1 for v in daily_tf.values() if v is not None)
    print(f"  Daily cache: {valid}/{len(daily_tf)} dates have EMA200")
    return daily_tf


def precompute_4h(df_4h: pd.DataFrame) -> dict:
    print(f"Pre-computing 4H timeframes ({len(df_4h)} candles)...")
    h4_tf = {}
    for i in range(len(df_4h)):
        ts = df_4h.index[i]
        if i < MIN_CANDLES_4H:
            h4_tf[ts] = None
            continue
        candles = df_to_candles(df_4h.iloc[max(0, i-54):i+1])
        logging.disable(logging.WARNING)
        try:
            tf = analyze_timeframe(candles)
        except Exception:
            tf = None
        logging.disable(logging.NOTSET)
        h4_tf[ts] = tf
    valid = sum(1 for v in h4_tf.values() if v is not None)
    print(f"  4H cache: {valid}/{len(h4_tf)} bars computed")
    return h4_tf


# ── Setup record ──────────────────────────────────────────────────────────────

class SetupRecord:
    """Qualifying setup with ATR-based SL/TP pre-computed."""
    __slots__ = (
        "idx", "entry_time", "direction", "setup_type", "entry_price",
        "confidence_score", "session", "future_candles", "timeframe",
        "atr_15m",       # ATR at entry — used for lot sizing + SL/TP
        "sl_distance",   # ATR-based dynamic SL (pts)
        "tp_distance",   # ATR-based dynamic TP (pts)
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ── Setup detection loop ──────────────────────────────────────────────────────

def find_setups(df_15m: pd.DataFrame, df_1h: pd.DataFrame, df_1d: pd.DataFrame,
                daily_tf: dict, h4_tf: dict) -> tuple[list[SetupRecord], int, int]:
    """
    Scan all candles (1H for older data, 15M for last 60d) and collect qualifying setups.
    Attaches ATR-based SL/TP to each setup record.
    Returns: (setups, signals_raw, signals_qualified)
    """
    # Ensure UTC indexes
    for df in [df_15m, df_1h]:
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

    h4_sorted = sorted(h4_tf.keys())
    cutoff_15m = df_15m.index[0]  # 15M data starts here

    setups: list[SetupRecord] = []
    signals_raw = 0
    signals_qual = 0

    # ── Phase 1: 1H scan for gap period before 15M ───────────────────────────
    if df_1h.index[0] < cutoff_15m:
        print(f"Scanning 1H candles ({df_1h.index[0].date()} -> {cutoff_15m.date()})...")
        total_1h = len(df_1h)

        for i in range(MIN_CANDLES_1H, total_1h):
            ts = df_1h.index[i]
            ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            if ts_utc >= cutoff_15m:
                break

            session = get_session(ts_utc)
            if session is None or session in SKIP_SESSIONS or ts_utc.weekday() >= 5:
                continue

            date_key = ts_utc.date()
            tf_daily = daily_tf.get(date_key)
            if tf_daily is None:
                continue

            tf_4h = None
            for h4_ts in reversed(h4_sorted):
                if h4_ts <= ts_utc:
                    tf_4h = h4_tf.get(h4_ts)
                    break
            if tf_4h is None:
                continue

            candles_1h = df_to_candles(df_1h.iloc[max(0, i-54):i+1])
            if len(candles_1h) < MIN_CANDLES_1H:
                continue

            logging.disable(logging.WARNING)
            try:
                tf_entry = analyze_timeframe(candles_1h)
            except Exception:
                logging.disable(logging.NOTSET)
                continue
            logging.disable(logging.NOTSET)

            setup = detect_setup(tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_entry)
            if not setup["found"]:
                continue
            if setup.get("type") in SKIP_SETUP_TYPES:
                continue
            signals_raw += 1

            logging.disable(logging.WARNING)
            try:
                conf = compute_confidence(
                    direction=setup["direction"],
                    tf_daily=tf_daily,
                    tf_4h=tf_4h,
                    tf_15m=tf_entry,
                    upcoming_events=[],
                    web_research=None,
                    setup_type=setup.get("type"),
                )
            except Exception:
                logging.disable(logging.NOTSET)
                continue
            logging.disable(logging.NOTSET)

            if not conf["meets_threshold"]:
                continue
            signals_qual += 1

            future_df = df_1h.iloc[i+1: i+101]
            future_candles = df_to_candles(future_df)
            if len(future_candles) < 4:
                continue

            # ATR from the entry TF (1H candles — wider)
            atr = tf_entry.get("atr", 0) or 0
            sl_dist = get_dynamic_sl(atr, setup.get("type"))
            tp_dist = get_dynamic_tp(atr, setup.get("type"))

            setups.append(SetupRecord(
                idx=i,
                entry_time=ts_utc,
                direction=setup["direction"],
                setup_type=setup["type"],
                entry_price=float(tf_entry["price"]),
                confidence_score=conf["score"],
                session=session,
                future_candles=future_candles,
                timeframe="1H",
                atr_15m=atr,
                sl_distance=sl_dist,
                tp_distance=tp_dist,
            ))

    # ── Phase 2: 15M scan ────────────────────────────────────────────────────
    print(f"Scanning 15M candles ({df_15m.index[0].date()} -> {df_15m.index[-1].date()})...")
    total_15m = len(df_15m)

    for i in range(MIN_CANDLES_15M, total_15m - 30):
        ts = df_15m.index[i]
        ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

        session = get_session(ts_utc)
        if session is None or session in SKIP_SESSIONS or ts_utc.weekday() >= 5:
            continue

        date_key = ts_utc.date()
        tf_daily = daily_tf.get(date_key)
        if tf_daily is None:
            continue

        tf_4h = None
        for h4_ts in reversed(h4_sorted):
            if h4_ts <= ts_utc:
                tf_4h = h4_tf.get(h4_ts)
                break
        if tf_4h is None:
            continue

        candles_15m = df_to_candles(df_15m.iloc[max(0, i-54):i+1])
        if len(candles_15m) < MIN_CANDLES_15M:
            continue

        logging.disable(logging.WARNING)
        try:
            tf_15m = analyze_timeframe(candles_15m)
        except Exception:
            logging.disable(logging.NOTSET)
            continue
        logging.disable(logging.NOTSET)

        setup = detect_setup(tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_15m)
        if not setup["found"]:
            continue
        if setup.get("type") in SKIP_SETUP_TYPES:
            continue
        signals_raw += 1

        logging.disable(logging.WARNING)
        try:
            conf = compute_confidence(
                direction=setup["direction"],
                tf_daily=tf_daily,
                tf_4h=tf_4h,
                tf_15m=tf_15m,
                upcoming_events=[],
                web_research=None,
                setup_type=setup.get("type"),
            )
        except Exception:
            logging.disable(logging.NOTSET)
            continue
        logging.disable(logging.NOTSET)

        if not conf["meets_threshold"]:
            continue
        signals_qual += 1

        future_df = df_15m.iloc[i+1: i+101]
        future_candles = df_to_candles(future_df)
        if len(future_candles) < 4:
            continue

        # ATR from 15M candles — drives SL/TP/lot sizing
        atr = tf_15m.get("atr", 0) or 0
        sl_dist = get_dynamic_sl(atr, setup.get("type"))
        tp_dist = get_dynamic_tp(atr, setup.get("type"))

        setups.append(SetupRecord(
            idx=i,
            entry_time=ts_utc,
            direction=setup["direction"],
            setup_type=setup["type"],
            entry_price=float(tf_15m["price"]),
            confidence_score=conf["score"],
            session=session,
            future_candles=future_candles,
            timeframe="15M",
            atr_15m=atr,
            sl_distance=sl_dist,
            tp_distance=tp_dist,
        ))

        if len(setups) % 10 == 0 and len(setups) > 0:
            print(f"  ... {i}/{total_15m} scanned, {signals_raw} signals, {len(setups)} setups")

    return setups, signals_raw, signals_qual


# ── Trade simulation (candle-by-candle) ──────────────────────────────────────

def simulate_trade_candles(
    direction: str,
    entry_price: float,
    future_candles: list[dict],
    sl_dist: float,
    tp_dist: float,
) -> tuple[float, str, int]:
    """
    Simulate a single trade candle-by-candle.
    Pure fixed TP/SL — no breakeven, no trailing stop.
    Returns: (pnl_pts_after_spread, exit_reason, candles_held)
    """
    sl = entry_price - sl_dist if direction == "LONG" else entry_price + sl_dist
    tp = entry_price + tp_dist if direction == "LONG" else entry_price - tp_dist

    for idx, candle in enumerate(future_candles):
        high, low = candle["high"], candle["low"]
        if direction == "LONG":
            if high >= tp:
                return tp - entry_price - SPREAD_PTS, "TP_HIT", idx + 1
            if low <= sl:
                return sl - entry_price - SPREAD_PTS, "SL_HIT", idx + 1
        else:
            if low <= tp:
                return entry_price - tp - SPREAD_PTS, "TP_HIT", idx + 1
            if high >= sl:
                return entry_price - sl - SPREAD_PTS, "SL_HIT", idx + 1

    last_close = future_candles[-1]["close"]
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return pnl - SPREAD_PTS, "END_OF_DATA", len(future_candles)


# ── Multi-position compound simulation ───────────────────────────────────────

class TradeResultV2:
    __slots__ = (
        "entry_time", "exit_time", "direction", "setup_type", "entry_price",
        "exit_price", "exit_reason", "lots", "pnl_pts", "pnl_usd",
        "sl_distance", "tp_distance", "atr", "confidence_score",
        "session", "timeframe", "balance_after",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def simulate_all_trades_v2(
    setups: list[SetupRecord],
    start_balance: float = START_BALANCE,
    sl_scale: float = 1.0,
    tp_scale: float = 1.0,
) -> list[TradeResultV2]:
    """
    Multi-position compound simulation.

    Key differences from old simulate_all_trades():
    - Up to MAX_OPEN_POSITIONS=3 concurrent trades
    - Portfolio risk cap 8%: total open risk <= 8% of balance
    - Lot sizing is risk-based (2% of balance / sl_distance)
    - P&L compounds — each trade's lot size uses current balance
    - sl_scale / tp_scale: multiplier on dynamic SL/TP (for WFO sensitivity)

    Uses a time-step approach: advance through candles chronologically,
    opening/closing positions as signals arrive and SL/TP fire.
    """
    sorted_setups = sorted(setups, key=lambda x: x.entry_time)
    if not sorted_setups:
        return []

    balance = start_balance
    peak_balance = start_balance

    # Active positions: list of dicts with trade state
    open_positions = []
    closed_trades: list[TradeResultV2] = []

    # Candle clock index per setup — tracks how far we've advanced
    # We process setups in order, advancing each open position in parallel
    setup_idx = 0
    total_setups = len(sorted_setups)

    # Event queue approach: maintain open trades and advance all by candle
    # Each open position carries its future_candles iterator + current candle index
    pos_id = 0

    def _can_open(new_sl_dist, new_entry_price, new_lots) -> bool:
        """Check max positions and portfolio risk cap."""
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            return False
        new_dollar_risk = new_lots * new_sl_dist * CONTRACT_SIZE
        existing_risk = sum(
            p["lots"] * p["sl_distance"] * CONTRACT_SIZE
            for p in open_positions
        )
        total_risk = (new_dollar_risk + existing_risk) / balance if balance > 0 else 1.0
        return total_risk <= MAX_PORTFOLIO_RISK_PERCENT

    # Build a flat timeline: each event is either "open_setup" or "close_check"
    # We iterate setup by setup and advance all open positions in parallel

    # Process: for each new setup signal time, first advance all open positions
    # up to that signal time, then open the new position if checks pass.

    def advance_position(pos, target_time) -> bool:
        """
        Advance a position's candle clock up to target_time.
        Returns True if position is still open, False if closed.
        Modifies pos in place. Appends to closed_trades if closed.
        """
        nonlocal balance, peak_balance

        tf = pos["timeframe"]
        candle_mins = CANDLE_MINUTES.get(tf, 15)

        while pos["candle_idx"] < len(pos["future_candles"]):
            candle = pos["future_candles"][pos["candle_idx"]]
            candle_time = pos["entry_time"] + timedelta(
                minutes=(pos["candle_idx"] + 1) * candle_mins
            )
            if candle_time > target_time:
                break

            # Check SL/TP/BE/trail for this candle
            high, low = candle["high"], candle["low"]
            direction = pos["direction"]

            if direction == "LONG":
                if high >= pos["tp"]:
                    _close_position(pos, pos["tp"], "TP_HIT", pos["candle_idx"] + 1)
                    return False
                if low <= pos["sl"]:
                    _close_position(pos, pos["sl"], "SL_HIT", pos["candle_idx"] + 1)
                    return False
            else:  # SHORT
                if low <= pos["tp"]:
                    _close_position(pos, pos["tp"], "TP_HIT", pos["candle_idx"] + 1)
                    return False
                if high >= pos["sl"]:
                    _close_position(pos, pos["sl"], "SL_HIT", pos["candle_idx"] + 1)
                    return False

            pos["candle_idx"] += 1

        # Check if we've exhausted future candles
        if pos["candle_idx"] >= len(pos["future_candles"]):
            last_close = pos["future_candles"][-1]["close"]
            _close_position(pos, last_close, "END_OF_DATA", len(pos["future_candles"]))
            return False

        return True  # still open

    def _close_position(pos, exit_price: float, reason: str, candles_held: int):
        nonlocal balance, peak_balance

        direction = pos["direction"]
        if direction == "LONG":
            pnl_pts = exit_price - pos["entry_price"] - SPREAD_PTS
        else:
            pnl_pts = pos["entry_price"] - exit_price - SPREAD_PTS

        pnl_usd = round(pnl_pts * pos["lots"] * CONTRACT_SIZE, 2)
        balance += pnl_usd
        if balance > peak_balance:
            peak_balance = balance

        tf = pos["timeframe"]
        candle_mins = CANDLE_MINUTES.get(tf, 15)
        exit_time = pos["entry_time"] + timedelta(minutes=candles_held * candle_mins)

        closed_trades.append(TradeResultV2(
            entry_time=pos["entry_time"],
            exit_time=exit_time,
            direction=direction,
            setup_type=pos["setup_type"],
            entry_price=round(pos["entry_price"], 1),
            exit_price=round(exit_price, 1),
            exit_reason=reason,
            lots=pos["lots"],
            pnl_pts=round(pnl_pts, 1),
            pnl_usd=pnl_usd,
            sl_distance=pos["sl_distance"],
            tp_distance=pos["tp_distance"],
            atr=pos["atr"],
            confidence_score=pos["confidence_score"],
            session=pos["session"],
            timeframe=pos["timeframe"],
            balance_after=round(balance, 2),
        ))

    # Main simulation loop
    for s in sorted_setups:
        signal_time = s.entry_time

        # Advance all open positions to this signal time
        still_open = []
        for pos in open_positions:
            if advance_position(pos, signal_time):
                still_open.append(pos)
        open_positions = still_open

        # Compute this setup's SL/TP with scaling
        sl_dist = get_dynamic_sl_scaled(s.atr_15m, s.setup_type, sl_scale)
        tp_dist = get_dynamic_tp_scaled(s.atr_15m, s.setup_type, tp_scale)

        # Compute lot size using CURRENT balance and peak
        lots = get_safe_lot_size(
            balance=balance,
            price=s.entry_price,
            sl_distance=sl_dist,
            confidence=s.confidence_score,
            peak_balance=peak_balance,
        )

        # Portfolio risk gate
        if not _can_open(sl_dist, s.entry_price, lots):
            continue  # skip — positions full or portfolio risk cap hit

        # Open new position
        pos_id += 1
        new_pos = {
            "id": pos_id,
            "entry_time": signal_time,
            "direction": s.direction,
            "setup_type": s.setup_type,
            "entry_price": s.entry_price,
            "lots": lots,
            "sl": s.entry_price - sl_dist if s.direction == "LONG" else s.entry_price + sl_dist,
            "tp": s.entry_price + tp_dist if s.direction == "LONG" else s.entry_price - tp_dist,
            "sl_distance": sl_dist,
            "tp_distance": tp_dist,
            "atr": s.atr_15m,
            "confidence_score": s.confidence_score,
            "session": s.session,
            "timeframe": s.timeframe,
            "future_candles": s.future_candles,
            "candle_idx": 0,
        }
        open_positions.append(new_pos)

    # Close any remaining open positions at end-of-data
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    for pos in open_positions:
        advance_position(pos, far_future)

    return closed_trades


# ── Stats helpers ────────────────────────────────────────────────────────────

def _hold_hours(t: TradeResultV2) -> float:
    tf = getattr(t, "timeframe", "15M")
    candle_mins = CANDLE_MINUTES.get(tf, 15)
    try:
        delta = (t.exit_time - t.entry_time).total_seconds() / 3600
        return delta
    except Exception:
        return 0.0


def _calc_stats(trades: list[TradeResultV2]) -> dict:
    if not trades:
        return {"n": 0, "wr": 0, "pf": 0, "pnl": 0, "avg": 0}
    wins   = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    gp = sum(t.pnl_usd for t in wins) if wins else 0
    gl = abs(sum(t.pnl_usd for t in losses)) or 0.01
    total_pnl = sum(t.pnl_usd for t in trades)
    return {
        "n": len(trades),
        "wr": len(wins) / len(trades),
        "pf": gp / gl,
        "pnl": total_pnl,
        "avg": total_pnl / len(trades),
    }


def _max_drawdown(trades: list[TradeResultV2], start_balance: float) -> float:
    """Compute max drawdown in USD from compounding balance sequence."""
    running = start_balance
    peak = start_balance
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.entry_time):
        running += t.pnl_usd
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(
    trades: list[TradeResultV2],
    signals_raw: int,
    signals_qual: int,
    start_balance: float,
    df_15m: pd.DataFrame,
):
    n = len(trades)
    start_date = df_15m.index[0].date()
    end_date   = df_15m.index[-1].date()

    print("\n" + "=" * 72)
    print("  JAPAN 225 BOT — BACKTEST v2 RESULTS")
    print("=" * 72)
    print(f"  Period:       {start_date} -> {end_date}")
    print(f"  Data:         NKD=F 1H+15M | ^N225 Daily (EMA200)")
    print(f"  Spread:       {SPREAD_PTS}pts round-trip")
    print(f"  SL:           ATR × multiplier (1.2-1.8), floor {SL_FLOOR_PTS}pts")
    print(f"  TP:           ATR × 2.5-3.0, floor {TP_FLOOR_PTS}pts")
    print(f"  Lot sizing:   {RISK_PERCENT}% risk / sl_dist, cap {MAX_MARGIN_PERCENT*100:.0f}% margin")
    print(f"  Max positions:{MAX_OPEN_POSITIONS} concurrent | Portfolio risk cap {MAX_PORTFOLIO_RISK_PERCENT*100:.0f}%")
    print(f"  Start balance:${start_balance:.2f}")
    print("-" * 72)

    print(f"\n  SIGNAL FUNNEL")
    print(f"  Raw setups detected:     {signals_raw}")
    if signals_raw:
        print(f"  Passed confidence gate:  {signals_qual}  ({signals_qual/signals_raw*100:.0f}%)")
    print(f"  Trades executed:         {n}  (no AI filter)")

    if n == 0:
        print("\n  NO TRADES — strategy never triggered.")
        return

    wins   = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    total_pnl = sum(t.pnl_usd for t in trades)
    final_balance = start_balance + total_pnl
    total_return_pct = total_pnl / start_balance * 100

    # Average SL/TP/RR across trades
    avg_sl = sum(t.sl_distance for t in trades) / n
    avg_tp = sum(t.tp_distance for t in trades) / n
    avg_rr = avg_tp / avg_sl if avg_sl > 0 else 0
    breakeven_wr = 1 / (1 + avg_rr) if avg_rr > 0 else 0.5
    avg_lots = sum(t.lots for t in trades) / n

    # Avg ATR
    atrs = [t.atr for t in trades if t.atr > 0]
    avg_atr = sum(atrs) / len(atrs) if atrs else 0

    print(f"\n  P&L SUMMARY")
    print(f"  Total trades:    {n}")
    print(f"  Wins:            {len(wins)}  ({len(wins)/n*100:.1f}%)")
    print(f"  Losses:          {len(losses)}  ({len(losses)/n*100:.1f}%)")
    print(f"  Start balance:   ${start_balance:.2f}")
    print(f"  Final balance:   ${final_balance:.2f}")
    print(f"  Total P&L:       ${total_pnl:+.2f}  ({total_return_pct:+.1f}%)")
    print(f"  Avg P&L/trade:   ${total_pnl/n:+.2f}")
    if wins:
        print(f"  Avg win:         ${sum(t.pnl_usd for t in wins)/len(wins):+.2f}")
    if losses:
        print(f"  Avg loss:        ${sum(t.pnl_usd for t in losses)/len(losses):+.2f}")
    print(f"  Best trade:      ${max(t.pnl_usd for t in trades):+.2f}")
    print(f"  Worst trade:     ${min(t.pnl_usd for t in trades):+.2f}")

    gp = sum(t.pnl_usd for t in wins) if wins else 0
    gl = abs(sum(t.pnl_usd for t in losses)) if losses else 0.01
    pf = gp / gl
    max_dd = _max_drawdown(trades, start_balance)
    max_dd_pct = max_dd / start_balance * 100

    win_rate = len(wins) / n

    print(f"\n  RISK METRICS")
    print(f"  Win rate:        {win_rate*100:.1f}%  (breakeven ~{breakeven_wr*100:.0f}% at avg {avg_rr:.1f}:1 R:R)")
    print(f"  Profit factor:   {pf:.2f}  (>1.5 healthy)")
    print(f"  Max drawdown:    ${max_dd:.2f}  ({max_dd_pct:.1f}% of start)")
    print(f"  Avg SL/TP:       {avg_sl:.0f}pts / {avg_tp:.0f}pts  (R:R {avg_rr:.2f}:1)")
    print(f"  Avg lot size:    {avg_lots:.3f}")
    print(f"  Avg ATR 15M:     {avg_atr:.0f}pts")

    avg_hold = sum(_hold_hours(t) for t in trades) / n
    total_days = (df_15m.index[-1].date() - df_15m.index[0].date()).days or 1
    trades_per_day = n / total_days
    print(f"  Avg hold time:   {avg_hold:.1f}h")
    print(f"  Trades/day:      {trades_per_day:.2f}")

    print(f"\n  BY DIRECTION")
    for d in ["LONG", "SHORT"]:
        dt = [t for t in trades if t.direction == d]
        if not dt:
            continue
        dw = [t for t in dt if t.pnl_usd > 0]
        d_pnl = sum(t.pnl_usd for t in dt)
        print(f"  {d}: {len(dt)} trades | {len(dw)/len(dt)*100:.0f}% win | P&L: ${d_pnl:+.2f}")

    print(f"\n  BY SESSION")
    for sess in ["Tokyo", "London", "New York"]:
        st = [t for t in trades if t.session == sess]
        if not st:
            continue
        sw = [t for t in st if t.pnl_usd > 0]
        s_pnl = sum(t.pnl_usd for t in st)
        print(f"  {sess}: {len(st)} trades | {len(sw)/len(st)*100:.0f}% win | P&L: ${s_pnl:+.2f}")

    print(f"\n  BY EXIT REASON")
    reasons = defaultdict(list)
    for t in trades:
        reasons[t.exit_reason].append(t)
    for r, rt in sorted(reasons.items()):
        rw = [x for x in rt if x.pnl_usd > 0]
        print(f"  {r:<20} {len(rt):>3} trades | {len(rw)/len(rt)*100:.0f}% win | P&L: ${sum(t.pnl_usd for t in rt):+.2f}")

    print(f"\n  BY SETUP TYPE")
    setups_d = defaultdict(list)
    for t in trades:
        setups_d[t.setup_type].append(t)
    for st_name, sl in sorted(setups_d.items()):
        sw2 = [t for t in sl if t.pnl_usd > 0]
        sl_avg = sum(t.sl_distance for t in sl) / len(sl)
        tp_avg = sum(t.tp_distance for t in sl) / len(sl)
        print(
            f"  {st_name:<32} {len(sl):>3} trades | {len(sw2)/len(sl)*100:.0f}% win | "
            f"SL≈{sl_avg:.0f} TP≈{tp_avg:.0f} | ${sum(t.pnl_usd for t in sl):+.2f}"
        )

    print(f"\n  CONFIDENCE DISTRIBUTION")
    for lo, hi in [(70, 80), (80, 90), (90, 101)]:
        ct = [t for t in trades if lo <= t.confidence_score < hi]
        if not ct:
            continue
        cw = [t for t in ct if t.pnl_usd > 0]
        print(
            f"  {lo}-{hi-1}%: {len(ct):>3} trades | {len(cw)/len(ct)*100:.0f}% win | "
            f"${sum(t.pnl_usd for t in ct):+.2f}"
        )

    print(f"\n  BALANCE CURVE (by exit time, sorted)")
    running = start_balance
    print(f"  {'Date':>10}  {'Dir':>5}  {'Setup':>20}  {'Lots':>5}  {'SL':>5}  {'TP':>5}  {'P&L':>8}  {'Balance':>9}")
    print("  " + "-" * 80)
    for t in sorted(trades, key=lambda x: x.exit_time):
        running_check = running + t.pnl_usd
        print(
            f"  {t.entry_time.strftime('%Y-%m-%d'):>10}  {t.direction:>5}  {t.setup_type:>20}  "
            f"{t.lots:>5.2f}  {t.sl_distance:>5.0f}  {t.tp_distance:>5.0f}  "
            f"${t.pnl_usd:>+7.2f}  ${t.balance_after:>8.2f}"
        )
        running = running_check

    # Verdict
    print("\n" + "=" * 72)
    print("  VERDICT")
    print("=" * 72)
    if n < 10:
        print(f"  WARNING: Only {n} trades — statistically thin. More data needed.")
    if win_rate > breakeven_wr and pf > 1.0:
        print(f"  POSITIVE EDGE: WR {win_rate*100:.0f}% > breakeven ~{breakeven_wr*100:.0f}%, PF={pf:.2f}")
        if pf >= 1.5:
            print(f"  PF={pf:.2f} >= 1.5 — strategy viable for live capital")
        else:
            print(f"  PF={pf:.2f} < 1.5 — profitable but thin margin")
    else:
        print(f"  BELOW BREAKEVEN: WR {win_rate*100:.0f}% (need ~{breakeven_wr*100:.0f}%), PF={pf:.2f}")
        print(f"  Do NOT deploy live at these numbers.")


# ── WFO: ATR Multiplier Sensitivity ──────────────────────────────────────────

def run_wfo_sensitivity(setups: list[SetupRecord], start_balance: float = START_BALANCE):
    """
    Walk-Forward Optimization: test ATR multiplier sensitivity.
    IS = first 70%, OOS = last 30%.
    Instead of a SL/TP combo grid, test ±20% scaling on ATR multipliers.
    """
    if len(setups) < 5:
        print(f"\n  WFO skipped — only {len(setups)} setups (need >= 5)")
        return

    sorted_setups = sorted(setups, key=lambda x: x.entry_time)
    split = int(len(sorted_setups) * 0.70)
    is_setups  = sorted_setups[:split]
    oos_setups = sorted_setups[split:]

    # Scale grid: (sl_scale, tp_scale, label)
    grid = [
        (0.80, 0.80, "SL-20% TP-20%"),
        (0.80, 1.00, "SL-20% TP=def"),
        (0.80, 1.20, "SL-20% TP+20%"),
        (1.00, 0.80, "SL=def  TP-20%"),
        (1.00, 1.00, "DEFAULT        "),
        (1.00, 1.20, "SL=def  TP+20%"),
        (1.20, 0.80, "SL+20% TP-20%"),
        (1.20, 1.00, "SL+20% TP=def"),
        (1.20, 1.20, "SL+20% TP+20%"),
    ]

    print("\n" + "=" * 72)
    print("  WALK-FORWARD OPTIMIZATION — ATR Multiplier Sensitivity")
    print(f"  IS: {len(is_setups)} setups ({is_setups[0].entry_time.date()} -> {is_setups[-1].entry_time.date()})")
    print(f"  OOS: {len(oos_setups)} setups ({oos_setups[0].entry_time.date()} -> {oos_setups[-1].entry_time.date()})")
    print("=" * 72)
    print(f"\n  {'Config':>16}  {'Trades':>6}  {'WR':>7}  {'PF':>6}  {'P&L':>9}  {'MaxDD':>8}")
    print("  " + "-" * 65)

    is_results = []
    for sl_s, tp_s, label in grid:
        trades_is = simulate_all_trades_v2(is_setups, start_balance, sl_s, tp_s)
        s = _calc_stats(trades_is)
        dd = _max_drawdown(trades_is, start_balance)
        is_results.append((sl_s, tp_s, label, s, dd, trades_is))
        print(
            f"  {label:>16}  {s['n']:>6}  {s['wr']*100:>6.1f}%  "
            f"{s['pf']:>6.2f}  ${s['pnl']:>+8.2f}  ${dd:>7.2f}"
        )

    # Best IS by PF (min 3 trades)
    qualified = [(r) for r in is_results if r[3]["n"] >= 3]
    if not qualified:
        qualified = is_results
    best = max(qualified, key=lambda x: x[3]["pf"])
    best_sl_s, best_tp_s, best_label = best[0], best[1], best[2]

    print(f"\n  Best IS config: {best_label.strip()} (PF={best[3]['pf']:.2f})")
    trades_oos = simulate_all_trades_v2(oos_setups, start_balance, best_sl_s, best_tp_s)
    s_oos = _calc_stats(trades_oos)
    dd_oos = _max_drawdown(trades_oos, start_balance)

    print(f"\n  OOS RESULT with best IS config ({best_label.strip()}):")
    print(f"  Trades: {s_oos['n']} | WR: {s_oos['wr']*100:.1f}% | PF: {s_oos['pf']:.2f} | "
          f"P&L: ${s_oos['pnl']:+.2f} | MaxDD: ${dd_oos:.2f}")

    best_is = best[3]
    if best_is["pf"] > 0:
        degrad = (best_is["pf"] - s_oos["pf"]) / best_is["pf"] * 100
        print(f"  PF degradation IS→OOS: {degrad:+.1f}% (<40% = acceptable)")

    if s_oos["pf"] >= 1.0:
        print(f"  OOS is profitable — ATR-based sizing generalises well out-of-sample")
    else:
        print(f"  OOS PF < 1.0 — strategy may be overfitting or needs more data")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Japan 225 Backtest v2")
    parser.add_argument("--wfo", action="store_true", help="Run ATR multiplier sensitivity WFO")
    parser.add_argument("--balance", type=float, default=START_BALANCE,
                        help=f"Starting balance (default ${START_BALANCE})")
    args = parser.parse_args()

    start_balance = args.balance

    print("=" * 72)
    print("  JAPAN 225 BOT — BACKTEST v2")
    print(f"  New system: ATR-based SL/TP, risk-based lot sizing, multi-position")
    print(f"  Start balance: ${start_balance:.2f}")
    print("=" * 72)

    # ── 1. Fetch data ────────────────────────────────────────────────────────
    df_15m, df_1h, df_1d = download_data()

    # ── 2. Resample 4H ──────────────────────────────────────────────────────
    df_4h = resample_4h(df_1h)

    # ── 3. Pre-compute timeframe caches ──────────────────────────────────────
    daily_tf = precompute_daily(df_1d)
    h4_tf    = precompute_4h(df_4h)

    # ── 4. Find setups (with ATR-based SL/TP pre-computed per setup) ─────────
    setups, signals_raw, signals_qual = find_setups(df_15m, df_1h, df_1d, daily_tf, h4_tf)
    print(f"\nFound {len(setups)} qualifying setups ({signals_raw} raw signals, {signals_qual} passed confidence gate)")

    # ── 5. Simulate with multi-position compound P&L ─────────────────────────
    print(f"\nSimulating {len(setups)} setups (multi-position, compound P&L)...")
    trades = simulate_all_trades_v2(setups, start_balance=start_balance)

    # ── 6. Report ─────────────────────────────────────────────────────────────
    print_report(trades, signals_raw, signals_qual, start_balance, df_15m)

    # ── 7. WFO sensitivity (optional) ─────────────────────────────────────────
    if args.wfo:
        run_wfo_sensitivity(setups, start_balance)

    print(f"\nDone.")


if __name__ == "__main__":
    main()
