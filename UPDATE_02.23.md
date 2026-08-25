# Chandra Trading MODULAR-02.23

## Timing Candle — Pending-Limit-on-Armed

- PAPER behavior is unchanged.
- LIVE Timing Candle no longer waits for Python tick detection to submit a market order on retest.
- After a completed-candle BO/BD arms the setup, LIVE places a broker-side BUY_LIMIT/SELL_LIMIT at the Timing High/Low with the opposite Timing level as SL.
- MT5 executes the pending order automatically when price retests the level.
- Pending orders use MT5 RETURN filling, which is the appropriate filling mode for pending orders.
- Pending order ticket/side/price/SL are persisted and recovered after restart.
- A normal bot STOP cancels the pending Timing order to avoid unmanaged entries.
- Session end cancels any pending Timing order.
- If a pending order fills, the real MT5 position is detected and the OPEN trade is recorded once.
- Strategic Entry and Magical Entry logic are unchanged.
- Existing MT5 immediate-order filling-mode fix remains intact for other execution paths.
