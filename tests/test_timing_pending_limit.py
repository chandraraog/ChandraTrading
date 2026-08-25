from pathlib import Path


def test_pending_limit_execution_hooks_present():
    root = Path(__file__).parents[1]
    bot = (root / "backend/app/bot_manager.py").read_text(encoding="utf-8")
    core = (root / "backend/app/engine/chandra_core.py").read_text(encoding="utf-8")
    assert "timing_pending_ticket" in bot
    assert "_place_timing_pending" in bot
    assert "_sync_timing_pending" in bot
    assert "place_limit_order" in core
    assert "TRADE_ACTION_PENDING" in core
    assert "ORDER_TYPE_BUY_LIMIT" in core
    assert "ORDER_TYPE_SELL_LIMIT" in core
    assert '"type_filling": mt5.ORDER_FILLING_RETURN' in core
