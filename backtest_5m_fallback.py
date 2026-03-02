#!/usr/bin/env python3
"""
Walk-Forward Backtest: 5M Fallback Strategy — yfinance ^N225 data.

Compares:
  A) 15M-only setup detection (baseline)
  B) 15M + 5M fallback (new strategy)

Uses same detect_setup() and alignment logic as the live bot.
No IG API calls. No allowance consumed.
"""
import sys
import json
import logging
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import (
    DEFAULT_SL_DISTANCE, DEFAULT_TP_DISTANCE, SPREAD_ESTIMATE,
    SESSION_HOURS_UTC,
)
from core.indicators import analyze_timeframe, detect_setup
from monitor import TradingMonitor

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
logger = logging.getLogger("backtest")
logger.setLevel(logging.INFO)

SL = DEFAULT_SL_DISTANCE   # 150
TP = DEFAULT_TP_DISTANCE    # 400
SPREAD = SPREAD_ESTIMATE    # 7
COOLDOWN_BARS = 9           # 9 × 5min = 45min between trades


# ─── Data fetch ───────────────────────────────────────────────────────────────

def fetch_data() -> dict:
    """Fetch all timeframes from yfinance. Zero IG impact."""
    t = yf.Ticker("NKD=F")

    logger.info("Fetching NKD=F (Nikkei futures, 23h/day) from yfinance...")
    df5 = t.history(period="60d", interval="5m")
    df15 = t.history(period="60d", interval="15m")
    dfd = t.history(period="1y", interval="1d")

    def to_candles(df):
        out = []
        for idx, row in df.iterrows():
            out.append({
                "timestamp": str(idx),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row.get("Volume", 0)),
            })
        return out

    c5 = to_candles(df5)
    c15 = to_candles(df15)
    cd = to_candles(dfd)
    logger.info(f"  5M: {len(c5)} | 15M: {len(c15)} | Daily: {len(cd)}")
    return {"5m": c5, "15m": c15, "daily": cd}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_ts(ts_str: str) -> Optional[datetime]:
    if not ts_str:
        return None
    s = str(ts_str).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S+09:00",
                "%Y-%m-%d %H:%M:%S-05:00", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s[:25], fmt)
            # Convert to UTC if timezone-aware (^N225 is JST = UTC+9)
            if dt.tzinfo:
                from datetime import timezone
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    try:
        from dateutil import parser as dp
        dt = dp.parse(s)
        if dt.tzinfo:
            from datetime import timezone
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def get_session(dt: datetime) -> Optional[str]:
    h = dt.hour
    for name, (start, end) in SESSION_HOURS_UTC.items():
        if start <= h < end:
            return name
    return None


@dataclass
class Trade:
    entry_time: datetime
    direction: str
    setup_type: str
    entry_price: float
    sl: float
    tp: float
    session: str
    timeframe: str  # "15m" or "5m"
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    result: Optional[str] = None  # WIN, LOSS, TIMEOUT
    max_favorable: float = 0.0
    max_adverse: float = 0.0
    bars_held: int = 0
    r_multiple: float = 0.0


# ─── Walk-forward engine ─────────────────────────────────────────────────────

