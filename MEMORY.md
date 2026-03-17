# Japan 225 Bot — Project Memory
**Read this at the start of EVERY conversation before touching any code.**
Digests live in `.claude/digests/`. Read only the digest(s) relevant to your task — never scan raw files.
Historical session notes → `.claude/session-history.md`

---

## Architecture (single VM process)
```
Oracle VM: monitor.py (24/7, systemd: japan225-bot)
  SCANNING (no position): every 5min active sessions, 30min off-hours
    → fetch 15M+5M parallel, then Daily sequential (cached, delta fetches after 1st)
    → BIDIRECTIONAL detect_setup(): LONG + SHORT checked independently (exclude_direction)
    → 5M fallback: per-direction (if 15M no setup → try 5M with alignment guard)
    → 5M setups tagged with _5m suffix. entry_timeframe passed to AI prompts.
    → NO cooldown ($0/call subscription) → compute_confidence() for BOTH directions
    → Primary = highest confidence. Secondary context passed to AI.
    → if score >= 60%: Sonnet 4.6 scan (with Opus sub-agent for borderline 72-86%)
    → Sequential: Sonnet runs first → if rejected, Opus evaluates OPPOSITE direction as swing trade
    → Gate: opposite direction must have had a detected setup + local conf >= 60% + Sonnet conf >= 30%
    → evaluate_opposite(): Opus gets FULL context (same as Sonnet), full SL/TP freedom, same thresholds
    → Single subprocess: Sonnet analyzes, delegates to Opus sub-agent internally when needed
    → if AI confirms & risk passes: Telegram CONFIRM/REJECT buttons (NEVER auto-execute)
  MONITORING (position open): every 2s
    → Lightstreamer streaming price ticks (BID/OFR mid) — real-time, ~0 REST calls for price
    → REST fallback if streaming stale >10s. Background reconnect after 30 stale cycles (60s).
    → Position existence REST check every N cycles unchanged (SAFETY_CONSECUTIVE_EMPTY=2)
    → MomentumTracker.add_price() → SEVERE adverse tier → Telegram alert only (no auto-SL moves)
    → MILD/MODERATE adverse alerts REMOVED — replaced by Opus position evaluator
    → Opus position eval every 60 cycles (120s): evaluate_open_position() → send_position_eval()
       CLOSE_NOW + conf >= 70% → Telegram alert, user must /close (never auto-close). TIGHTEN_SL → Telegram alert only.
    → SL and TP fixed at entry (set by AI). No mechanical modifications after open.
  TELEGRAM: always-on polling, callbacks: on_trade_confirm, on_force_scan

Dashboard: FastAPI (systemd: japan225-dashboard, port 8080)
           Tunnel: ngrok static domain (systemd: japan225-ngrok)
           Frontend: GitHub Pages (docs/index.html)
           SSE: /api/stream pushes state_update + new_logs (replaces setInterval polling)
```
GitHub Actions: CI tests ONLY (tests.yml). scan.yml outdated/unused.

---

