from types import SimpleNamespace as NS
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.app import bot_manager as bm
from backend.app.api import trading


def make(tmp_path,monkeypatch,features,limit=1):
    monkeypatch.delenv("CHANDRA_DEV_MODE",raising=False)
    monkeypatch.setattr(bm,"HISTORY_FILE",tmp_path/"history.json")
    monkeypatch.setattr(bm,"POB_FILE",tmp_path/"pob.json")
    # Exercise the real require/authorization path with a validated payload.
    monkeypatch.setattr(bm.LicenseService,"validate",lambda self:{"features":features,"max_lot":limit})
    return bm.BotManager()


def request():
    return dict(strategies=["pob"],symbol="TEST",timeframe="M1",lot=.01,mode="paper",pob_high=110,pob_low=100)


def test_missing_pob_rejected_without_mutation(tmp_path,monkeypatch):
    m=make(tmp_path,monkeypatch,["TIMING_CANDLE"])
    before=m.state.strategies.copy()
    with pytest.raises(PermissionError,match="POB"):m.configure(**request())
    assert m.state.strategies==before and m._pob.data["config"] is None


@pytest.mark.parametrize("features",[["POB"],["pob"],["*"]])
def test_authorized_pob_can_configure(tmp_path,monkeypatch,features):
    m=make(tmp_path,monkeypatch,features)
    m.configure(**request())
    assert m.state.strategies==["pob"]


def test_individual_lot_cannot_bypass_limit(tmp_path,monkeypatch):
    m=make(tmp_path,monkeypatch,["POB"],.02)
    with pytest.raises(PermissionError,match="maximum lot"):
        m.configure(**request(),strategy_settings={"pob":{"lot":.03}})
    assert m._pob.data["config"] is None


def test_start_and_resume_recheck_saved_features(tmp_path,monkeypatch):
    m=make(tmp_path,monkeypatch,["TIMING_CANDLE"])
    m.state.strategies=["pob"]
    m.mt5_test=lambda:pytest.fail("Broker must not be accessed for unlicensed start")
    with pytest.raises(PermissionError,match="POB"):m.start()
    assert not m.state.running
    m.state.running=True;m.state.entries_enabled=False
    with pytest.raises(PermissionError,match="POB"):m.resume_trading()
    assert not m.state.entries_enabled


def test_direct_api_rejects_unlicensed_deployment(tmp_path,monkeypatch):
    m=make(tmp_path,monkeypatch,["TIMING_CANDLE"])
    monkeypatch.setattr(trading,"manager",m)
    app=FastAPI();app.include_router(trading.router)
    with TestClient(app) as client:
        response=client.post("/api/trading/configure",json=request())
    assert response.status_code==403
    assert "POB" in response.json()["detail"]
