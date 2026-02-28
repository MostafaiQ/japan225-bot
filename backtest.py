"""
Japan 225 Bot — Full Strategy Backtest
=======================================
Simulates the complete pipeline on real historical Nikkei data:

  1. Fetch historical data (yfinance: 15min + 4H + Daily)
  2. For each 15min candle: run detect_setup() [same code as live bot]
  3. If setup found: run compute_confidence() [same code as live bot]
  4. If confidence passes threshold: simulate trade
  5. Exit management: breakeven at +150pts, trailing stop 150pts, TP 400pts, SL 200pts

Data source:  ^N225 (Nikkei 225 cash index, Yahoo Finance)
Instrument:   $1 per point, 1 contract (matches bot config)
Spread:       10pts round-trip deducted from each trade entry
Session:      Tokyo / London / New York only (same session logic as live bot)

Output:
  - Trade log (every simulated trade)
  - Summary stats by direction, session, confidence tier
  - WFO parameter grid search (SL/TP combinations)
  - Adjustment recommendations

Thresholds: Live code now correctly calibrated at 150pts for Nikkei ~50k-60k.
  BB_MID_THRESHOLD_PTS = 150 (was 30 — fixed 2026-02-28 via expert agent analysis)
  EMA50_THRESHOLD_PTS  = 150 (was 30 — same fix)
  C4 tp_viable: 350pts (was 100 — now a genuine filter vs. trivially passing 78% of candles)
"""
import sys
import os
from datetime import datetime, timezone, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

import yfinance as yf
import pandas as pd

from core.indicators import analyze_timeframe, detect_setup
from core.confidence import compute_confidence, format_confidence_breakdown


# detect_setup() is now imported directly from core.indicators (thresholds corrected to 150pts)

# ── Config ─────────────────────────────────────────────────────────────────────

SPREAD_PTS       = 10       # round-trip spread (5pts each side)
CONTRACT_SIZE    = 1        # $1/pt
SL_DISTANCE      = 200      # pts
TP_DISTANCE      = 400      # pts
BREAKEVEN_TRIGGER = 150     # move SL to entry+10 when profit hits this
BREAKEVEN_BUFFER  = 10      # SL moves to entry + this
TRAILING_DISTANCE = 150     # pts trailing stop (runner phase)
MIN_CANDLES_15M   = 55      # need 50+ for EMA50 on 15M
MIN_CANDLES_DAILY = 210     # need 200+ for EMA200 on daily
MIN_CANDLES_4H    = 55      # need 50+ for EMA50 on 4H

# API cost constants (per trade that escalates to AI)
AI_COST_PER_SIGNAL_USD = 0.015   # ~$0.015 per Sonnet+Opus call (prompt caching)

# ── Session filter (UTC hours) ──────────────────────────────────────────────────
SESSIONS = {
    "Tokyo":   (0,  6),    # 00:00–06:00 UTC
    "London":  (7,  16),   # 07:00–16:00 UTC
    "New York": (13, 21),  # 13:00–21:00 UTC
}

def get_session(dt: datetime) -> str | None:
    """Return session name if active, else None. dt must be UTC-aware."""
    h = dt.hour
    for name, (start, end) in SESSIONS.items():
        if start <= h < end:
            return name
    return None

# ── Data download ───────────────────────────────────────────────────────────────

def download_data():
    print("Downloading Nikkei 225 data from Yahoo Finance...")

    # 15min data (max 60 days from yfinance)
    df_15m = yf.download("^N225", interval="15m", period="60d", progress=False, auto_adjust=True)

    # 1H data (used to build 4H candles, last 730d available)
    df_1h = yf.download("^N225", interval="1h", period="730d", progress=False, auto_adjust=True)

    # Daily data (for EMA200 trend, last 2 years)
    df_1d = yf.download("^N225", interval="1d", period="2y", progress=False, auto_adjust=True)

    # Flatten MultiIndex columns if present
    for df in [df_15m, df_1h, df_1d]:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

    print(f"  15min candles: {len(df_15m)} ({df_15m.index[0].date()} → {df_15m.index[-1].date()})")
    print(f"  1H candles:    {len(df_1h)}")
    print(f"  Daily candles: {len(df_1d)}")

    return df_15m, df_1h, df_1d


