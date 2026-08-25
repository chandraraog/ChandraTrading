# MODULAR-02.22.2

## Restart-safe live position recovery
- On bot restart, existing Chandra MT5 positions are recovered by strategy magic number.
- Existing OPEN trade-history records are matched by MT5 ticket and their trade_id is reused.
- Restart no longer creates a duplicate OPEN trade-log record for the same broker position.
- Broker position remains the source of truth for side, entry, SL, target, volume, ticket and current P&L.

## Live Trade Log refresh
- Dashboard trade-log polling now adds a cache-busting query parameter.
- Polling awaits the trade-log refresh, so new OPEN/CLOSE records render promptly.
- No browser/page refresh is required.

## Scope
- No Timing Candle entry/retest rules changed.
- No Strategic Entry logic changed.
- No Magical Entry logic changed.
- No MT5 strategy behavior changed.
