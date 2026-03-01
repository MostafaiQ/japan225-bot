"""
Japan 225 Bot — Full Strategy Backtest
=======================================
Simulates the complete pipeline on real historical Nikkei data:

  1. Fetch historical data (yfinance: NKD=F 15min + 1H, ^N225 Daily)
  2. Pre-compute daily and 4H timeframe analysis ONCE per unique period
  3. For each 15min candle: compute 15M tf, look up pre-computed daily/4H
  4. Run detect_setup() + compute_confidence() [same code as live bot]
  5. Record all qualifying setups with entry price and future candles
  6. WFO: test different SL/TP combos against the SAME pre-computed setups
     (no re-scanning needed — 10x faster WFO)

Data sources:
  NKD=F   (CME Nikkei 225 futures) — 15M + 1H, 23h/day multi-session coverage
  ^N225   (Nikkei cash) — Daily only, for EMA200 daily trend direction
  Sessions covered: Tokyo (00-06 UTC) + London (08-16 UTC) + New York (16-21 UTC)
  Skipped: 06-08 UTC gap (Tokyo/London crossover), 21-00 UTC (thin volume)

Instrument:   $1 per point, 1 contract (matches bot config)
Spread:       10pts round-trip deducted from each trade entry
EMA200:       250 daily candles fetched — EMA200 always available
              (matches live bot fix: DAILY_EMA200_CANDLES=250)

Output:
  - Signal funnel (candles scanned → setups → qualified → trades)
  - Trade log and P&L summary
  - By direction / session / exit reason / confidence tier
  - WFO parameter grid search (SL/TP combinations, fast)
"""
import sys
import os
import logging
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

# Suppress EMA200-unavailable warnings — expected for 55-candle 15M/4H inputs
logging.disable(logging.WARNING)

import yfinance as yf
import pandas as pd

from core.indicators import analyze_timeframe, detect_setup
from core.confidence import compute_confidence
from config.settings import SESSION_HOURS_UTC, MINUTE_5_CANDLES

logging.disable(logging.NOTSET)  # re-enable after imports

# ── Config ─────────────────────────────────────────────────────────────────────

SPREAD_PTS        = 10       # round-trip spread (5pts each side)
CONTRACT_SIZE     = 1        # $1/pt
SL_DISTANCE       = 150      # pts (WFO-validated — matches DEFAULT_SL_DISTANCE in settings.py)
TP_DISTANCE       = 400      # pts
BREAKEVEN_TRIGGER = 150      # move SL to entry+10 when profit hits this
BREAKEVEN_BUFFER  = 10       # SL moves to entry + this
TRAILING_DISTANCE = 150      # pts trailing stop (runner phase)
MIN_CANDLES_15M   = 55       # need 50+ for EMA50 on 15M
MIN_CANDLES_DAILY = 250      # need 200+ for EMA200 on daily (matches DAILY_EMA200_CANDLES)
MIN_CANDLES_4H    = 55       # need 50+ for EMA50 on 4H

AI_COST_PER_SIGNAL_USD = 0.015   # ~$0.015 per Sonnet+Opus call

# ── Session filter (UTC hours) ──────────────────────────────────────────────────
# Single source of truth from settings.py — covers Tokyo + London + New York
SESSIONS = SESSION_HOURS_UTC

def get_session(dt: datetime) -> str | None:
    h = dt.hour
    for name, (start, end) in SESSIONS.items():
        if start <= h < end:
            return name
    return None  # 06-08 UTC gap and 21-00 UTC thin — skip

# ── Data download ───────────────────────────────────────────────────────────────

