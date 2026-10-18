from types import SimpleNamespace
import pytest
from backend.app import bot_manager as bm


def test_corrupt_pob_does_not_block_stc(tmp_path, monkeypatch):
    journal=tmp_path/'pob.json'
    journal.write_text('',encoding='utf-8')
    monkeypatch.setattr(bm,'POB_FILE',journal)
    monkeypatch.setattr(bm,'HISTORY_FILE',tmp_path/'history.json')
    monkeypatch.setattr(bm.BotManager,'_authorize_strategies',lambda *a:None)
    monkeypatch.setattr(bm.BotManager,'_resolve_strategy_markets',lambda *a:({'timing':'TEST'},{'timing':1.0}))
    monkeypatch.setattr(bm.threading,'Thread',lambda **kw:SimpleNamespace(start=lambda:None,is_alive=lambda:False,join=lambda **kw:None))
    m=bm.BotManager()
    config=dict(strategies=['timing'],symbol='TEST',timeframe='M1',lot=.01,mode='live')
    m.configure(**config)
    m.mt5_test=lambda:{'connected':True}
    m._sync_live_snapshot=lambda ss:None
    m.start()
    assert m.state.running
    assert journal.read_bytes()==b''
    m.stop()
    with pytest.raises(ValueError,match='POB recovery journal'):
        m.configure(**dict(config,strategies=['pob'],pob_high=110,pob_low=100))
    m.state.strategies=['pob']
    with pytest.raises(RuntimeError,match='POB recovery journal'):m.start()
