from types import SimpleNamespace as NS
import pytest
from backend.app.bot_manager import BotManager


def test_overall_includes_removed_strategies_without_double_counting():
    m = BotManager.__new__(BotManager)
    m.state = NS(mode="live", trades=[
        dict(strategy="strategic", action="CLOSE", mode="live", pnl=20),
        dict(strategy="timing", action="CLOSE", mode="live", pnl=-5.88),
        dict(strategy="pob", action="PARTIAL_CLOSE", mode="live", pnl=3),
        dict(strategy="timing", action="OPEN", mode="live", pnl=None),
        dict(strategy="pob", action="CLOSE", mode="paper", pnl=100),
    ])
    active = {"timing": dict(position=1, pnl=10, total_pnl=4.12),
              "manual": dict(position=0, pnl=0)}
    assert m._overall_pnl(active) == pytest.approx(27.12)
    m.state.trades = []
    assert m._overall_pnl(active) == 10
    active["timing"]["position"] = 0
    assert m._overall_pnl(active) == 0
