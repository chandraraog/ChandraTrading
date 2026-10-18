from types import SimpleNamespace as NS
import pytest
from backend.app import bot_manager as bm


def test_paper_pnl_moves_between_candles_for_both_instruments(monkeypatch):
    m=bm.BotManager.__new__(bm.BotManager)
    a=NS(paper_position=-1,paper_entry_price=4169.27,volume=.03,current_pnl=0)
    b=NS(paper_position=1,paper_entry_price=30,volume=.02,current_pnl=0)
    m.state=NS(mode="paper",strategy_states={"timing":a,"timing2":b})
    m._strategy_symbol=lambda name:name
    m._strategy_contract=lambda name:100 if name=="timing" else 5000
    quotes={"timing":NS(bid=4167.1,ask=4167.49),"timing2":NS(bid=30.1,ask=30.2)}
    monkeypatch.setattr(bm.core,"mt5",NS(symbol_info_tick=lambda name:quotes[name]))
    m._refresh_paper_pnl()
    assert a.current_pnl==pytest.approx(5.34)
    assert b.current_pnl==pytest.approx(10)
    quotes["timing"].ask=4170.27
    m._refresh_paper_pnl()
    assert a.current_pnl==pytest.approx(-3)
    quotes["timing"]=None
    m._refresh_paper_pnl()
    assert a.current_pnl==pytest.approx(-3)
    m.state.mode="live"
    b.current_pnl=123
    m._refresh_paper_pnl()
    assert b.current_pnl==123