def _bisect_right(timestamps, target):
    lo, hi = 0, len(timestamps)
    while lo < hi:
        mid = (lo + hi) // 2
        if timestamps[mid] is None or timestamps[mid] <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def run_backtest(c5m, c15m, cdaily, mode="combined"):
    """Walk forward through 5M candles, detect setups, simulate trades."""
    trades = []
    cooldown_until = 0  # index into c5m

    ts5 = [parse_ts(c.get("timestamp", "")) for c in c5m]
    ts15 = [parse_ts(c.get("timestamp", "")) for c in c15m]
    tsd = [parse_ts(c.get("timestamp", "")) for c in cdaily]

    MIN_5M, MIN_15M, MIN_DAILY = 100, 220, 200
    scan_count = setup_count = 0

    for i in range(MIN_5M, len(c5m)):
        ts = ts5[i]
        if ts is None or ts.weekday() >= 5:
            continue
        session = get_session(ts)
        if session is None:
            continue
        if i < cooldown_until:
            continue

        # Find lookback windows
        i15 = _bisect_right(ts15, ts)
        if i15 < MIN_15M:
            continue
        iD = _bisect_right(tsd, ts)
        if iD < MIN_DAILY:
            continue

        w15 = c15m[max(0, i15 - MIN_15M):i15]
        wd = cdaily[max(0, iD - MIN_DAILY):iD]
        w5 = c5m[max(0, i - MIN_5M):i + 1]

        tf_15m = analyze_timeframe(w15)
        tf_daily = analyze_timeframe(wd)
        tf_5m = analyze_timeframe(w5)

        scan_count += 1

        # --- Strategy A: 15M only ---
        setup = detect_setup(tf_daily=tf_daily, tf_4h={}, tf_15m=tf_15m)
        entry_tf = "15m"

        # --- Strategy B: 5M fallback ---
        if mode == "combined" and not setup["found"] and tf_5m:
            setup_5m = detect_setup(tf_daily=tf_daily, tf_4h={}, tf_15m=tf_5m)
            if setup_5m["found"] and TradingMonitor._5m_aligns_with_15m(setup_5m, tf_15m):
                setup = setup_5m
                setup["type"] += "_5m"
                entry_tf = "5m"

        if not setup["found"]:
            continue

        setup_count += 1
        trade = Trade(
            entry_time=ts, direction=setup["direction"],
            setup_type=setup["type"], entry_price=setup["entry"],
            sl=setup["sl"], tp=setup["tp"],
            session=session, timeframe=entry_tf,
        )
        trade = _simulate(trade, c5m, i)

        if trade.result:
            trades.append(trade)
            cooldown_until = i + max(trade.bars_held, COOLDOWN_BARS)

    logger.info(f"  [{mode}] scans={scan_count} raw_setups={setup_count} trades={len(trades)}")
    return trades


def _simulate(trade: Trade, candles: list, entry_idx: int) -> Trade:
    """Walk forward bar-by-bar, check SL/TP hit."""
    max_bars = 96  # 96 × 5min = 8 hours max hold

    if trade.direction == "LONG":
        eff = trade.entry_price + SPREAD / 2
        sl_level = eff - SL
        tp_level = eff + TP
    else:
        eff = trade.entry_price - SPREAD / 2
        sl_level = eff + SL
        tp_level = eff - TP

    for j in range(entry_idx + 1, min(entry_idx + max_bars + 1, len(candles))):
        c = candles[j]
        h, l = c["high"], c["low"]
        trade.bars_held = j - entry_idx

        if trade.direction == "LONG":
            trade.max_favorable = max(trade.max_favorable, h - eff)
            trade.max_adverse = max(trade.max_adverse, eff - l)
            # SL wins on same-bar conflict
            if l <= sl_level:
                trade.pnl = -SL - SPREAD
                trade.result = "LOSS"
                trade.r_multiple = -1.0
                break
            if h >= tp_level:
                trade.pnl = TP - SPREAD
                trade.result = "WIN"
                trade.r_multiple = TP / SL
                break
        else:
            trade.max_favorable = max(trade.max_favorable, eff - l)
            trade.max_adverse = max(trade.max_adverse, h - eff)
            if h >= sl_level:
                trade.pnl = -SL - SPREAD
                trade.result = "LOSS"
                trade.r_multiple = -1.0
                break
            if l <= tp_level:
                trade.pnl = TP - SPREAD
                trade.result = "WIN"
                trade.r_multiple = TP / SL
                break

        exit_ts = parse_ts(c.get("timestamp", ""))
        if exit_ts:
            trade.exit_time = exit_ts
    else:
        # Timeout — close at last bar's close
        if trade.bars_held > 0:
            last = candles[min(entry_idx + max_bars, len(candles) - 1)]
            if trade.direction == "LONG":
                trade.pnl = last["close"] - eff - SPREAD
            else:
                trade.pnl = eff - last["close"] - SPREAD
            trade.result = "TIMEOUT"
            trade.r_multiple = trade.pnl / SL if SL else 0

    return trade