def resample_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Resample 1H OHLCV into 4H candles."""
    df = df_1h.copy()
    df.index = pd.to_datetime(df.index)
    df_4h = df.resample("4h").agg({
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }).dropna()
    return df_4h


def df_to_candles(df: pd.DataFrame, n: int) -> list[dict]:
    """Convert last n rows of a DataFrame to the candle dict format."""
    subset = df.iloc[-n:]
    candles = []
    for ts, row in subset.iterrows():
        candles.append({
            "timestamp": ts,
            "open":   float(row["Open"]),
            "high":   float(row["High"]),
            "low":    float(row["Low"]),
            "close":  float(row["Close"]),
            "volume": float(row.get("Volume", 0) or 0),
        })
    return candles


# ── Trade simulation ────────────────────────────────────────────────────────────

class TradeResult:
    __slots__ = ("entry_time", "direction", "entry_price", "exit_price",
                 "exit_reason", "pnl_pts", "pnl_usd", "confidence_score",
                 "session", "setup_type", "duration_candles")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def simulate_trade(direction: str, entry_price: float, entry_time,
                   future_candles: list[dict]) -> tuple[float, str, int]:
    """
    Simulate trade exit using future 15min candles.
    Returns: (exit_price, exit_reason, candles_held)
    """
    sl = entry_price - SL_DISTANCE if direction == "LONG" else entry_price + SL_DISTANCE
    tp = entry_price + TP_DISTANCE if direction == "LONG" else entry_price - TP_DISTANCE
    be_triggered = False
    trailing_active = False
    peak_profit = 0.0

    for i, candle in enumerate(future_candles):
        high = candle["high"]
        low  = candle["low"]

        if direction == "LONG":
            # Check profit at high
            profit_at_high = high - entry_price

            # Breakeven trigger
            if not be_triggered and profit_at_high >= BREAKEVEN_TRIGGER:
                sl = entry_price + BREAKEVEN_BUFFER
                be_triggered = True

            # Trailing stop
            if be_triggered:
                if profit_at_high > peak_profit:
                    peak_profit = profit_at_high
                trailing_sl = peak_profit - TRAILING_DISTANCE + entry_price
                if trailing_sl > sl:
                    sl = trailing_sl
                    trailing_active = True

            # Check TP
            if high >= tp:
                return tp - entry_price - SPREAD_PTS, "TP_HIT", i + 1

            # Check SL
            if low <= sl:
                exit = sl
                reason = "TRAILING_STOP" if trailing_active else ("BREAKEVEN" if be_triggered else "SL_HIT")
                return exit - entry_price - SPREAD_PTS, reason, i + 1

        else:  # SHORT
            profit_at_low = entry_price - low

            if not be_triggered and profit_at_low >= BREAKEVEN_TRIGGER:
                sl = entry_price - BREAKEVEN_BUFFER
                be_triggered = True

            if be_triggered:
                if profit_at_low > peak_profit:
                    peak_profit = profit_at_low
                trailing_sl = entry_price - peak_profit + TRAILING_DISTANCE
                if trailing_sl < sl:
                    sl = trailing_sl
                    trailing_active = True

            if low <= tp:
                return entry_price - tp - SPREAD_PTS, "TP_HIT", i + 1

            if high >= sl:
                exit = sl
                reason = "TRAILING_STOP" if trailing_active else ("BREAKEVEN" if be_triggered else "SL_HIT")
                return entry_price - exit - SPREAD_PTS, reason, i + 1

    # End of data — close at last candle's close
    last_close = future_candles[-1]["close"]
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return pnl - SPREAD_PTS, "END_OF_DATA", len(future_candles)


# ── Main backtest loop ──────────────────────────────────────────────────────────

def run_backtest(df_15m: pd.DataFrame, df_4h: pd.DataFrame, df_1d: pd.DataFrame):
    df_15m = df_15m.copy()
    df_4h  = df_4h.copy()
    df_1d  = df_1d.copy()

    # Ensure UTC-aware index
    for df in [df_15m, df_4h, df_1d]:
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

    trades: list[TradeResult] = []
    signals_found = 0
    signals_qualifying = 0
    skip_until_idx = -1           # prevent overlapping trades

    total_candles = len(df_15m)

    for i in range(MIN_CANDLES_15M, total_candles - 30):
        # Skip if we're inside an open trade
        if i < skip_until_idx:
            continue

        ts = df_15m.index[i]
        ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

        # Session filter
        session = get_session(ts_utc)
        if session is None:
            continue

        # Skip weekends
        if ts_utc.weekday() >= 5:
            continue

        # ── Build timeframe inputs ──────────────────────────────────────
        candles_15m  = df_to_candles(df_15m, i + 1)[-55:]

        # Daily: all daily candles up to this date
        daily_mask = df_1d.index.date <= ts_utc.date()
        df_daily_slice = df_1d[daily_mask]
        if len(df_daily_slice) < MIN_CANDLES_DAILY:
            continue
        candles_daily = df_to_candles(df_daily_slice, len(df_daily_slice))[-210:]

        # 4H: all 4H candles up to this timestamp
        h4_mask = df_4h.index <= ts_utc
        df_4h_slice = df_4h[h4_mask]
        if len(df_4h_slice) < MIN_CANDLES_4H:
            continue
        candles_4h = df_to_candles(df_4h_slice, len(df_4h_slice))[-55:]

        # ── Analyze all timeframes ──────────────────────────────────────
        try:
            tf_15m   = analyze_timeframe(candles_15m)
            tf_daily = analyze_timeframe(candles_daily)
            tf_4h    = analyze_timeframe(candles_4h)
        except Exception:
            continue

        # ── Pre-screen: detect_setup() (live code — thresholds now calibrated) ─
        setup = detect_setup(tf_daily=tf_daily, tf_4h=tf_4h, tf_15m=tf_15m)
        if not setup["found"]:
            continue

        signals_found += 1

        # ── Confidence scoring ──────────────────────────────────────────
        try:
            conf = compute_confidence(
                direction=setup["direction"],
                tf_daily=tf_daily,
                tf_4h=tf_4h,
                tf_15m=tf_15m,
                upcoming_events=[],
                web_research=None,
            )
        except Exception:
            continue

        if not conf["meets_threshold"]:
            continue

        signals_qualifying += 1
        score = conf["score"]

        # ── Simulate trade ──────────────────────────────────────────────
        entry_price = tf_15m["price"]
        direction   = setup["direction"]

        # Future candles for exit simulation (up to 100 candles ahead = ~25 hours)
        future_candles = df_to_candles(df_15m.iloc[i+1 : i+101], min(100, total_candles - i - 1))
        if len(future_candles) < 4:
            continue

        pnl_pts, exit_reason, duration = simulate_trade(
            direction, entry_price, ts_utc, future_candles
        )

        trades.append(TradeResult(
            entry_time       = ts_utc,
            direction        = direction,
            entry_price      = round(entry_price, 1),
            exit_price       = round(entry_price + (pnl_pts if direction == "LONG" else -pnl_pts), 1),
            exit_reason      = exit_reason,
            pnl_pts          = round(pnl_pts, 1),
            pnl_usd          = round(pnl_pts * CONTRACT_SIZE, 2),
            confidence_score = score,
            session          = session,
            setup_type       = setup["type"],
            duration_candles = duration,
        ))

        # Skip ahead past this trade's duration to avoid overlapping signals
        skip_until_idx = i + duration + 1

        if len(trades) % 10 == 0:
            print(f"  ... {i}/{total_candles} candles scanned, {len(trades)} trades so far")

    return trades, signals_found, signals_qualifying


# ── Reporting ───────────────────────────────────────────────────────────────────

def print_report(trades: list[TradeResult], signals_found: int, signals_qualifying: int,
                 df_15m: pd.DataFrame):

    if not trades:
        print("\n⚠️  NO TRADES GENERATED — strategy never triggered. See adjustment section below.")
        print_adjustments(trades, signals_found, signals_qualifying)
        return

    wins   = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    total_pnl  = sum(t.pnl_usd for t in trades)
    total_risk = len(trades) * SL_DISTANCE * CONTRACT_SIZE  # max possible loss

    print("\n" + "═"*60)
    print("  JAPAN 225 BOT — BACKTEST RESULTS")
    print("═"*60)
    print(f"  Period:         {df_15m.index[0].date()} → {df_15m.index[-1].date()}")
    print(f"  Data:           ^N225, 15min candles")
    print(f"  Contract:       $1/pt, 1 lot | Spread: {SPREAD_PTS}pts deducted")
    print(f"  SL: {SL_DISTANCE}pts | TP: {TP_DISTANCE}pts | BE trigger: {BREAKEVEN_TRIGGER}pts")
    print("─"*60)

    print("\n📊  SIGNAL FUNNEL")
    print(f"  15min candles scanned:    {len(df_15m)}")
    print(f"  Setups found (pre-screen):{signals_found:>8}")
    print(f"  Passed confidence gate:   {signals_qualifying:>8}  ({signals_qualifying/max(signals_found,1)*100:.0f}% of setups)")
    print(f"  Trades simulated:         {len(trades):>8}  (no AI filter — all qualifying signals taken)")
    est_ai_cost = signals_qualifying * AI_COST_PER_SIGNAL_USD
    print(f"  Est. AI API cost:         ${est_ai_cost:.2f}  ({signals_qualifying} Sonnet+Opus calls @ ~$0.015 each)")

    print("\n💰  P&L SUMMARY")
    print(f"  Total trades:   {len(trades)}")
    print(f"  Wins:           {len(wins)}  ({len(wins)/len(trades)*100:.1f}%)")
    print(f"  Losses:         {len(losses)}  ({len(losses)/len(trades)*100:.1f}%)")
    print(f"  Total P&L:      ${total_pnl:+.2f}")
    print(f"  Avg P&L/trade:  ${total_pnl/len(trades):+.2f}")
    if wins:
        print(f"  Avg win:        ${sum(t.pnl_usd for t in wins)/len(wins):+.2f}")
    if losses:
        print(f"  Avg loss:       ${sum(t.pnl_usd for t in losses)/len(losses):+.2f}")
    print(f"  Best trade:     ${max(t.pnl_usd for t in trades):+.2f}")
    print(f"  Worst trade:    ${min(t.pnl_usd for t in trades):+.2f}")

    # Max drawdown
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.entry_time):
        running += t.pnl_usd
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    print(f"  Max drawdown:   ${max_dd:.2f}")

    # Profit factor
    gross_profit = sum(t.pnl_usd for t in wins) if wins else 0
    gross_loss   = abs(sum(t.pnl_usd for t in losses)) if losses else 0.01
    pf = gross_profit / gross_loss
    print(f"  Profit factor:  {pf:.2f}  (>1.5 = healthy)")

    # Net after AI cost
    net = total_pnl - est_ai_cost
    print(f"  Net (after AI): ${net:+.2f}")

    print("\n📋  BY DIRECTION")
    for d in ["LONG", "SHORT"]:
        dt = [t for t in trades if t.direction == d]
        if not dt:
            print(f"  {d}: 0 trades")
            continue
        dw = [t for t in dt if t.pnl_usd > 0]
        dpnl = sum(t.pnl_usd for t in dt)
        print(f"  {d}: {len(dt)} trades | {len(dw)/len(dt)*100:.0f}% win | P&L: ${dpnl:+.2f}")

    print("\n📋  BY SESSION")
    for sess in ["Tokyo", "London", "New York"]:
        st = [t for t in trades if t.session == sess]
        if not st:
            print(f"  {sess}: 0 trades")
            continue
        sw = [t for t in st if t.pnl_usd > 0]
        spnl = sum(t.pnl_usd for t in st)
        print(f"  {sess}: {len(st)} trades | {len(sw)/len(st)*100:.0f}% win | P&L: ${spnl:+.2f}")

    print("\n📋  BY EXIT REASON")
    reasons = defaultdict(list)
    for t in trades:
        reasons[t.exit_reason].append(t)
    for reason, rt in sorted(reasons.items()):
        rpnl = sum(t.pnl_usd for t in rt)
        print(f"  {reason:<20} {len(rt):>3} trades | P&L: ${rpnl:+.2f}")

    print("\n📋  BY SETUP TYPE")
    setups = defaultdict(list)
    for t in trades:
        setups[t.setup_type].append(t)
    for st_name, st_list in setups.items():
        sw2 = [t for t in st_list if t.pnl_usd > 0]
        spnl2 = sum(t.pnl_usd for t in st_list)
        print(f"  {st_name:<30} {len(st_list):>3} trades | {len(sw2)/len(st_list)*100:.0f}% win | ${spnl2:+.2f}")

    print("\n📋  CONFIDENCE DISTRIBUTION")
    tiers = [(70, 80), (80, 90), (90, 101)]
    for lo, hi in tiers:
        ct = [t for t in trades if lo <= t.confidence_score < hi]
        if not ct:
            continue
        cw = [t for t in ct if t.pnl_usd > 0]
        cpnl = sum(t.pnl_usd for t in ct)
        print(f"  Score {lo}-{hi-1}%: {len(ct):>3} trades | {len(cw)/len(ct)*100:.0f}% win | ${cpnl:+.2f}")

    print("\n📋  TRADE LOG (last 20)")
    print(f"  {'Date':>10}  {'Dir':>5}  {'Entry':>7}  {'Exit':>7}  {'P&L':>8}  {'Conf':>5}  {'Reason'}")
    print("  " + "-"*75)
    for t in sorted(trades, key=lambda x: x.entry_time)[-20:]:
        print(
            f"  {t.entry_time.strftime('%Y-%m-%d'):>10}  {t.direction:>5}  "
            f"{t.entry_price:>7.0f}  {t.exit_price:>7.0f}  "
            f"${t.pnl_usd:>+7.2f}  {t.confidence_score:>4}%  {t.exit_reason}"
        )

    print_adjustments(trades, signals_found, signals_qualifying)


def print_adjustments(trades: list[TradeResult], signals_found: int, signals_qualifying: int):
    print("\n" + "═"*60)
    print("  ADJUSTMENT RECOMMENDATIONS")
    print("═"*60)

    total = len(trades)
    wins = [t for t in trades if t.pnl_usd > 0]
    win_rate = len(wins) / total if total > 0 else 0
    total_pnl = sum(t.pnl_usd for t in trades)

    # Signal frequency
    if signals_found == 0:
        print("""
  ❌ CRITICAL: Zero setups found.
     Root causes (check in order):
     1. daily_bullish always None? → check EMA200 data (need 200 daily candles)
     2. RSI never in zone? → current LONG: 35-60, SHORT: 55-75
        Try widening to LONG: 30-65, SHORT: 50-80 to get more signals
     3. Price never near BB mid / EMA50 (±30pts)?
        Try widening thresholds: BB_MID_THRESHOLD_PTS=50, EMA50_THRESHOLD_PTS=50
     4. TP not viable? (need 100pts to BB opposite band)
        May be too tight in low-volatility periods — try reducing to 70pts
