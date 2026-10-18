from types import SimpleNamespace as NS
from backend.app.bot_manager import BotManager, StrategyState


def test_target_retries_failed_close_after_points_drop():
    m=BotManager.__new__(BotManager)
    ss=StrategyState("timing",NS(magic_number=123,entries_enabled=True))
    ss.session_target_points=5
    ss.paper_position=1
    m.state=NS(mode="live",strategy_states={"timing":ss})
    points=[6]
    m._strategy_open_points=lambda _:points[0]
    remaining=[object()]
    m._live_positions=lambda _:remaining
    calls=[]
    def close(name):
        calls.append(name)
        if len(calls)==1:raise RuntimeError("temporary broker failure")
        remaining.clear()
        ss.paper_position=0
    m.close_strategy=close
    m._enforce_strategy_targets()
    assert ss.session_target_reached and not ss.execution.entries_enabled
    assert "close pending" in ss.target_close_message
    points[0]=3
    m._enforce_strategy_targets()
    assert calls==["timing","timing"]
    assert "position closed" in ss.target_close_message
    assert not ss.execution.entries_enabled
