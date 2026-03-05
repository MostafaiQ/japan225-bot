# Japan 225 Bot — Session History Archive
Historical session notes moved from MEMORY.md to keep it under 200 lines.
Read MEMORY.md for current state. Read relevant digests for module details.

---

## Session Notes (2026-03-05 #5) — Bug fixes (12 bugs)
- exit_manager.py: BUG-008 — `except ValueError` → `except (ValueError, TypeError)` + TZ stripping
- monitor.py: BUG-022 — Force-open path now applies Tokyo lot cap
- indicators.py: BUG-002 — `analyze_timeframe()` stores candlestick_patterns (plural list)
- monitor.py: BUG-016 — milestone_alert() sent via send_alert() (was wrong call)
- confidence.py: BUG-017 — C3 SHORT RSI 30-60 (was LONG range 55-75)
- indicators.py: BUG-020 — result["vwap"] overridden with anchored_vwap_daily
- confidence.py: BUG-006 — C10/C11 default False when data unavailable (was True)
- monitor.py: BUG-015 — Opus rr logged as rr_computed (was overwritten)
- risk_manager.py: BUG-014 — month-end check uses trading-day counting
- session.py: BUG-010 — Friday blackout end `< blackout_end` (was `<=`)
- dashboard/services/ig_history.py: BUG-007 — docstring corrected: "1 min" cache TTL
- tests/test_bug_fixes.py: NEW — 29 tests for 6 code-level bug fixes
- Tests: 437/437 passing → 444/444 after streaming tests

## Session Notes (2026-03-05 #4) — AI prompt engineering (Wyckoff + SMC + VP + token optimization)
- analyzer.py: build_system_prompt() — 14,568 chars → 9,690 chars (-34% / ~1,220 tokens saved)
- WYCKOFF PHASE DETECTION: Accumulation/Markup/Distribution/Markdown/Coil
  Spring (swept_low+recovery)=LONG, UpThrust (swept_high+fail)=SHORT, Coil=lower bar for MR
- VOLUME PROFILE USAGE: POC=equilibrium, VAH from below=resistance, VAL from above=support
  Inside VA=slow mean-reversion. LVN=fast movement. VP edge at slow-day band extremes → -5pts threshold
- SMC CONCEPTS: Order Block, FVG, sweeps (BOS/CHoCH). Spring=sweep+bullish_FVG+demand_OB
- build_scan_prompt(): WYCKOFF/SMC CONTEXT block pre-computed from live 15M/4H indicators
  Phase hint | Bias | HA streaks | Sweep status | VP position | BB width with COIL tag
- Role block expanded: 7-step. SLOW DAY CHECK: coil + band extremes → lower confidence threshold

## Session Notes (2026-03-05 #3) — telegram_bot.py bug fixes
- send_position_eval: Opus reasoning now html.escape()'d (was silent HTML parse failure)
- CLOSE_NOW threshold: conf >= 60 → conf >= 70 (aligned with monitor.py auto-close gate)
- send_force_open_alert: set_pending_alert() moved before try block + HTML fallback retry
- _journal_text(): expanded — shows SL, TP, computed R:R, confidence, duration, session
- Auth bypass closed: _auth() decorator on ALL handlers — auth-gates by TELEGRAM_CHAT_ID

## Session Notes (2026-03-05 #2) — journal corruption fixes
- ig_history.py: _ts_fallback_match() added — timestamp ±60s + direction fallback when ref match fails
- opened_by/closed_by inference fixed — bot trades no longer labeled "Manual"
- dur_str == "—" guard: DB placeholder no longer injected into notes
- _sync_trades_to_db rowcount: conn.total_changes → cursor.rowcount (per-statement)
- Double replace: .replace("DIAAAAQ", "", 1) (was chained double replace)
- _ts_fallback_match loop mutation fixed: local cmp_txn instead of mutating txn_ts

## Session Notes (2026-03-05 #1) — major context enrichment
- counter_signal: Sonnet JSON has counter_signal/counter_reasoning. monitor.py triggers Opus on opposite
- New indicators: anchored_vwap_daily/weekly, volume_poc/vah/val, equal_highs/lows_zones, compute_session_context()
- Sonnet prompt: MARKET STRUCTURE block (weekly/daily VWAP, POC/VAH/VAL, PDH/PDL, prev week H/L, gap, Asia range)
- Lightstreamer: CHART:5MINUTE CONS_TICK_COUNT → tick density (HIGH_ABSORPTION/HIGH_EXPANSION/NORMAL)
- confidence.py: C2 enriched with weekly anchored VWAP proximity (within 200pts)
- Tests: 395/395 passing

## Market Structure Features
- indicators.py: anchored_vwap(candles, anchor_isodate), compute_volume_profile(candles, lookback, bucket_size)
  detect_equal_levels(candles, lookback, tolerance), compute_session_context(candles_15m, candles_daily)