""")
        return

    print(f"\n  Pipeline pass rate: {signals_found} setups → {signals_qualifying} passed confidence "
          f"({signals_qualifying/signals_found*100:.0f}%) → {total} traded")

    if signals_qualifying == 0:
        print("""
  ❌ CRITICAL: Setups found but NONE pass confidence gate.
     Confidence needs ≥70% (LONG) / ≥75% (SHORT) = 4-5/8 criteria.
     Most likely failing criteria:
     1. daily_trend (C1)   — is price consistently above/below daily EMA200?
     2. entry_level (C2)   — tighten entry: only trade when VERY close to BB/EMA50
     3. tp_viable (C4)     — 100pts to opposite band may be too far in ranging market
     4. macro/4H RSI (C6)  — 4H RSI range 35-75, may still be outside in choppy market
     Suggested: Lower confidence thresholds to 60% (LONG) / 65% (SHORT) for testing
""")
        return

    if signals_qualifying > 0 and total == 0:
        print("  ❌ Signals qualified but no trades simulated — check backtest logic.")
        return

    # Win rate assessment
    breakeven_wr = 1 / (1 + TP_DISTANCE / SL_DISTANCE)  # 33.3% with 1:2 RR
    print(f"\n  Win rate: {win_rate*100:.1f}% (breakeven = {breakeven_wr*100:.0f}% with {SL_DISTANCE}SL/{TP_DISTANCE}TP)")

    if win_rate < breakeven_wr:
        deficit = breakeven_wr - win_rate
        print(f"  ⚠️  Strategy is BELOW breakeven by {deficit*100:.1f} percentage points.")
        print("""
     Suggested fixes (try one at a time, re-backtest each):

     A. Tighten entry conditions — only take the HIGHEST confidence signals:
        • Raise MIN_CONFIDENCE from 70% to 80% (will reduce trades but improve quality)
        • Add a 4th require: only trade if BOTH BB_mid AND EMA50 are aligned

     B. Adjust SL/TP ratio to be more forgiving:
        • SL 150pts instead of 200 (smaller initial risk)
        • TP 300pts instead of 400 (easier to hit, lower RR but higher win rate)
        • This requires ~38% win rate to break even — still achievable

     C. Add a trend strength filter:
        • Only LONG when daily RSI is above 50 (trending up)
        • Only SHORT when daily RSI is below 50 (trending down)

     D. Remove SHORT trades if SHORT win rate is dragging overall:
        • Run LONG-only and check — Nikkei 225 has had an upward bias historically