def download_data():
    print("Downloading Nikkei data from Yahoo Finance...")
    # NKD=F: CME Nikkei 225 futures — 23h/day, covers Tokyo + London + NY
    df_5m  = yf.download("NKD=F", interval="5m",  period="60d",  progress=False, auto_adjust=True)
    df_15m = yf.download("NKD=F", interval="15m", period="60d",  progress=False, auto_adjust=True)
    df_1h  = yf.download("NKD=F", interval="1h",  period="730d", progress=False, auto_adjust=True)
    # ^N225 daily: cash market closes, most reliable for EMA200 daily trend direction
    df_1d  = yf.download("^N225", interval="1d",  period="2y",   progress=False, auto_adjust=True)
    for df in [df_5m, df_15m, df_1h, df_1d]:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
    if df_15m.empty:
        raise RuntimeError("NKD=F 15M data empty — check Yahoo Finance ticker")
    if not df_5m.empty:
        print(f"  NKD=F 5min:  {len(df_5m)} ({df_5m.index[0].date()} -> {df_5m.index[-1].date()})")
    else:
        print("  NKD=F 5min:  empty (5M confirmation disabled)")
    print(f"  NKD=F 15min: {len(df_15m)} ({df_15m.index[0].date()} -> {df_15m.index[-1].date()})")
    print(f"  NKD=F 1H:    {len(df_1h)}")
    print(f"  ^N225 Daily: {len(df_1d)}")
    return df_15m, df_1h, df_5m, df_1d

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

# ── Pre-compute timeframes once per unique period ───────────────────────────────

def precompute_daily(df_1d: pd.DataFrame) -> dict:
    """
    Pre-compute analyze_timeframe() for each unique trading date.
    Returns dict: date -> tf_daily dict (or None if < MIN_CANDLES_DAILY).
    Each entry uses all daily candles UP TO AND INCLUDING that date.
    """
    print(f"Pre-computing daily timeframes ({len(df_1d)} candles)...")
    daily_tf = {}
    dates = df_1d.index.normalize()
    for i in range(len(df_1d)):
        date = df_1d.index[i].date()
        candles = df_to_candles(df_1d.iloc[:i+1])
        if len(candles) < MIN_CANDLES_DAILY:
            daily_tf[date] = None
            continue
        # use last 250 candles so EMA200 always computed
        logging.disable(logging.WARNING)
        try:
            tf = analyze_timeframe(candles[-250:])
        except Exception:
            tf = None
        logging.disable(logging.NOTSET)
        daily_tf[date] = tf
    valid = sum(1 for v in daily_tf.values() if v is not None)
    print(f"  Daily cache: {valid}/{len(daily_tf)} dates have EMA200 available")
    return daily_tf


def precompute_4h(df_4h: pd.DataFrame) -> dict:
    """
    Pre-compute analyze_timeframe() for each 4H bar.
    Returns dict: 4H-bar-timestamp -> tf_4h dict.
    Each entry uses all 4H candles UP TO AND INCLUDING that bar.
    """
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


def precompute_5m(df_5m: pd.DataFrame) -> dict:
    """
    Pre-compute analyze_timeframe() for each 5M bar.
    Uses MINUTE_5_CANDLES lookback (sufficient for EMA9 / RSI14 / BB20).
    Returns dict: 5M-bar-timestamp -> tf_5m dict (or None if insufficient candles).
    """
    if df_5m.empty:
        print("  5M cache: skipped (empty data)")
        return {}
    print(f"Pre-computing 5M timeframes ({len(df_5m)} candles, lookback={MINUTE_5_CANDLES})...")
    h5m_tf = {}
    for i in range(len(df_5m)):
        ts = df_5m.index[i]
        if i < MINUTE_5_CANDLES:
            h5m_tf[ts] = None
            continue
        candles = df_to_candles(df_5m.iloc[max(0, i - MINUTE_5_CANDLES + 1):i + 1])
        logging.disable(logging.WARNING)
        try:
            tf = analyze_timeframe(candles)
        except Exception:
            tf = None
        logging.disable(logging.NOTSET)
        h5m_tf[ts] = tf
    valid = sum(1 for v in h5m_tf.values() if v is not None)
    print(f"  5M cache: {valid}/{len(h5m_tf)} bars computed")
    return h5m_tf

# ── Setup detection loop ────────────────────────────────────────────────────────

