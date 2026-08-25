# MODULAR-02.22

Timing Candle backtest diagnostic/session update.

- Keeps MT5 broker-time interpretation.
- Keeps selected broker-date range filtering.
- End Time remains an entry cutoff; it does not change the historical date range.
- Adds per-day diagnostics showing Timing Candle / breakout / retest status.
- Zero-trade days now explain whether the timing candle, breakout, or retest was missing.
- No Timing Candle strategy entry rules were changed.