""")
    elif win_rate < 0.50:
        print(f"  ✅ Strategy is PROFITABLE (>{breakeven_wr*100:.0f}% required), win rate {win_rate*100:.0f}%.")
        print("""
     Suggested optimisations to push further:

     A. Raise confidence threshold to 80%+ → filters weakest setups, likely improves win rate
     B. Consider skipping SHORT trades during strong uptrends (Daily RSI > 60)
     C. AI filter (Sonnet → Opus) will reject additional weak signals → expect real-world results better than this simulation
""")
    else:
        print(f"  🟢 Strong win rate {win_rate*100:.0f}%. Strategy is working well.")
        print("""
     You're already profitable. Consider:
     A. Increasing contract size when confidence ≥90%
     B. Adding a second entry on retests (pyramiding with half size)
""")

    # Direction-specific advice
    for d in ["LONG", "SHORT"]:
        dt = [t for t in trades if t.direction == d]
        if len(dt) < 3:
            continue
        dw = [t for t in dt if t.pnl_usd > 0]
        dwr = len(dw) / len(dt)
        if dwr < breakeven_wr:
            print(f"  ⚠️  {d} trades losing (win rate {dwr*100:.0f}%) — consider disabling {d} direction")

    # Session-specific advice
    for sess in ["Tokyo", "London", "New York"]:
        st = [t for t in trades if t.session == sess]
        if len(st) < 3:
            continue
        sw = [t for t in st if t.pnl_usd > 0]
        swr = len(sw) / len(st)
        spnl = sum(t.pnl_usd for t in st)
        if spnl < 0:
            print(f"  ⚠️  {sess} session is net NEGATIVE (${spnl:+.2f}, {swr*100:.0f}% win) — consider blacklisting this session")

    # Confidence tier advice
    best_tier = None
    best_wr = 0
    for lo, hi in [(70, 80), (80, 90), (90, 101)]:
        ct = [t for t in trades if lo <= t.confidence_score < hi]
        if len(ct) < 2:
            continue
        cwr = len([t for t in ct if t.pnl_usd > 0]) / len(ct)
        if cwr > best_wr:
            best_wr = cwr
            best_tier = (lo, hi)
    if best_tier:
        print(f"\n  ✨ Best confidence tier: {best_tier[0]}-{best_tier[1]-1}% with {best_wr*100:.0f}% win rate")
        if best_tier[0] > 70:
            print(f"     → Raise MIN_CONFIDENCE to {best_tier[0]}% for better signal quality")

    print(f"""
  PIPELINE FUNNEL SUMMARY:
  Every signal costs ~$0.015 AI API. At {signals_qualifying} signals/60 days ≈ {signals_qualifying*6} signals/year.
  Annual AI cost estimate: ~${signals_qualifying*6*0.015:.2f}

  NEXT STEPS:
  1. If win rate < 33%: the STRATEGY LOGIC needs fixing before live trading.
  2. If win rate 33-50%: profitable but thin — raise confidence threshold first.
  3. If win rate > 50%: strategy is sound. Go live with small size, monitor 2 weeks.
