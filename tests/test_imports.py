def test_imports():
    from backend.app.bot_manager import BotManager
    from backend.app.strategies.timing_candle import TimingCandleStrategy
    from backend.app.strategies.strategic_entry import StrategicEntryStrategy
    from backend.app.strategies.magical_entry import MagicalEntryStrategy
    from backend.app.engine.chandra_trend_engine import ChandraTrendEngine
    assert BotManager and TimingCandleStrategy and StrategicEntryStrategy and MagicalEntryStrategy and ChandraTrendEngine


def test_historical_range_loader_exists():
    from backend.app.engine import chandra_core as core
    assert callable(core.get_rates_range)
