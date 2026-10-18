import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from backend.app.services import pob_execution as module


def test_temporary_windows_lock_retries_atomic_save(tmp_path, monkeypatch):
    path = tmp_path / "pob_state.json"
    p = module.POBExecution(path, NS())
    p.save()
    original = path.read_bytes()
    replace = Path.replace
    attempts = []
    def locked_then_ok(source, destination):
        attempts.append(source)
        assert path.read_bytes() == original
        if len(attempts) < 4:
            raise PermissionError(13, "Access denied")
        return replace(source, destination)
    monkeypatch.setattr(Path, "replace", locked_then_ok)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    p.data["error"] = "updated"
    p.save()
    assert len(attempts) == 4
    assert attempts[0].name != "pob_state.tmp"
    assert json.loads(path.read_text())["error"] == "updated"
    assert not list(tmp_path.glob("*.tmp"))


def test_persistent_lock_preserves_journal_and_prevents_order(tmp_path, monkeypatch):
    requests = []
    p = module.POBExecution(tmp_path / "pob_state.json",
                            NS(mt5=NS(order_send=lambda r: requests.append(r))))
    p.save()
    original = p.path.read_bytes()
    attempts = []
    def locked(*args):
        attempts.append(1)
        raise PermissionError(13, "Access denied")
    monkeypatch.setattr(Path, "replace", locked)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        p._send({}, "entry")
    assert len(attempts) == 7
    assert requests == []
    assert p.path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))
