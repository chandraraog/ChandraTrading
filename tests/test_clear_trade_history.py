import json
import threading
from types import SimpleNamespace as NS
import pytest
from backend.app import bot_manager as bm


def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(bm,"HISTORY_FILE",tmp_path / "trade_history.json")
    m=bm.BotManager.__new__(bm.BotManager)
    m._lock=threading.RLock()
    m.state=NS(trades=[{"event_id":"old"}])
    m._cleared_pob_events=set()
    m._pob=NS(lock=threading.RLock(),data={"events":[{"event_id":"old"}],"position":{"ticket":123},"pending":{"kind":"close"}})
    bm.HISTORY_FILE.write_text(json.dumps(m.state.trades))
    return m


def test_clear_file_preserves_recovery_and_survives_reload(tmp_path,monkeypatch):
    m=manager(tmp_path,monkeypatch)
    recovery=json.dumps(m._pob.data)
    assert m.clear_trade_history()["ok"]
    assert json.loads(bm.HISTORY_FILE.read_text())==[]
    assert m.state.trades==[]
    assert json.dumps(m._pob.data)==recovery
    m._load_trade_history()
    assert m._cleared_pob_events=={"old"}
    assert "new" not in m._cleared_pob_events
    m.state.trades=[{"event_id":"new"}]
    m._save_trade_history()
    assert json.loads(bm.HISTORY_FILE.read_text())==[{"event_id":"new"}]


def test_failed_clear_does_not_empty_memory(tmp_path,monkeypatch):
    m=manager(tmp_path,monkeypatch)
    def denied(*args,**kwargs):raise PermissionError("locked")
    from pathlib import Path
    monkeypatch.setattr(Path,"replace",denied)
    with pytest.raises(PermissionError):m.clear_trade_history()
    assert m.state.trades==[{"event_id":"old"}]
    assert json.loads(bm.HISTORY_FILE.read_text())==m.state.trades
