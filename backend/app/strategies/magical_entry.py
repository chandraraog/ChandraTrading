from __future__ import annotations

class MagicalEntryStrategy:
    """Magical Entry adapter. The Pine-equivalent signal engine remains in chandra_core."""
    def __init__(self, manager):
        self.manager = manager

    def process_candle(self, row, signal, state, lot, live):
        return self.manager.core.process_candle(
            self.manager.state.mt5_symbol, row, signal, lot, live, state
        )
