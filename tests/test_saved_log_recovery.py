from types import SimpleNamespace as NS
from unittest.mock import Mock
from backend.app import bot_manager as bm

def test_saved_exit_recovery_is_idempotent(tmp_path,monkeypatch):
    monkeypatch.setattr(bm,'HISTORY_FILE',tmp_path/'history.json')
    monkeypatch.setattr(bm,'POB_FILE',tmp_path/'pob.json')
    m=bm.BotManager()
    m.state.trades=[dict(mode='live',action='OPEN',ticket='42',strategy='timing',side='SELL',symbol='XAUUSD.v',price=4122.51,volume=.03,trade_id='x')]
    entry=NS(entry=0,symbol='XAUUSD.v',type=1,price=4122.51,magic=bm.MAGIC_BY_STRATEGY['timing'],volume=.03)
    close=NS(entry=1,volume=.03,price=4128.71,profit=-18.6,time=1791498780,time_msc=1791498780095,comment='[sl 4128.71]')
    mt=NS(initialize=lambda:True,positions_get=Mock(return_value=[]),history_deals_get=Mock(return_value=[entry,close]))
    monkeypatch.setattr(bm.core,'mt5',mt)
    m.state.running=True  # Older orphan tickets must reconcile during a new run.
    assert m.reconcile_saved_trade_logs()==1
    assert m.state.trades[0]['pnl']==-18.6 and m.state.trades[0]['action']=='CLOSE'
    m._last_log_reconcile=-100
    assert m.reconcile_saved_trade_logs()==0
    assert len(bm.BotManager().state.trades)==2
    m.state.trades=m.state.trades[1:];m._last_log_reconcile=-100
    mt.positions_get.return_value=[NS(ticket=42)]
    assert m.reconcile_saved_trade_logs()==0
    mt.positions_get.return_value=[];entry.magic=99;m._last_log_reconcile=-100
    assert m.reconcile_saved_trade_logs()==0