class SetupRecord:
    """A qualifying setup ready for trade simulation."""
    __slots__ = ("idx", "entry_time", "direction", "setup_type", "entry_price",
                 "confidence_score", "session", "future_candles")
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def find_setups(df_15m: pd.DataFrame, df_4h: pd.DataFrame,
                daily_tf: dict, h4_tf: dict,
                df_5m: pd.DataFrame = None, h5m_tf: dict = None) -> tuple[list[SetupRecord], int, int]:
    """
    Scan all 15M candles and collect qualifying setups.
    Returns: (setups, signals_raw_count, signals_qualified_count)
    """
    if h5m_tf is None:
        h5m_tf = {}

    # Ensure UTC index
    dfs_to_tz = [df_15m, df_4h]
    if df_5m is not None and not df_5m.empty:
        dfs_to_tz.append(df_5m)
    for df in dfs_to_tz:
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

    # Pre-sort 4H index for fast lookback
    h4_sorted = sorted(h4_tf.keys())

    setups: list[SetupRecord] = []
    signals_raw = 0
    signals_qual = 0
    total = len(df_15m)

    for i in range(MIN_CANDLES_15M, total - 30):
        ts = df_15m.index[i]
        ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

        # Session + weekend filter
        session = get_session(ts_utc)
        if session is None:
            continue
        if ts_utc.weekday() >= 5:
            continue

        # Look up pre-computed daily tf for this date
        date_key = ts_utc.date()
        tf_daily = daily_tf.get(date_key)
        if tf_daily is None:
            continue

        # Look up pre-computed 4H tf for the most recent 4H bar before this candle
        tf_4h = None
        for h4_ts in reversed(h4_sorted):
            if h4_ts <= ts_utc:
                tf_4h = h4_tf.get(h4_ts)
                break
        if tf_4h is None:
            continue

        # Compute 15M tf (only this one per candle)
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

        # Pre-screen (tf_5m not passed here — 5M is context for AI, not a code gate)
        setup = detect_setup(tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_15m)
        if not setup["found"]:
            continue
        signals_raw += 1

        # Confidence gate
        try:
            logging.disable(logging.WARNING)
            conf = compute_confidence(
                direction=setup["direction"],
                tf_daily=tf_daily,
                tf_4h=tf_4h,
                tf_15m=tf_15m,
                upcoming_events=[],
                web_research=None,
            )
            logging.disable(logging.NOTSET)
        except Exception:
            logging.disable(logging.NOTSET)
            continue
        if not conf["meets_threshold"]:
            continue
        signals_qual += 1

        # Collect future candles for exit simulation
        future_df = df_15m.iloc[i+1 : i+101]
        future_candles = df_to_candles(future_df)
        if len(future_candles) < 4:
            continue

        setups.append(SetupRecord(
            idx            = i,
            entry_time     = ts_utc,
            direction      = setup["direction"],
            setup_type     = setup["type"],
            entry_price    = float(tf_15m["price"]),
            confidence_score = conf["score"],
            session        = session,
            future_candles = future_candles,
        ))

        # Progress
        if len(setups) % 5 == 0 and len(setups) > 0:
            print(f"  ... scanned {i}/{total}, {signals_raw} signals, {len(setups)} setups so far")

    return setups, signals_raw, signals_qual


# ── Trade simulation ────────────────────────────────────────────────────────────

def simulate_trade(direction: str, entry_price: float, future_candles: list[dict],
                   sl_dist: int, tp_dist: int, be_trigger: int,
                   trail_dist: int) -> tuple[float, str, int]:
    sl = entry_price - sl_dist if direction == "LONG" else entry_price + sl_dist
    tp = entry_price + tp_dist if direction == "LONG" else entry_price - tp_dist
    be_triggered = False
    trailing_active = False
    peak_profit = 0.0

    for i, candle in enumerate(future_candles):
        high, low = candle["high"], candle["low"]

        if direction == "LONG":
            profit_at_high = high - entry_price
            if not be_triggered and profit_at_high >= be_trigger:
                sl = entry_price + BREAKEVEN_BUFFER
                be_triggered = True
            if be_triggered:
                if profit_at_high > peak_profit:
                    peak_profit = profit_at_high
                trail_sl = peak_profit - trail_dist + entry_price
                if trail_sl > sl:
                    sl = trail_sl
                    trailing_active = True
            if high >= tp:
                return tp - entry_price - SPREAD_PTS, "TP_HIT", i + 1
            if low <= sl:
                reason = "TRAILING_STOP" if trailing_active else ("BREAKEVEN" if be_triggered else "SL_HIT")
                return sl - entry_price - SPREAD_PTS, reason, i + 1
        else:
            profit_at_low = entry_price - low
            if not be_triggered and profit_at_low >= be_trigger:
                sl = entry_price - BREAKEVEN_BUFFER
                be_triggered = True
            if be_triggered:
                if profit_at_low > peak_profit:
                    peak_profit = profit_at_low
                trail_sl = entry_price - peak_profit + trail_dist
                if trail_sl < sl:
                    sl = trail_sl
                    trailing_active = True
            if low <= tp:
                return entry_price - tp - SPREAD_PTS, "TP_HIT", i + 1
            if high >= sl:
                reason = "TRAILING_STOP" if trailing_active else ("BREAKEVEN" if be_triggered else "SL_HIT")
                return entry_price - sl - SPREAD_PTS, reason, i + 1

    last_close = future_candles[-1]["close"]
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return pnl - SPREAD_PTS, "END_OF_DATA", len(future_candles)


