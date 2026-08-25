# Chandra Trading MODULAR-02.21

## Timing Candle clock standardization

Timing Candle now uses the **MT5 broker/server clock directly** across Paper, Live, and Backtest.

- No IST conversion.
- No UTC conversion in Timing Candle date/time interpretation.
- Backtest From/To dates are matched directly against the MT5 timestamps returned by the historical loader.
- Timing Candle Hour/Minute and optional End Time use the same broker/server clock as Paper/Live.
- Selected backtest Quick range button remains highlighted.
- Manual date changes clear the Quick range selection.
- Backtest End Time field is visible when Timing Candle is selected.

## Validation

11 tests passed with `PYTHONPATH=. pytest -q`.