# ─── Analytics ────────────────────────────────────────────────────────────────

def analyze(trades: list, label: str) -> dict:
    if not trades:
        return {"label": label, "count": 0}

    n = len(trades)
    wins = [t for t in trades if t.result == "WIN"]
    losses = [t for t in trades if t.result == "LOSS"]
    timeouts = [t for t in trades if t.result == "TIMEOUT"]

    wr = len(wins) / n * 100
    avg_w = sum(t.pnl for t in wins) / len(wins) if wins else 0
    avg_l = abs(sum(t.pnl for t in losses) / len(losses)) if losses else 1
    rr = avg_w / avg_l if avg_l else 0

    gross_p = sum(t.pnl for t in trades if t.pnl and t.pnl > 0)
    gross_l = abs(sum(t.pnl for t in trades if t.pnl and t.pnl < 0))
    pf = gross_p / gross_l if gross_l else float("inf")

    risk = SL + SPREAD
    exp_pts = (wr / 100 * avg_w) - ((100 - wr) / 100 * avg_l)
    exp_r = exp_pts / risk if risk else 0
    total_pnl = sum(t.pnl for t in trades if t.pnl is not None)

    kelly = ((wr / 100) * avg_w - ((100 - wr) / 100) * avg_l) / avg_w if avg_w > 0 else 0

    # Max drawdown
    cum, running = [], 0
    for t in sorted(trades, key=lambda x: x.entry_time):
        running += (t.pnl or 0)
        cum.append(running)
    peak = max_dd = 0
    for p in cum:
        peak = max(peak, p)
        max_dd = max(max_dd, peak - p)

    holds = [t.bars_held * 5 for t in trades]
    avg_hold = sum(holds) / len(holds)
    avg_mfe = sum(t.max_favorable for t in trades) / n
    avg_mae = sum(t.max_adverse for t in trades) / n

    def _group(key_fn):
        g = defaultdict(list)
        for t in trades:
            g[key_fn(t)].append(t)
        out = {}
        for k, st in sorted(g.items(), key=lambda x: -len(x[1])):
            sw = [t for t in st if t.result == "WIN"]
            sl_ = [t for t in st if t.result == "LOSS"]
            aw = sum(t.pnl for t in sw) / len(sw) if sw else 0
            al = abs(sum(t.pnl for t in sl_) / len(sl_)) if sl_ else 1
            sp = sum(t.pnl for t in st if t.pnl)
            gp = sum(t.pnl for t in st if t.pnl and t.pnl > 0)
            gl = abs(sum(t.pnl for t in st if t.pnl and t.pnl < 0))
            out[k] = {"n": len(st), "wr": len(sw) / len(st) * 100, "pnl": sp,
                       "rr": aw / al if al else 0, "pf": gp / gl if gl else 0}
        return out

    return {
        "label": label, "count": n,
        "wins": len(wins), "losses": len(losses), "timeouts": len(timeouts),
        "wr": round(wr, 1), "avg_w": round(avg_w, 1), "avg_l": round(avg_l, 1),
        "rr": round(rr, 2), "pf": round(pf, 2),
        "exp_pts": round(exp_pts, 1), "exp_r": round(exp_r, 3),
        "kelly": round(kelly * 100, 1),
        "total_pnl": round(total_pnl, 1), "max_dd": round(max_dd, 1),
        "avg_hold": round(avg_hold, 0), "max_hold": round(max(holds), 0) if holds else 0,
        "avg_mfe": round(avg_mfe, 1), "avg_mae": round(avg_mae, 1),
        "by_type": _group(lambda t: t.setup_type),
        "by_session": _group(lambda t: t.session),
        "by_tf": _group(lambda t: t.timeframe),
        "by_dir": _group(lambda t: t.direction),
    }


# ─── Report ───────────────────────────────────────────────────────────────────