- analyze_timeframe() appends: anchored_vwap_daily/weekly, volume_poc/vah/val, equal_highs/lows_zones
- analyzer.py _fmt_indicators(): MARKET STRUCTURE block with all new fields

## AI Decision Quality Fixes (2026-03-04)
- confidence.py: C1 daily trend: EMA50 primary (was EMA200 — lagged 5400pts below price, always "bullish")
- settings.py: EXTREME_DAY_RANGE_PTS=1000, EXTREME_DAY_MIN_CONFIDENCE=85, OVERSOLD_SHORT_BLOCK_RSI_4H=32
- risk_manager.py: extreme day gate — intraday range > 1000pts AND confidence < 85% → trade blocked
- analyzer.py: 5M data in prompt, full fibonacci grid (5 levels), BB width, MARKET REGIME block
- analyzer.py: extreme day rules in system prompt (crash + bull day). Oversold short prohibition. Overbought long prohibition.
- analyzer.py: warning severity rule — 4+ warnings → <70%, 6+ warnings → <60%
- Tests: 408/408 passing (before 2026-03-05 bug session)

## Architecture Change (2026-03-05) — Opus opposite-direction swing path
- After Sonnet rejects primary direction, Opus evaluates OPPOSITE direction as swing trade
- Gate: opposite direction must have detected setup + local conf >= 60% + Sonnet conf >= 30%
- evaluate_opposite() in ai/analyzer.py: full context, full SL/TP freedom, same thresholds
- Old evaluate_scalp() kept for momentum bypass path only
- storage/database.py: save_opus_decision() + get_recent_opus_decision() (30-min persistence)

## Critical Fixes (2026-03-04)
- monitor.py: SL/TP verification after order placement — verifies stopLevel/limitLevel in deal confirmation
- monitor.py: Sequential Opus pipeline — Sonnet first → Opus after with Sonnet's analysis
- monitor.py: Sonnet confidence gate — Sonnet rejects < 50% skip Opus entirely
- backtest.py: Direct Anthropic API (anthropic SDK) — no timeouts, ~5x faster
- ig_history.py: Reuse cached IG session (1hr TTL), threading lock, cache TTL 60→300s
- ig_client.py: _check_auth_error catches empty error strings. get_market_info retries on auth error
- ig_client.py: close_position missing args fixed (epic, expiry, level, quote_id)
- monitor.py: SIGUSR1 handler for instant dashboard force scan

## Critical Fixes (2026-03-03)
- monitor.py: _trade_execution_lock (asyncio.Lock) wraps _on_trade_confirm(). Prevents race conditions
- monitor.py: Distance-based SL/TP (stop_distance/limit_distance) instead of absolute levels
- monitor.py: _execute_scalp() re-fetches live price before execution
- risk_manager.py: get_safe_lot_size() margin-only (50% cap). MAX_RISK_PER_TRADE removed
- claude_client.py: Dashboard chat exit -15 fix: start_new_session=True, safety prompt
- analyzer.py: --tools "" disables file access → pure analysis, cuts response 60-180s → 10-30s

## Critical Fixes (2026-03-02)
- ig_client.py: CRITICAL — Pandas 2.3.3 conv_resol() breaks on "MINUTE_15"/"DAY". Added _PANDAS_RESOLUTIONS map
- ig_client.py: get_all_timeframes() "HOUR4" → "HOUR_4"
- settings.py: PRE_SCREEN_CANDLES 50→220, AI_ESCALATION_CANDLES 100→220
- settings.py: AI_COOLDOWN_MINUTES 30→15
- monitor.py: action_taken includes direction suffix for dashboard (_long/_short)
- monitor.py: No cooldown on AI reject. Haiku pre-gate REMOVED

## AI Pipeline (current)
- Auth: Claude Code CLI (OAuth/subscription) — no ANTHROPIC_API_KEY in analyzer
  Single `claude --model sonnet-4-6 --print --effort low --tools "" --agents {...}` subprocess
  ANTHROPIC_API_KEY stripped from env before each call to force OAuth
- Opus sub-agent: --agents flag. Same subprocess, no extra Node.js startup
- Conditional Opus: --agents only loaded when local conf 60-86%
- WebResearcher: Google News RSS, nager.date JP holidays, CNN Fear & Greed
- prompt_learnings.json: auto-updated after each trade close, injected into future prompts
- brier_scores.json: Brier score calibration tracking

## Backtest Results (2026-03-04, last 10 trading days Feb 16-Mar 02)
- Raw: Scalp SL=60/TP=300 PF=1.25 (+$13k). Swing SL=150/TP=600 PF=0.88 (-$7.6k)
- AI filtered (305 setups): Sonnet approved 49 (16%). AI improves PF 0.54 → 0.72
- Combined 1H+15M: 2222 qualifying setups, 807 trades after dedup. 44% WR PF=0.70 (without AI)