## File Map
| File | Purpose |
|------|---------|
| `monitor.py` | Main process. TradingMonitor class. _scanning_cycle(), _monitoring_cycle(), _write_state() |
| `config/settings.py` | ALL constants. Never scatter config. |
| `core/ig_client.py` | IG REST API + Lightstreamer streaming. connect/ensure_connected, get_prices, open/modify/close_position |
| `core/indicators.py` | analyze_timeframe(), detect_setup() (LONG+SHORT bidirectional), ema/rsi/bb/vwap/heiken_ashi, FVG, Fibonacci, PDH/PDL, liquidity sweep, pivot_points, detect_candlestick_patterns (12 patterns), analyze_body_trend |
| `core/session.py` | get_current_session() UTC, is_no_trade_day(), is_weekend(), is_friday_blackout() |
| `core/momentum.py` | MomentumTracker class. add_price(), should_alert(), milestone_alert() |
| `core/confidence.py` | compute_confidence(...) → score dict. 9-criteria WEIGHTED scoring + R:R estimate penalty (2026-03-10). C7/C8 pre-gates. R:R penalty: rr<0.8→×0.70, rr<1.0→×0.80, rr<1.2→×0.90. |
| `ai/analyzer.py` | AIAnalyzer. scan_with_sonnet() (Opus sub-agent). evaluate_opposite(). post_trade_analysis(). **CLI subprocess (OAuth/subscription).** |
| `ai/context_writer.py` | write_context() — writes storage/context/*.md. market_snapshot, recent_activity, macro, live_edge. |
| `trading/risk_manager.py` | RiskManager.validate_trade() 12 checks (incl. portfolio_risk). get_safe_lot_size(balance,price,sl_distance,confidence,peak_balance). get_dynamic_sl(atr,setup_type). |
| `trading/exit_manager.py` | ExitManager. SL/TP fixed at entry. Only close_early (Opus evaluator). No BE/trailing. |
| `notifications/telegram_bot.py` | TelegramBot. send_trade_alert(), /menu inline buttons, /close /kill /pause /resume |
| `storage/database.py` | Storage class. SQLite WAL mode. open_trade_atomic(), AI cooldown, get_ai_context_block() |
| `storage/scan_analyzer.py` | Cron-based scan analyzer. SL/TP-aware classification. Writes `storage/data/scan_analysis.md`. |
| `storage/probability_tracker.py` | Conditional probability tracker. Wilson CI + Kelly. Writes `probability_tracker.md`. |
| `dashboard/main.py` + `routers/` | FastAPI app, CORS, Bearer auth. Routes: status/config/history/logs/chat/controls/stream |
| `dashboard/services/claude_client.py` | 3-tier chat: Haiku/Sonnet/Opus. Context injection. |
| `dashboard/services/ig_history.py` | IG journal. Timestamp fallback match for DB merge. 1min cache TTL. |

---

## Key Constants (settings.py)
```
EPIC = "IX.D.NIKKEI.IFM.IP"   CONTRACT_SIZE = 1 ($1/pt)   MARGIN_FACTOR = 0.005 (0.5%)
MIN_CONFIDENCE = 70            MIN_CONFIDENCE_SHORT = 75    HAIKU_MIN_SCORE = 60
EXTREME_DAY_RANGE_PTS = 1000   EXTREME_DAY_MIN_CONFIDENCE = 85
OVERSOLD_SHORT_BLOCK_RSI_4H = 32  OVERBOUGHT_LONG_BLOCK_RSI_4H = 68
DEFAULT_SL_DISTANCE = 150 (fallback only)  DEFAULT_TP_DISTANCE = 400 (fallback only)  MIN_RR_RATIO = 1.2
EXIT: SL/TP fixed at entry. No breakeven, no trailing. AI sets dynamic TP at structural levels.
SCAN_INTERVAL_SECONDS = 300    MONITOR_INTERVAL_SECONDS = 2  OFFHOURS_INTERVAL_SECONDS = 1800
POSITION_CHECK_EVERY_N_CYCLES = 5   ADVERSE_LOOKBACK_READINGS = 150   STREAMING_STALE_SECONDS = 10
AI_COOLDOWN_MINUTES = 15       PRICE_DRIFT_ABORT_PTS = 20    SAFETY_CONSECUTIVE_EMPTY = 2
SONNET_MODEL = "claude-sonnet-4-6"   OPUS_MODEL = "claude-opus-4-6"
DISPLAY_TZ = UTC+3 (Kuwait). display_now() helper. All user-facing timestamps in Kuwait time.
MOMENTUM_RSI_HIGH = 75   RSI_ENTRY_HIGH_BOUNCE = 55   ENABLE_EMA50_BOUNCE_SETUP = False

# RISK-BASED SIZING (2026-03-10 — micro account friendly)
MAX_MARGIN_PERCENT = 0.10 (10% per position)   MAX_OPEN_POSITIONS = 3   MAX_PORTFOLIO_RISK_PERCENT = 0.15
RISK_PERCENT = 5.0  MAX_RISK_PERCENT = 8.0
DRAWDOWN_REDUCE_10PCT=0.5  DRAWDOWN_REDUCE_15PCT=0.25  DRAWDOWN_STOP_20PCT=True
SL_ATR_MULTIPLIER: MOMENTUM=1.2 MEAN_REVERSION=1.8 BREAKOUT=1.5 VWAP=1.3 DEFAULT=1.5
SL_FLOOR_PTS=60   TP_ATR_MULTIPLIER_BASE=2.5  TP_ATR_MULTIPLIER_MOMENTUM=3.0  TP_FLOOR_PTS=150
```
Dashboard chat: 3-tier auto-select. Haiku (status, ≤60s) | Sonnet (analysis, ≤180s) | Opus (code fixes, ≤600s).

---

## Known Bugs
- monitor.py: naive vs UTC-aware datetime mismatch in duration calculation (MEDIUM)
- dashboard chat: non-atomic _write_history() race condition on concurrent writes (MEDIUM)
- monitor.py: _handle_position_closed uses last monitored price, not actual IG fill price (MEDIUM)
- exit_manager.py: Runner phase trailing stop can exceed IG rate limit (30 non-trading/min) (MEDIUM)

---

## Bug Log (fixed)
- Force-open alert showed 0.05 lots in Telegram but trade executed at 0.01 — Tokyo lot cap was applied AFTER the initial Telegram notification, BEFORE execution. Fixed 2026-03-06 by removing Tokyo lot cap entirely.
- get_safe_lot_size() silently ignored sl_distance, used 50% margin cap → produced 30%+ account risk. Fixed 2026-03-07: full risk-based rewrite (2% of balance / sl_distance).

## Session Notes (2026-03-07 session 3 — remaining 4-agent items)
- SL_FLOOR_PTS: 120 → 60 (backtest: tight SL=60 PF=1.16 vs wide SL PF=1.10)
- PROMPT_LEARNINGS metadata fix: extract setup_type/session/ai_reasoning from entry_context JSON (not position_state columns which don't have those fields). All future learnings will be properly tagged.
- Tier 3 noise removal from AI prompt (analyzer.py _fmt_indicators):
  - EMA200: now only shown for D1 and 4H (not 15M/5M where it's noise)
  - Fibonacci: reduced from 5 levels to 2 nearest (1 SUP below + 1 RES above)
  - Removed: bounce_starting bool, body_trend/consecutive_direction/wick_ratio block
  - Removed from Market Structure: prev_week_high/low, tick_density
  - Removed from web research: Fear & Greed (CNN crypto-based, not Nikkei-specific)
- New setups: tokyo_gap_fill + london_orb (session_context required)
  - detect_setup() now accepts session_context: dict = None (backward compatible)
  - monitor.py: compute_session_context() now called BEFORE detect_setup + reused for snap.update()
  - tokyo_gap_fill: ≥100pt overnight gap, early Tokyo 00-02 UTC, direction = fill gap
  - london_orb: break above/below Asia range at London open 08-10 UTC, vol≠LOW
- AI system prompt updated with SESSION SETUPS section

## Session Notes (2026-03-07)
AI Escalation Quality fixes (session 2):
- analyzer.py: LOCAL SCORE block now explains criteria-based nature + historical WR ranges (~43% at 70-79%, ~34% at 80-89%, ~46% at 90-100%). AI no longer mistakes 72% score for 72% probability.
- analyzer.py: build_scan_prompt() now accepts open_positions_context dict (count, directions, daily_pnl). Sonnet sees portfolio state before deciding.
- analyzer.py: _fmt_web_research() adds USD/JPY directional context (JPY>152 = tailwind, <148 = headwind) + MEDIUM-impact calendar events.
- context_writer.py: macro.md now includes MEDIUM-impact events + USD/JPY direction framing.
- monitor.py: collects open_positions_context before scan_with_sonnet call (storage.get_open_positions + daily P&L from recent trades).
- DISABLED_SETUP_TYPES: added ema9_pullback_short (29% WR, structural loser outside crash regimes).
- bollinger_mid_bounce proximity: tightened 150→80pts (reduces marginal entries at band edge). REVERTED later — overfitting. Currently at 80pts (needs re-check).
- Key principle: backtest has no AI. Optimizing backtest parameters is overfitting. The AI filter IS the edge.

Major overhaul across 4 phases completed:
- Phase 1 (settings + risk_manager + database + monitor):
  - get_safe_lot_size() rewritten: 2% risk-based, ATR-aware floors, confidence scaling, drawdown protection
  - get_dynamic_sl() added: multiplier × ATR, floored at 120pts
  - validate_trade(): 12 checks now (portfolio_risk cap 8%, dollar_risk now enforced at MAX_RISK_PERCENT=3%)
  - MAX_OPEN_POSITIONS: 1→3. MAX_MARGIN_PERCENT: 50%→5%.
  - database.py: get_open_positions_count() + get_open_positions() added
  - monitor.py: main loop now allows concurrent scan when 0<open_count<MAX_OPEN_POSITIONS (background task). Race check uses open_count>=MAX not has_open.
- Phase 2 (analyzer.py): DECISION FRAME (5-30min horizon), FATAL FLAWS section (7 disqualifiers), 5-gate chain replaced 7-step, expected vs unexpected failed criteria, secondary setup removed from Sonnet, time-of-day context header, confidence_breakdown removed from JSON schema.
- Phase 3 (confidence.py): 9-criteria weighted scoring (was 12 equal-weight). C7/C8 pre-gates only. entry_timing = ha_aligned OR entry_quality.
- Tests: 424/424 passing (2026-03-07)

## Tokyo Session
Tokyo (00:00-06:00 UTC) uses same lot sizing and consecutive-loss rules as other sessions.
AI (Sonnet + Opus) receives ATR14 value in prompt: explicit rule to widen SL/TP when ATR > 120pts.
compute_atr(candles, period) in core/indicators.py. Called in analyze_timeframe() → result["atr"].

---

## Setup Types (detect_setup reference)
### Mean-Reversion LONG
| Type | Trigger | RSI | Gate |
|------|---------|-----|------|
| bollinger_mid_bounce | ±150pts from BB mid | 30-65 | bounce_starting OR (RSI<40 + wick/HA/candle) |
| bollinger_lower_bounce | ±150pts from BB lower | 20-40 | lower_wick ≥15pts |
| extreme_oversold_reversal | RSI<22 + 4H near BB lower(300pts) | <22 | wick/HA/candle/sweep |
| oversold_reversal | RSI<30 + daily bullish | <30 | wick/HA/candle/sweep |
### Momentum/Trend LONG
| breakout_long | near BB upper/swing_high | 55-75 | vol≥1.3x + HA bullish + above EMA50 |
| vwap_bounce_long | near VWAP(120pts) + above EMA50 | 40-65 | bounce confirm |
| ema9_pullback_long | near EMA9(100pts) + above EMA50 | 40-65 | HA bullish or turning |
| momentum_continuation_long | above EMA50+VWAP | 45-70 | HA streak≥2 + vol not LOW |
### SHORT (13 types)
bb_upper_rejection, ema50_rejection, bb_mid_rejection, overbought_reversal, breakdown_continuation,
dead_cat_bounce_short, bear_flag_breakdown, vwap_rejection_short, high_volume_distribution,
multi_tf_bearish, ema200_rejection, lower_lows_bearish_momentum, pivot_r1_rejection,
momentum_continuation_short, vwap_rejection_short_momentum
### Session-Specific (require session_context)
| tokyo_gap_fill | ≥100pt overnight gap | Tokyo 00-02 UTC | RSI 30-70 | direction = gap fill |
| london_orb | break above/below Asia range | London 08-10 UTC | vol≠LOW | RSI 32-72 |
### All types: SL=150, TP=400. 5M fallback: _5m suffix. C1 penalizes counter-trend.

---

## Important Behavioral Notes (hard-won, never forget)
- **POSITIONS_API_ERROR** is a sentinel in ig_client.py. Check with `is POSITIONS_API_ERROR`, not `not`.
- **open_trade_atomic()** in storage.py: log_trade_open + set_position_open in one DB transaction. Always use this.
- **Telegram starts FIRST** in TradingMonitor.start() — before IG connection. Retries IG every 5 min.
- **Startup sync** handles multi-position: iterates ALL IG positions and ALL DB positions independently.
- **Multi-position tracking** (2026-03-10): trades table is source of truth for position state.
  - `get_position_state()` reads from trades WHERE closed_at IS NULL (not position_state singleton).
  - `get_all_position_states()` returns ALL open positions as list.
  - position_state table kept for legacy compat but never dropped.
  - Per-position trackers in monitor: `_position_trackers[deal_id]` dict with momentum_tracker, price_buffer, etc.
  - `set_position_closed(deal_id)` requires deal_id param. `log_trade_close` must be called first (sets closed_at).
  - pending_alerts table replaces pending_alert column in position_state.
  - Telegram /close and /kill show selection buttons when 2+ positions.
  - Dashboard API returns `positions` (list) + `position` (first, backward compat).
  - Frontend `renderPositions()` renders multiple position cards.
- **MomentumTracker** is per-position. Legacy singleton `self.momentum_tracker` synced to first position.
- **Local confidence pre-gate**: only escalates to AI if local score >= 60%. Sonnet rejects <50% skip Opus.
- **WebResearcher.research()** is synchronous/blocking → run in executor.
- **detect_setup()** bidirectional. BB_MID_THRESHOLD=150pts, BB_LOWER_THRESHOLD=80pts. No above_ema50 gate on mid bounce.
- **Session logic**: session.py uses UTC. get_current_session() is authoritative for live bot.
- **candlestick_patterns** (plural list) written by analyze_timeframe(). detect_setup() reads plural form. Both forms exist.
- **Dashboard ngrok header**: all fetch() calls must include `ngrok-skip-browser-warning: true`.

---

## Dashboard Running State
| systemd unit         | what it runs                                               | status  |
|----------------------|------------------------------------------------------------|---------|
| `japan225-bot`       | python monitor.py                                          | running |
| `japan225-dashboard` | uvicorn dashboard.main:app --host 127.0.0.1 --port 8080    | running |
| `japan225-ngrok`     | ngrok http --domain=unmopped-shrimplike-sook.ngrok-free.app 8080 | running |

Frontend: https://mostafaiq.github.io/japan225-bot/
Backend: https://unmopped-shrimplike-sook.ngrok-free.app
Auth: Bearer DASHBOARD_TOKEN (in .env). All routes except GET /api/health.

### Inter-process communication
| File | Writer | Reader |
|------|--------|--------|
| `storage/data/bot_state.json` | monitor._write_state() | /api/status |
| `storage/data/dashboard_overrides.json` | config_manager | monitor._reload_overrides() |
| `storage/data/force_scan.trigger` | /api/controls/force-scan | monitor |
| `storage/data/prompt_learnings.json` | post_trade_analysis() | AI prompts |
| `storage/data/brier_scores.json` | post_trade_analysis() | /brier-check skill |
| `storage/data/scan_analysis.md` | scan_analyzer.py (cron, hourly) | user inspection |
| `storage/data/probability_tracker.md` | probability_tracker.py (cron) | user inspection |

---

## Telegram
HTML parse_mode. REPLY_KB (4×2) always visible. `/menu` → inline panel.
Commands: `/status /balance /journal /today /stats /cost /force /stop /pause /resume /close /kill`
`/kill`=emergency close (no confirm). `/close`=confirm dialog. All handlers auth-gated by TELEGRAM_CHAT_ID.
Force Open: when local conf 100% (12/12) but AI rejects → Telegram alert. Force Open/Skip buttons. 15min TTL.

## DB + Digests
DB: `storage/data/trading.db` — Oracle VM only. WAL mode. Never commit.
Digests: `.claude/digests/` — settings · monitor · database · indicators · session · momentum ·
         confidence · ig_client · risk_manager · exit_manager · analyzer · telegram_bot · dashboard · claude_client
Tests: **437/437 passing** (2026-03-10).

## Backtest Benchmarks (2026-03-07 — new weighted confidence + risk-based sizing)
TA-Only OOS: Scalp SL=60 TP=300 → 690 trades, 47.8% WR, PF=1.16, +$3,305 ✓ PROFITABLE
TA-Only OOS: Swing SL=150 TP=600 → 416 trades, 52.2% WR, PF=0.76, -$5,505 ✗ LOSING
AI-Filtered (last 10d): 141 trades, 46.1% WR, PF=0.97, -$198 (near-breakeven)
AI delta vs no-filter: +1.5pp WR, +0.10 PF, +$2,555 P&L improvement.
Key: tight SL (60pts) beats wide SL (150pts). ATR-based SL targets this range. AI filter works.
