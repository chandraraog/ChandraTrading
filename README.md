MODULAR-02.15 — Timing Candle Intrabar Retest Fix

# Chandra Trading — Modular 02.16

This project is a modular refactor of the current Chandra Trading web/paper-trading runtime.

## Strategies
- Strategic Entry — `backend/app/strategies/strategic_entry.py`
- Magical Entry — `backend/app/strategies/magical_entry.py`
- Timing Candle — `backend/app/strategies/timing_candle.py`

## Common runtime
- `backend/app/bot_manager.py` — orchestration/state/API-facing manager
- `backend/app/engine/chandra_core.py` — Pine-equivalent indicator + MT5 execution primitives
- `backend/app/engine/chandra_trend_engine.py` — closed-candle data facade
- `backend/app/api/` — HTTP endpoints
- `frontend/index.html` — current dashboard UI

## Timing Candle behavior included
- Timing Candle High/Low are locked from the latest completed candle returned by the engine.
- Completed candle close beyond the Timing level arms BO/BD.
- Later live tick retest triggers entry; no retest-close confirmation.
- After SL, a new BO/BD is required; N trades are possible.
- Optional End Time. Blank End Time uses broker-session history and stops five minutes before session close.
- Initial Timing Candle SL is sent to MT5 with the order.
- Existing MT5 positions are reconciled after restart.

## Run
1. Start/login to MT5.
2. Install dependencies: `pip install -r requirements.txt`
3. From this project root run: `python run_server.py`
4. Open `http://127.0.0.1:8000`

LIVE trading can send real MT5 orders. Test in paper mode first.


## MODULAR-02.14 UI SYNC
- UI aligned with the requested Trading Monitor layout.
- Manual Order status/order/log sections are visible by default.
- Preserved modular strategy/backend structure and Timing Candle retest changes.
- Default theme is light when no prior theme preference exists.


## MODULAR-02.16 UI / Trade Log Fix
- Manual Order card supports minimize/maximize (collapse/expand) and remembers the preference.
- Trade Logs API and frontend response contract are aligned.
- Manual OPEN/CLOSE records are displayed in the Manual Orders trade log.