class TradeResult:
    __slots__ = ("entry_time", "direction", "entry_price", "exit_price",
                 "exit_reason", "pnl_pts", "pnl_usd", "confidence_score",
                 "session", "setup_type", "duration_candles")
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def simulate_all_trades(setups: list[SetupRecord],
                        sl_dist: int = SL_DISTANCE, tp_dist: int = TP_DISTANCE,
                        be_trigger: int = BREAKEVEN_TRIGGER,
                        trail_dist: int = TRAILING_DISTANCE) -> list[TradeResult]:
    """Run trade simulation for all setups. No re-scanning needed."""
    trades = []
    skip_until_idx = -1

    for s in sorted(setups, key=lambda x: x.idx):
        if s.idx < skip_until_idx:
            continue  # skip overlapping trades
        pnl_pts, reason, duration = simulate_trade(
            s.direction, s.entry_price, s.future_candles,
            sl_dist, tp_dist, be_trigger, trail_dist
        )
        exit_price = s.entry_price + (pnl_pts if s.direction == "LONG" else -pnl_pts)
        trades.append(TradeResult(
            entry_time       = s.entry_time,
            direction        = s.direction,
            entry_price      = round(s.entry_price, 1),
            exit_price       = round(exit_price, 1),
            exit_reason      = reason,
            pnl_pts          = round(pnl_pts, 1),
            pnl_usd          = round(pnl_pts * CONTRACT_SIZE, 2),
            confidence_score = s.confidence_score,
            session          = s.session,
            setup_type       = s.setup_type,
            duration_candles = duration,
        ))
        skip_until_idx = s.idx + duration + 1

    return trades


# ── Reporting ───────────────────────────────────────────────────────────────────

