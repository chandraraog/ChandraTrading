MODULAR-02.17 — Manual status card hidden when no manual position. Manual controls/log remain available. Timing logic unchanged.

# Completed work carried into Modular 02.9

Based on the project artifacts supplied during the Chandra Trading work, the current runtime includes:

- Strategic Entry and Magical Entry signal/execution paths.
- Timing Candle strategy with BO/BD -> later retest -> entry behavior.
- Timing Candle N-trade behavior: SL does not terminate the strategy; a fresh BO/BD is required for the next trade.
- Timing Candle live retest evaluated independently of new-candle events.
- Broker-side initial Timing Candle SL.
- MT5 position recovery/reconciliation after restart.
- Optional Timing End Time; blank means broker-session-derived cutoff five minutes before session end.
- Dashboard strategy selection, per-strategy trade logs, collapsible logs, paper/live mode, MT5 test, manual orders, close controls and backtest UI.
- No strategy selected is allowed in the stopped UI; START requires at least one strategy.
- Successful Uvicorn access logging is disabled in `run_server.py` so polling does not flood the console.

## Refactor
Strategy-specific Timing Candle logic and backtest now live in `strategies/timing_candle.py`; Strategic and Magical strategy adapters live in their own files. `bot_manager.py` remains the orchestration layer.


## 02.10 - Latest Timing Candle Close Update
- bot_manager.py replaced with exact latest `bot_manager_timing_corrected.py`.
- frontend/index.html replaced with exact latest `index_timing_end_time.html`.
- Timing Candle end-time/close logic preserved from the latest source; blank end time remains broker-session-close behavior.

## MODULAR-02.13 — Timing Candle modular retest correction
- Timing Candle rules moved to pure strategy functions in `backend/app/strategies/timing_candle.py`.
- `bot_manager.py` now delegates BO/BD and retest decisions to the strategy module.
- Added compatibility exports `breakout_arm` and `retest_signal`.
- Future retest is detected from both live executable tick and completed candle OHLC, preventing a one-second polling gap from missing a wick touch.
- Breakout candle itself cannot trigger its own retest.
- Raw price precision is preserved in strategy calculations; rounding remains display/backtest-output only.
- Strategic Entry and Magical Entry logic unchanged.
- Added Timing Candle regression tests.


## MODULAR-02.14 UI SYNC
- UI aligned with the requested Trading Monitor layout.
- Manual Order status/order/log sections are visible by default.
- Preserved modular strategy/backend structure and Timing Candle retest changes.
- Default theme is light when no prior theme preference exists.

## MODULAR-02.15 — Timing Candle Intrabar Retest Fix (2026-08-20)

- Fixed the Timing Candle retest event so the next forming candle can trigger immediately on a wick touch.
- Root cause: the engine intentionally removes the forming candle from `calculate_frame()`, so `df.iloc[-1]` remains the breakout candle during the whole next candle. The previous pure strategy rule used that completed-bar key to block the live tick, causing the bot to wait until the next candle closed.
- Added `live_bar_key` to the pure Timing Candle strategy contract.
- `BotManager` derives the current forming candle key from the MT5 tick and passes it to `retest_signal()`.
- Breakout candle OHLC is still excluded from retest detection.
- Future live tick touch of Timing High/Low now triggers immediately.
- Future completed candle wick remains a fallback if the live polling interval misses the exact touch.
- Raw price precision is preserved; no strategy rounding was introduced.
- Tests: 9 passed.
## MODULAR-02.16 — Manual Order UI + Trade Log Fix (2026-08-20)

- Added Manual Order minimize/maximize (collapse/expand) control with localStorage persistence.
- Fixed `/api/trading/trades` response shape to return `{ "trades": [...] }`, matching the frontend contract.
- Made frontend trade-log loading tolerant of both the corrected object response and the previous raw-array response.
- Manual Orders now appear/update correctly in the dedicated trade log after OPEN/CLOSE records are created.
- Trade P&L summary now totals realized P&L from CLOSE records only.
- Preserved the Timing Candle intrabar retest fix from MODULAR-02.15.
- Validation: `PYTHONPATH=. pytest -q` -> 9 passed; Python compileall passed; frontend JavaScript syntax check passed.
## MODULAR-02.21 — Timing Configuration UI Defaults
- Timing Candle configuration labels simplified to `Timing Candle Hour`, `Timing Candle Minute`, and `End Time (Optional)` (no IST suffix in UI).
- Default Timing Candle set to `04:00`.
- Default End Time set to `08:00`.
- User can still clear End Time to use the MT5 broker-session fallback behavior.
- User-facing SL note changed from `L100` to `Red Line`; internal `L100` calculation remains unchanged.
- Backtest timing defaults aligned to `04:00`.



## MODULAR-02.21
- Fixed Timing Candle backtest historical data retrieval by adding `chandra_core.get_rates_range()`.
- Backtest now uses MT5 `copy_rates_range()` for explicit date ranges.
- No Timing Candle live/retest/SL logic changed.