def _print_stats(s: dict):
    be = SL / (SL + TP) * 100
    if s["count"] == 0:
        print(f"  {s['label']}: NO TRADES\n")
        return

    ev = "+" if s["exp_r"] > 0 else ""
    print(f"\n{'─' * 80}")
    print(f"  {s['label']}")
    print(f"{'─' * 80}")
    print(f"  Trades:          {s['count']}  (W:{s['wins']}  L:{s['losses']}  T:{s['timeouts']})")
    print(f"  Win Rate:        {s['wr']:.1f}%  (break-even = {be:.1f}%)")
    print(f"  Avg Win:         +{s['avg_w']:.0f}pts  |  Avg Loss: -{s['avg_l']:.0f}pts")
    print(f"  Realized R:R:    {s['rr']:.2f}:1")
    print(f"  Profit Factor:   {s['pf']:.2f}{'  ✓ PROFITABLE' if s['pf'] > 1 else '  ✗ LOSING'}")
    print(f"  Expectancy:      {ev}{s['exp_pts']:.1f}pts/trade ({ev}{s['exp_r']:.3f}R)")
    print(f"  Kelly Criterion: {s['kelly']:.1f}% of capital per trade")
    print(f"  Total P&L:       {s['total_pnl']:+.0f}pts (${s['total_pnl']:+.0f} at $1/pt)")
    print(f"  Max Drawdown:    {s['max_dd']:.0f}pts")
    print(f"  Avg Hold:        {s['avg_hold']:.0f}min  |  Max: {s['max_hold']:.0f}min")
    print(f"  Avg MFE:         {s['avg_mfe']:.0f}pts  |  Avg MAE: {s['avg_mae']:.0f}pts")

    print(f"\n  By Setup Type:")
    print(f"    {'Type':<35s} {'n':>4s} {'WR':>6s} {'R:R':>6s} {'PF':>6s} {'P&L':>8s}")
    for k, v in s["by_type"].items():
        print(f"    {k:<35s} {v['n']:>4d} {v['wr']:>5.1f}% {v['rr']:>5.2f} {v['pf']:>5.2f} {v['pnl']:>+7.0f}")

    print(f"\n  By Session:")
    for k, v in s["by_session"].items():
        print(f"    {k:<12s}  n={v['n']:>3d}  WR={v['wr']:>5.1f}%  PF={v['pf']:>5.2f}  P&L={v['pnl']:>+7.0f}")

    if len(s.get("by_dir", {})) > 1:
        print(f"\n  By Direction:")
        for k, v in s["by_dir"].items():
            print(f"    {k:<6s}  n={v['n']:>3d}  WR={v['wr']:>5.1f}%  R:R={v['rr']:.2f}  P&L={v['pnl']:>+7.0f}")

    if len(s.get("by_tf", {})) > 1:
        print(f"\n  By Entry TF:")
        for k, v in s["by_tf"].items():
            print(f"    {k:<5s}  n={v['n']:>3d}  WR={v['wr']:>5.1f}%  PF={v['pf']:>5.2f}  P&L={v['pnl']:>+7.0f}")