def print_report(trades: list[TradeResult], signals_raw: int, signals_qual: int,
                 df_15m: pd.DataFrame, sl_dist=SL_DISTANCE, tp_dist=TP_DISTANCE):

    n = len(trades)
    print("\n" + "="*62)
    print("  JAPAN 225 BOT — BACKTEST RESULTS")
    print("="*62)
    print(f"  Period:   {df_15m.index[0].date()} -> {df_15m.index[-1].date()}")
    print(f"  Data:     ^N225, 15min | EMA200: 250 daily candles (guaranteed)")
    print(f"  Spread:   {SPREAD_PTS}pts round-trip | SL: {sl_dist}pts | TP: {tp_dist}pts")
    print("-"*62)
    print(f"\n  SIGNAL FUNNEL")
    print(f"  15M candles scanned:      {len(df_15m)}")
    print(f"  Raw setups (pre-screen):  {signals_raw}")
    if signals_raw:
        print(f"  Passed confidence gate:   {signals_qual}  ({signals_qual/signals_raw*100:.0f}% of setups)")
    print(f"  Trades executed:          {n}  (no AI filter — worst case)")
    ai_cost = signals_qual * AI_COST_PER_SIGNAL_USD
    print(f"  Est. AI cost (60d):       ${ai_cost:.2f}")

    if n == 0:
        print("\n  NO TRADES — strategy never triggered at current thresholds.")
        _print_zero_trade_diagnosis(signals_raw, signals_qual)
        return

    wins   = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    total_pnl = sum(t.pnl_usd for t in trades)
    breakeven_wr = 1 / (1 + tp_dist / sl_dist)

    print(f"\n  P&L SUMMARY")
    print(f"  Total trades:   {n}")
    print(f"  Wins:           {len(wins)}  ({len(wins)/n*100:.1f}%)")
    print(f"  Losses:         {len(losses)}  ({len(losses)/n*100:.1f}%)")
    print(f"  Breakeven WR:   {breakeven_wr*100:.0f}%  (for {sl_dist}SL/{tp_dist}TP 1:{tp_dist/sl_dist:.1f}RR)")
    print(f"  Total P&L:      ${total_pnl:+.2f}")
    print(f"  Avg P&L/trade:  ${total_pnl/n:+.2f}")
    if wins:
        print(f"  Avg win:        ${sum(t.pnl_usd for t in wins)/len(wins):+.2f}")
    if losses:
        print(f"  Avg loss:       ${sum(t.pnl_usd for t in losses)/len(losses):+.2f}")
    print(f"  Best trade:     ${max(t.pnl_usd for t in trades):+.2f}")
    print(f"  Worst trade:    ${min(t.pnl_usd for t in trades):+.2f}")

    avg_duration = sum(t.duration_candles for t in trades) / n
    print(f"  Avg hold time:  {avg_duration:.0f} candles ({avg_duration*15/60:.1f} hours)")

    # Max drawdown
    running, peak, max_dd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda x: x.entry_time):
        running += t.pnl_usd
        if running > peak: peak = running
        dd = peak - running
        if dd > max_dd: max_dd = dd
    print(f"  Max drawdown:   ${max_dd:.2f}")

    gp = sum(t.pnl_usd for t in wins) if wins else 0
    gl = abs(sum(t.pnl_usd for t in losses)) if losses else 0.01
    pf = gp / gl
    print(f"  Profit factor:  {pf:.2f}  (>1.5 = healthy, >1.0 = profitable)")
    print(f"  Net (after AI): ${total_pnl - ai_cost:+.2f}")

    print(f"\n  BY DIRECTION")
    for d in ["LONG", "SHORT"]:
        dt = [t for t in trades if t.direction == d]
        if not dt: continue
        dw = [t for t in dt if t.pnl_usd > 0]
        print(f"  {d}: {len(dt)} trades | {len(dw)/len(dt)*100:.0f}% win | P&L: ${sum(t.pnl_usd for t in dt):+.2f}")

    print(f"\n  BY SESSION")
    for sess in ["Tokyo", "London", "New York"]:
        st = [t for t in trades if t.session == sess]
        if not st: continue
        sw = [t for t in st if t.pnl_usd > 0]
        print(f"  {sess}: {len(st)} trades | {len(sw)/len(st)*100:.0f}% win | P&L: ${sum(t.pnl_usd for t in st):+.2f}")

    print(f"\n  BY EXIT REASON")
    reasons = defaultdict(list)
    for t in trades: reasons[t.exit_reason].append(t)
    for r, rt in sorted(reasons.items()):
        print(f"  {r:<20} {len(rt):>3} trades | P&L: ${sum(t.pnl_usd for t in rt):+.2f}")

    print(f"\n  BY SETUP TYPE")
    setups_d = defaultdict(list)
    for t in trades: setups_d[t.setup_type].append(t)
    for st_name, sl in setups_d.items():
        sw2 = [t for t in sl if t.pnl_usd > 0]
        print(f"  {st_name:<30} {len(sl):>3} trades | {len(sw2)/len(sl)*100:.0f}% win | ${sum(t.pnl_usd for t in sl):+.2f}")

    print(f"\n  CONFIDENCE DISTRIBUTION")
    for lo, hi in [(70, 80), (80, 90), (90, 101)]:
        ct = [t for t in trades if lo <= t.confidence_score < hi]
        if not ct: continue
        cw = [t for t in ct if t.pnl_usd > 0]
        print(f"  Score {lo}-{hi-1}%: {len(ct):>3} trades | {len(cw)/len(ct)*100:.0f}% win | ${sum(t.pnl_usd for t in ct):+.2f}")

    print(f"\n  TRADE LOG (all trades)")
    print(f"  {'Date':>10}  {'Dir':>5}  {'Entry':>7}  {'P&L':>8}  {'Conf':>5}  {'Hold':>5}  Reason")
    print("  " + "-"*70)
    for t in sorted(trades, key=lambda x: x.entry_time):
        print(
            f"  {t.entry_time.strftime('%Y-%m-%d'):>10}  {t.direction:>5}  "
            f"{t.entry_price:>7.0f}  ${t.pnl_usd:>+7.2f}  {t.confidence_score:>4}%  "
            f"{t.duration_candles*15/60:>4.1f}h  {t.exit_reason}"
        )

    # Verdict
    print("\n" + "="*62)
    print("  VERDICT")
    print("="*62)
    win_rate = len(wins) / n
    if n < 10:
        print(f"  ⚠  Only {n} trades — statistically thin. More data needed.")
    if win_rate > breakeven_wr and pf > 1.0:
        print(f"  ✅ POSITIVE EDGE: WR {win_rate*100:.0f}% > breakeven {breakeven_wr*100:.0f}%, PF={pf:.2f}")
        if pf >= 1.5:
            print(f"  ✅ PF={pf:.2f} >= 1.5 — strategy is viable for live capital")
        else:
            print(f"  ⚠  PF={pf:.2f} < 1.5 — profitable but thin margin, keep monitoring")
    else:
        deficit = breakeven_wr - win_rate
        print(f"  ❌ BELOW BREAKEVEN: WR {win_rate*100:.0f}% (need {breakeven_wr*100:.0f}%), PF={pf:.2f}")
        print(f"     Win rate deficit: {deficit*100:.1f}pp. Do NOT go live.")


