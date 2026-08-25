from __future__ import annotations
from . import chandra_core as core

class ChandraTrendEngine:
    """Closed-candle data/indicator facade used by the web trading runtime."""
    def __init__(self, symbol: str, timeframe: str, bars: int = 3000):
        self.symbol=symbol; self.timeframe=timeframe; self.bars=bars
    def initialize(self):
        core.connect_mt5(); core.ensure_symbol(self.symbol)
    def calculate_frame(self):
        df=core.get_rates(self.symbol,self.timeframe,self.bars)
        if len(df) < 2:
            raise RuntimeError("Not enough closed candles")
        return core.calculate_pine_engine(df)