def print_report(sa, sb, data_info):
    be = SL / (SL + TP) * 100
    print("\n" + "=" * 80)
    print("  WALK-FORWARD BACKTEST: 5M Fallback Strategy")
    print(f"  Data: yfinance NKD=F (Nikkei futures, 23h/day) | SL={SL} TP={TP} | R:R={TP/SL:.2f}:1 | BE WR={be:.1f}%")
    print(f"  Candles: {data_info['5m']} 5M, {data_info['15m']} 15M, {data_info['daily']} Daily")
    if data_info.get("range"):
        print(f"  Period: {data_info['range']}")
    print("=" * 80)

    _print_stats(sa)
    _print_stats(sb)

    if sa["count"] > 0 and sb["count"] > 0:
        dt = sb["count"] - sa["count"]
        dp = sb["total_pnl"] - sa["total_pnl"]

        print(f"\n{'=' * 80}")
        print("  HEAD-TO-HEAD: 15M-Only vs 15M+5M Fallback")
        print(f"{'=' * 80}")
        print(f"  Extra trades from 5M:   +{dt}")
        print(f"  P&L delta:              {dp:+.0f}pts")
        print(f"  PF:                     {sa['pf']:.2f} → {sb['pf']:.2f}")
        print(f"  Expectancy:             {sa['exp_r']:+.3f}R → {sb['exp_r']:+.3f}R")

        if "5m" in sb.get("by_tf", {}):
            t5 = sb["by_tf"]["5m"]
            e5r = (t5["pnl"] / t5["n"]) / (SL + SPREAD) if t5["n"] else 0
            print(f"\n  5M-ONLY TRADES (incremental value):")
            print(f"    n={t5['n']}  WR={t5['wr']:.1f}%  R:R={t5['rr']:.2f}  "
                  f"PF={t5['pf']:.2f}  P&L={t5['pnl']:+.0f}pts  Exp={e5r:+.3f}R")
            for k, v in sb["by_type"].items():
                if "_5m" in k:
                    print(f"      {k:<33s}  n={v['n']:>3d}  WR={v['wr']:>5.1f}%  "
                          f"RR={v['rr']:.2f}  PF={v['pf']:.2f}")

    print(f"\n{'=' * 80}")
    print("  VERDICT")
    print(f"{'=' * 80}")

    if sb["count"] == 0:
        print("  No trades generated.\n")
        return

    if "5m" in sb.get("by_tf", {}):
        t5 = sb["by_tf"]["5m"]
        if t5["n"] > 0:
            e5r = (t5["pnl"] / t5["n"]) / (SL + SPREAD)
            if e5r > 0:
                print(f"  5M fallback: +EV at {e5r:+.3f}R/trade (n={t5['n']}) → KEEP ENABLED")
            elif e5r > -0.15:
                print(f"  5M fallback: MARGINAL at {e5r:+.3f}R (n={t5['n']}) → keep, AI filter helps")
            else:
                print(f"  5M fallback: -EV at {e5r:+.3f}R (n={t5['n']}) → consider tightening/disabling")

    print(f"\n  NOTE: Raw signals without AI filter. Live WR typically 10-20pp higher.")
    print(f"  With R:R {TP/SL:.1f}:1, break-even WR = {be:.1f}%. Low WR + high RR = correct approach.")
    print()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    data = fetch_data()

    for k in ["5m", "15m", "daily"]:
        if len(data[k]) < 100:
            logger.error(f"Insufficient {k} data: {len(data[k])}")
            sys.exit(1)

    first = parse_ts(data["5m"][0].get("timestamp", ""))
    last = parse_ts(data["5m"][-1].get("timestamp", ""))
    date_range = f"{first.strftime('%Y-%m-%d')} to {last.strftime('%Y-%m-%d')} ({(last-first).days}d)" if first and last else "?"

    data_info = {k: len(v) for k, v in data.items()}
    data_info["range"] = date_range

    logger.info("Running Strategy A (15M only)...")
    trades_a = run_backtest(data["5m"], data["15m"], data["daily"], mode="15m_only")

    logger.info("Running Strategy B (15M + 5M fallback)...")
    trades_b = run_backtest(data["5m"], data["15m"], data["daily"], mode="combined")

    sa = analyze(trades_a, "STRATEGY A: 15M Only (Baseline)")
    sb = analyze(trades_b, "STRATEGY B: 15M + 5M Fallback")

    print_report(sa, sb, data_info)

    # Save results
    out = Path("storage/data/backtest_5m_results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "ts": datetime.now().isoformat(),
        "data_source": "yfinance NKD=F (Nikkei 225 Futures)",
        "data": data_info,
        "strategy_a": sa,
        "strategy_b": sb,
        "trades": [
            {"t": str(t.entry_time), "d": t.direction, "type": t.setup_type,
             "entry": t.entry_price, "pnl": t.pnl, "res": t.result,
             "sess": t.session, "tf": t.timeframe, "bars": t.bars_held,
             "r": round(t.r_multiple, 2),
             "mfe": round(t.max_favorable, 1), "mae": round(t.max_adverse, 1)}
            for t in trades_b
        ],
    }, indent=2, default=str))
    logger.info(f"Results saved to {out}")


if __name__ == "__main__":
    main()