def _print_zero_trade_diagnosis(signals_raw: int, signals_qual: int):
    if signals_raw == 0:
        print("""
  Possible causes:
  1. daily_bullish gate: above_ema200_fallback may be None for all daily candles
     (check that 250 daily candles were fetched and EMA200 computes)
  2. bounce_starting gate: price > prev_close never triggered in dataset
  3. RSI zone 35-48 never hit during this 60-day window
""")
    elif signals_qual == 0:
        print(f"""
  {signals_raw} raw signals found but NONE passed confidence gate (>=70% LONG / >=75% SHORT).
  Most likely failing criteria:
  - daily_trend (C1): above/below EMA200 daily — verify EMA200 is computed
  - entry_level (C2): within 150pts of BB mid or EMA50
  - tp_viable  (C4): price <= bb_mid for LONG / price >= bb_mid for SHORT
""")


# ── WFO Parameter Grid Search (fast — no re-scanning) ──────────────────────────

def run_wfo_grid(setups: list[SetupRecord], df_15m: pd.DataFrame):
    """
    Walk-Forward Optimization on pre-computed setups.
    IS = first 75% of setups, OOS = last 25%.
    Fast because no re-scanning — just re-simulates trade exits.
    """
    if len(setups) < 5:
        print(f"\n  WFO skipped — only {len(setups)} setups (need >= 5)")
        return

    total = len(setups)
    split = int(total * 0.75)
    is_setups  = sorted(setups, key=lambda x: x.idx)[:split]
    oos_setups = sorted(setups, key=lambda x: x.idx)[split:]

    param_grid = [
        (sl, tp) for sl in [150, 200, 250, 300]
                 for tp in [300, 400, 500, 600]
                 if tp / sl >= 1.5
    ]

    print("\n" + "="*62)
    print("  WALK-FORWARD OPTIMIZATION (fast — no re-scanning)")
    print(f"  IS:  {len(is_setups)} setups | OOS: {len(oos_setups)} setups")
    print("="*62)
    print(f"\n  {'SL':>4}  {'TP':>4}  {'RR':>4}  {'Trades':>6}  {'WR':>7}  {'PF':>5}  {'P&L':>8}")
    print("  " + "-"*55)

    is_results = []
    for sl, tp in param_grid:
        be = min(sl - 50, 100)
        trail = max(sl - 50, 100)
        trades_is = simulate_all_trades(is_setups, sl, tp, be, trail)
        if not trades_is:
            print(f"  {sl:>4}  {tp:>4}  {tp/sl:.1f}     0     —      —         —")
            continue
        wins = [t for t in trades_is if t.pnl_usd > 0]
        wr = len(wins) / len(trades_is)
        gp = sum(t.pnl_usd for t in wins) if wins else 0
        gl = abs(sum(t.pnl_usd for t in trades_is if t.pnl_usd <= 0)) or 0.01
        pf = gp / gl
        pnl = sum(t.pnl_usd for t in trades_is)
        is_results.append((pf, wr, pnl, sl, tp, len(trades_is)))
        print(f"  {sl:>4}  {tp:>4}  {tp/sl:.1f}  {len(trades_is):>6}  {wr*100:>6.1f}%  {pf:>5.2f}  ${pnl:>+7.2f}")

    if not is_results:
        print("  No IS results generated.")
        return

    # Best IS by profit factor (min 3 trades)
    qualified = [(pf, wr, pnl, sl, tp, n) for pf, wr, pnl, sl, tp, n in is_results if n >= 3]
    if not qualified: qualified = is_results
    best_pf, best_wr, best_pnl, best_sl, best_tp, best_n = max(qualified, key=lambda x: x[0])
    be_best = min(best_sl - 50, 100)
    trail_best = max(best_sl - 50, 100)

    print(f"\n  Best IS:  SL={best_sl} TP={best_tp} (RR={best_tp/best_sl:.1f})")
    print(f"  IS:  {best_n} trades | {best_wr*100:.1f}% win | PF={best_pf:.2f} | P&L=${best_pnl:+.2f}")

    trades_oos = simulate_all_trades(oos_setups, best_sl, best_tp, be_best, trail_best)
    if trades_oos:
        wins_oos = [t for t in trades_oos if t.pnl_usd > 0]
        wr_oos = len(wins_oos) / len(trades_oos)
        gp_oos = sum(t.pnl_usd for t in wins_oos) if wins_oos else 0
        gl_oos = abs(sum(t.pnl_usd for t in trades_oos if t.pnl_usd <= 0)) or 0.01
        pf_oos = gp_oos / gl_oos
        pnl_oos = sum(t.pnl_usd for t in trades_oos)
        breakeven_wr = 1 / (1 + best_tp / best_sl)
        print(f"  OOS: {len(trades_oos)} trades | {wr_oos*100:.1f}% win | PF={pf_oos:.2f} | P&L=${pnl_oos:+.2f}")
        degrad = (best_pf - pf_oos) / best_pf * 100 if best_pf > 0 else 0
        print(f"  PF degradation IS→OOS: {degrad:+.1f}% (<40% = acceptable)")
        if wr_oos >= breakeven_wr:
            print(f"  ✅ OOS WR {wr_oos*100:.1f}% >= breakeven {breakeven_wr*100:.0f}% — strategy generalises")
        else:
            print(f"  ❌ OOS WR {wr_oos*100:.1f}% < breakeven {breakeven_wr*100:.0f}% — overfitting risk")
    else:
        print(f"  OOS: 0 trades.")


