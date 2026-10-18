from types import SimpleNamespace as NS
import pytest
from backend.app import bot_manager as bm


def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(bm.LicenseService,"require",lambda self:{"features":["*"],"max_lot":100})
    monkeypatch.setattr(bm,"HISTORY_FILE",tmp_path/"history.json")
    monkeypatch.setattr(bm,"POB_FILE",tmp_path/"pob.json")
    m=bm.BotManager();m.state.mode="live"
    broker=NS(initialize=lambda:True,account_info=lambda:NS(login=1,server="TEST"),positions_get=lambda:[],orders_get=lambda:[])
    monkeypatch.setattr(bm.core,"mt5",broker)
    return m,broker


@pytest.mark.parametrize("kind",["running","position","order","unavailable","pob","account"])
def test_live_to_paper_rejected_before_config_changes(tmp_path,monkeypatch,kind):
    m,b=setup(tmp_path,monkeypatch)
    if kind=="running":m.state.running=True
    if kind=="position":b.positions_get=lambda:[NS(magic=bm.MAGIC_BY_STRATEGY["timing"])]
    if kind=="order":b.orders_get=lambda:[NS(magic=bm.MAGIC_BY_STRATEGY["strategic"])]
    if kind=="unavailable":b.positions_get=lambda:None
    if kind=="pob":m._pob.data["pending"]={"kind":"entry"}
    if kind=="account":m.state.account={"login":2}
    with pytest.raises(ValueError):m.configure(["strategic"],"XAUUSD","M1",.01,"paper")
    assert m.state.mode=="live"


def test_stopped_flat_can_switch_without_touching_unrelated_positions(tmp_path,monkeypatch):
    m,b=setup(tmp_path,monkeypatch)
    b.positions_get=lambda:[NS(magic=999)]
    m.configure(["strategic"],"XAUUSD","M1",.01,"paper")
    assert m.state.mode=="paper"
