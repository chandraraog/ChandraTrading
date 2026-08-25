def test_timing_backtest_supports_optional_end_time():
    from backend.app.bot_manager import BotManager
    import inspect
    sig = inspect.signature(BotManager.timing_backtest)
    assert "timing_end_time" in sig.parameters
