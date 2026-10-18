import pytest
from backend.app import bot_manager as bm


def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(bm.LicenseService,"require",lambda self:{"features":["*"],"max_lot":100})
    monkeypatch.setattr(bm,"HISTORY_FILE",tmp_path/"history.json")
    monkeypatch.setattr(bm,"POB_FILE",tmp_path/"pob.json")
    monkeypatch.setattr(bm.BotManager,"_resolve_strategy_markets",lambda self,settings:({n:c["symbol"] for n,c in settings.items()},{n:1.0 for n in settings}))
    m=bm.BotManager()
    config=dict(strategies=["timing"],symbol="TEST",timeframe="M1",lot=.01,mode="paper",timing_hour=4,timing_minute=0,timing_end_time="08:00")
    m.configure(**config)
    m.state.running=True
    return m,config


def test_deploy_preserves_running_position_and_pause(tmp_path,monkeypatch):
    m,c=setup(tmp_path,monkeypatch)
    old=m.state.strategy_states["timing"]
    old.paper_position=1;old.paper_sl=100;old.session_realized_pnl=12
    m.state.entries_enabled=False
    old.execution.entries_enabled=False
    m.configure(**dict(c,strategies=["timing","strategic"]))
    assert m.state.running and m.state.strategy_states["timing"] is old
    assert (old.paper_position,old.paper_sl,old.session_realized_pnl)==(1,100,12)
    assert not m.state.strategy_states["strategic"].execution.entries_enabled
    assert not m.state.entries_enabled


@pytest.mark.parametrize("change",[{"lot":.02},{"symbol":"OTHER"},{"timeframe":"M5"},{"mode":"live"},{"timing_entry_mode":"direct"},{"timing_end_time":"09:00"},{"strategies":["strategic"]},{"strategy_target_points":{"timing":10}}])
def test_running_edits_rejected_without_changes(tmp_path,monkeypatch,change):
    m,c=setup(tmp_path,monkeypatch)
    old=m.state.strategy_states["timing"]
    with pytest.raises(ValueError):m.configure(**dict(c,**change))
    assert m.state.strategy_states=={"timing":old}
    assert m.state.lot==.01 and m.state.timing_entry_mode=="retest"


def test_running_pob_allows_unrelated_deployment(tmp_path,monkeypatch):
    m,c=setup(tmp_path,monkeypatch)
    m.state.running=False
    c=dict(c,strategies=["pob"],pob_high=110,pob_low=100)
    m.configure(**c)
    m.state.running=True
    # An unresolved POB request must survive adding another strategy.
    m._pob.data["pending"]={"kind":"entry"}
    original=m._pob.strategy
    m.configure(**dict(c,strategies=["pob","timing"]))
    assert m._pob.strategy is original
    assert m._pob.data["pending"]=={"kind":"entry"}
    assert set(m.state.strategy_states)=={"pob","timing"}


def test_stopped_remove_timing_does_not_validate_hidden_inputs(tmp_path,monkeypatch):
    m,c=setup(tmp_path,monkeypatch)
    m.stop()
    m.configure(**dict(c,strategies=["strategic"],timing_hour=99,timing_end_time="bad",timing_entry_mode="unused"))
    assert m.state.strategies==["strategic"]
    assert m.state.timing_end_time is None


def test_remove_timing_preserves_active_pob(tmp_path,monkeypatch):
    m,c=setup(tmp_path,monkeypatch)
    m.stop()
    c=dict(c,strategies=["pob","timing"],pob_high=110,pob_low=100)
    m.configure(**c)
    m._pob.data["pending"]={"kind":"entry"}
    original=m._pob.strategy
    m.configure(**dict(c,strategies=["pob"]))
    assert m.state.strategies==["pob"]
    assert m._pob.strategy is original
    assert m._pob.data["pending"]=={"kind":"entry"}
