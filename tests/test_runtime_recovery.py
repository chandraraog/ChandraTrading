import json
from types import SimpleNamespace
import pytest
from backend.app import bot_manager as bm

@pytest.fixture
def recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, 'HISTORY_FILE', tmp_path/'history.json')
    monkeypatch.setattr(bm, 'POB_FILE', tmp_path/'pob.json')
    monkeypatch.setattr(bm.BotManager, '_authorize_strategies', lambda *a: None)
    monkeypatch.setattr(bm.BotManager, '_resolve_strategy_markets', lambda self, settings: ({n:c['symbol'] for n,c in settings.items()}, {n:1.0 for n in settings}))
    monkeypatch.setattr(bm.threading, 'Thread', lambda **kw: SimpleNamespace(start=lambda:None, is_alive=lambda:False))
    m=bm.BotManager()
    m.configure(strategies=['timing','timing2'],symbol='XAUUSD',timeframe='M5',lot=.03,mode='paper', timing_hour=15,timing_minute=40, strategy_target_points={'timing':20}, strategy_settings={'timing2':{'symbol':'XAGUSD','timeframe':'M1','lot':.01}})
    m.state.running=True
    m.state.session_started_at='2026-10-08T15:40:00'
    ss=m.state.strategy_states['timing']
    ss.paper_position=-1;ss.paper_entry_price=4122.51;ss.paper_sl=4128.71
    ss.live_ticket='176137677';ss.active_trade_id='saved-trade'
    ss.session_realized_points=-6.2;ss.session_realized_pnl=-18.6
    ss.timing_high=4128.71;ss.timing_low=4122.34
    ss.timing_last_quote=(4121,4122)
    ss.execution.entries_enabled=False
    m._save_runtime()
    return m

def connect(m, account=None):
    def test():
        m.state.account=account or {'login':123,'server':'Test'}
        return {'connected':True}
    m.mt5_test=test

def test_restores_settings_position_and_progress_without_auto_start(recovery):
    m=bm.BotManager();ss=m.state.strategy_states['timing']
    assert not m.state.running and m._restart_pending
    assert m.state.strategy_settings['timing2']['symbol']=='XAGUSD'
    assert (ss.paper_position,ss.paper_entry_price,ss.paper_sl)==(-1,4122.51,4128.71)
    assert (ss.live_ticket,ss.active_trade_id)==('176137677','saved-trade')
    assert ss.session_realized_points==-6.2 and ss.session_realized_pnl==-18.6
    assert ss.timing_last_quote is None
    connect(m);m.start()
    assert m.state.strategy_states['timing'] is ss
    assert ss.session_realized_points==-6.2
    assert not ss.execution.entries_enabled
    assert m.state.session_started_at=='2026-10-08T15:40:00'

def test_live_resume_reconciles_saved_ticket_without_resetting_baseline(recovery):
    recovery.state.mode='live';recovery.state.account={'login':123,'server':'Test'}
    recovery.state.strategy_states['timing'].session_open_points_baseline=1.2
    recovery._save_runtime()
    m=bm.BotManager();connect(m)
    seen=[]
    m._reconcile_live_snapshot=lambda ss:seen.append(ss.live_ticket)
    m.start()
    assert seen==['176137677',None]
    assert m.state.strategy_states['timing'].session_open_points_baseline==1.2

def test_wrong_account_blocks_resume(recovery):
    recovery.state.mode='live';recovery.state.account={'login':123,'server':'Test'};recovery._save_runtime()
    m=bm.BotManager();connect(m,{'login':456,'server':'Test'})
    with pytest.raises(RuntimeError,match='another MT5 account'):m.start()
    assert not m.state.running and m._restart_pending

def test_failed_reconciliation_keeps_recovery_pending(recovery):
    recovery.state.mode='live';recovery._save_runtime()
    m=bm.BotManager();connect(m)
    def fail(ss):raise RuntimeError('MT5 unavailable')
    m._reconcile_live_snapshot=fail
    with pytest.raises(RuntimeError,match='MT5 unavailable'):m.start()
    assert not m.state.running and m._restart_pending

def test_explicit_stop_clears_resume_but_keeps_configuration(recovery):
    recovery.stop()
    m=bm.BotManager()
    assert not m._restart_pending
    assert m.state.strategies==['timing','timing2']
    assert m.state.strategy_states['timing'].paper_position==0

def test_corrupt_checkpoint_blocks_start(recovery):
    bm.HISTORY_FILE.with_name('strategy_runtime_state.json').write_text('{',encoding='utf-8')
    m=bm.BotManager()
    with pytest.raises(RuntimeError,match='recovery requires review'):m.start()
    assert not m.state.running

def test_resaving_recovered_configuration_preserves_session(recovery):
    m=bm.BotManager();ss=m.state.strategy_states['timing']
    m.configure(strategies=m.state.strategies,symbol=m.state.symbol,timeframe=m.state.timeframe,lot=m.state.lot,mode=m.state.mode,timing_hour=m.state.timing_hour,timing_minute=m.state.timing_minute,strategy_target_points=m.state.strategy_target_points,strategy_settings=m.state.strategy_settings)
    assert m.state.strategy_states['timing'] is ss
    restored=bm.BotManager()
    assert restored._restart_pending
    assert restored.state.strategy_states['timing'].session_realized_points==-6.2

def test_auto_resume_only_previously_running_session(recovery):
    m=bm.BotManager();connect(m)
    assert m.resume_after_server_restart()
    assert m.state.running and not m._restart_pending
    assert m.state.strategy_states['timing'].session_realized_points==-6.2
    assert not m.resume_after_server_restart()

def test_auto_resume_does_not_start_explicitly_stopped_bot(recovery):
    recovery.stop()
    m=bm.BotManager()
    m.start=lambda:pytest.fail('Stopped bot must not start')
    assert not m.resume_after_server_restart()

def test_auto_resume_failure_preserves_checkpoint_and_exposes_error(recovery):
    m=bm.BotManager()
    m.mt5_test=lambda:{'connected':False,'error':'MT5 unavailable'}
    assert not m.resume_after_server_restart()
    assert not m.state.running and m._restart_pending
    assert 'MT5 unavailable' in m.state.error
    assert bm.BotManager()._restart_pending

def test_server_lifespan_attempts_recovery(monkeypatch):
    import asyncio
    from backend.app import main
    calls=[]
    monkeypatch.setattr(main.manager,'resume_after_server_restart',lambda:calls.append('recover'))
    async def exercise():
        async with main.lifespan(main.app):
            assert calls==['recover']
    asyncio.run(exercise())