# ── Entry point ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time
    from core.confidence import BB_MID_THRESHOLD_PTS, EMA50_THRESHOLD_PTS

    print("Japan 225 Bot — Backtest")
    print(f"Thresholds: BB_MID={BB_MID_THRESHOLD_PTS}pts  EMA50={EMA50_THRESHOLD_PTS}pts")
    print(f"Daily candles: {MIN_CANDLES_DAILY} (EMA200 guaranteed)\n")

    t_start = time.time()

    # 1. Download
    df_15m, df_1h, df_5m, df_1d = download_data()
    df_4h = resample_4h(df_1h)
    print(f"  4H candles: {len(df_4h)} (resampled from 1H)")

    # 2. Pre-compute daily and 4H (done ONCE each)
    # Note: 5M precomputed via precompute_5m() — enable when 5M added to confidence scoring
    for df in [df_15m, df_4h, df_1d]:
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

    daily_tf = precompute_daily(df_1d)
    h4_tf    = precompute_4h(df_4h)

    print(f"\nScanning {len(df_15m)} 15M candles for setups...")
    setups, signals_raw, signals_qual = find_setups(df_15m, df_4h, daily_tf, h4_tf)
    t_scan = time.time() - t_start
    print(f"Scan complete: {len(setups)} qualifying setups found in {t_scan:.1f}s\n")

    # 3. Simulate baseline trades
    trades = simulate_all_trades(setups)
    print_report(trades, signals_raw, signals_qual, df_15m)

    # 4. WFO (fast — no re-scanning)
    run_wfo_grid(setups, df_15m)

    print(f"\nTotal runtime: {time.time()-t_start:.1f}s")