""")


# ── WFO Parameter Grid Search ───────────────────────────────────────────────────

def run_wfo_grid(df_15m: pd.DataFrame, df_4h: pd.DataFrame, df_1d: pd.DataFrame):
    """
    Walk-Forward Optimization: grid search over SL/TP parameters.
    Split: IS = first 45 days, OOS = last 15 days.
    Reports best IS parameter set and its OOS performance.
    """
    global SL_DISTANCE, TP_DISTANCE, BREAKEVEN_TRIGGER, TRAILING_DISTANCE

    # IS/OOS split by candle index
    total = len(df_15m)
    split = int(total * 0.75)  # 75% in-sample, 25% out-of-sample
    df_is  = df_15m.iloc[:split].copy()
    df_oos = df_15m.iloc[split:].copy()

    param_grid = [
        (sl, tp) for sl in [150, 200, 250, 300]
                 for tp in [300, 400, 500, 600]
                 if tp / sl >= 1.5  # enforce minimum 1.5 RR
    ]

    print("\n" + "═"*70)
    print("  WALK-FORWARD OPTIMIZATION")
    print(f"  IS: {df_is.index[0].date()} → {df_is.index[-1].date()} ({split} candles)")
    print(f"  OOS: {df_oos.index[0].date()} → {df_oos.index[-1].date()} ({total-split} candles)")
    print("═"*70)

    is_results = []
    print(f"\n  {'SL':>4}  {'TP':>4}  {'RR':>4}  {'Trades':>6}  {'WinRate':>8}  {'PF':>5}  {'P&L':>8}")
    print("  " + "-"*55)

    for sl, tp in param_grid:
        SL_DISTANCE = sl
        TP_DISTANCE = tp
        BREAKEVEN_TRIGGER = min(sl - 50, 100)   # BE trigger at ~SL-50, min 100pts
        TRAILING_DISTANCE = max(sl - 50, 100)   # trailing = SL-50

        trades_is, _, _ = run_backtest(df_is, df_4h, df_1d)
        if not trades_is:
            print(f"  {sl:>4}  {tp:>4}  {tp/sl:.1f}     0      —       —          —")
            continue

        wins = [t for t in trades_is if t.pnl_usd > 0]
        wr = len(wins) / len(trades_is)
        gp = sum(t.pnl_usd for t in wins) if wins else 0
        gl = abs(sum(t.pnl_usd for t in trades_is if t.pnl_usd <= 0)) or 0.01
        pf = gp / gl
        pnl = sum(t.pnl_usd for t in trades_is)
        is_results.append((pf, wr, pnl, sl, tp, trades_is))
        print(f"  {sl:>4}  {tp:>4}  {tp/sl:.1f}  {len(trades_is):>6}  {wr*100:>7.1f}%  {pf:>5.2f}  ${pnl:>+7.2f}")

    if not is_results:
        print("  No results generated — no trades in any IS window.")
        return

    # Best IS by profit factor (with minimum 5 trades)
    qualified = [(pf, wr, pnl, sl, tp, ts) for pf, wr, pnl, sl, tp, ts in is_results if len(ts) >= 5]
    if not qualified:
        qualified = is_results
    best_pf, best_wr, best_pnl, best_sl, best_tp, _ = max(qualified, key=lambda x: x[0])

    print(f"\n  Best IS params: SL={best_sl} TP={best_tp} (RR={best_tp/best_sl:.1f})")
    print(f"  IS performance: {best_wr*100:.1f}% win, PF={best_pf:.2f}, P&L=${best_pnl:+.2f}")

    # OOS validation
    SL_DISTANCE = best_sl
    TP_DISTANCE = best_tp
    BREAKEVEN_TRIGGER = min(best_sl - 50, 100)
    TRAILING_DISTANCE = max(best_sl - 50, 100)

    trades_oos, _, _ = run_backtest(df_oos, df_4h, df_1d)
    if trades_oos:
        wins_oos = [t for t in trades_oos if t.pnl_usd > 0]
        wr_oos = len(wins_oos) / len(trades_oos)
        gp_oos = sum(t.pnl_usd for t in wins_oos) if wins_oos else 0
        gl_oos = abs(sum(t.pnl_usd for t in trades_oos if t.pnl_usd <= 0)) or 0.01
        pf_oos = gp_oos / gl_oos
        pnl_oos = sum(t.pnl_usd for t in trades_oos)
        print(f"\n  OOS validation:  {len(trades_oos)} trades | {wr_oos*100:.1f}% win | PF={pf_oos:.2f} | P&L=${pnl_oos:+.2f}")

        degrad = (best_pf - pf_oos) / best_pf * 100
        print(f"  PF degradation IS→OOS: {degrad:+.1f}%  (< 40% degradation = acceptable)")
        if wr_oos > (1 / (1 + best_tp / best_sl)):
            print(f"  ✅ OOS win rate {wr_oos*100:.1f}% exceeds breakeven {100/(1+best_tp/best_sl):.1f}% — strategy generalises")
        else:
            print(f"  ⚠️  OOS win rate {wr_oos*100:.1f}% below breakeven — overfitting risk")
    else:
        print("  OOS: 0 trades generated.")


# ── Entry point ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from core.confidence import BB_MID_THRESHOLD_PTS, EMA50_THRESHOLD_PTS
    print("Japan 225 Bot — Backtest + WFO")
    print(f"Live code thresholds: BB_MID={BB_MID_THRESHOLD_PTS}pts, EMA50={EMA50_THRESHOLD_PTS}pts")
    print(f"  (calibrated 2026-02-28 from 30pts to 150pts for Nikkei ~50k-60k)\n")

    # Download
    df_15m, df_1h, df_1d = download_data()
    df_4h = resample_4h(df_1h)
    print(f"  4H candles:    {len(df_4h)} (resampled from 1H)")

    # Run baseline backtest with default params
    print(f"\nRunning baseline backtest on {len(df_15m)} 15min candles...")
    trades, signals_found, signals_qualifying = run_backtest(df_15m, df_4h, df_1d)
    print_report(trades, signals_found, signals_qualifying, df_15m)

    # WFO grid search
    if len(df_15m) >= 200:
        run_wfo_grid(df_15m, df_4h, df_1d)
