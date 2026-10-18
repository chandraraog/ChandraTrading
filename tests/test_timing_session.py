from datetime import datetime, timezone
from types import SimpleNamespace as NS
import pytest
from backend.app import bot_manager as bm


def setup(monkeypatch, mode="live"):
    m=bm.BotManager.__new__(bm.BotManager)
    m.state=NS(mode=mode, timing_hour=4,timing_minute=0,timing_end_time="08:00",mt5_symbol="TEST",timing_entry_mode="direct",entries_enabled=True)
    ss=bm.StrategyState(strategy="timing",execution=NS(magic_number=123,virtual_position=1,entries_enabled=True))
    ss.timing_high,ss.timing_low=110,100
    ss.timing_armed_buy=True
    ss.paper_position=1
    broker=NS(hour=7,minute=59,missing=False,fail=False,positions=[NS(magic=123,ticket=1),NS(magic=999,ticket=2)],closed=[])
    def rates(*args):
        if broker.missing:return None
        return [{"time":int(datetime(2026,9,24,broker.hour,broker.minute,tzinfo=timezone.utc).timestamp())}]
    def close(p,*args):
        broker.closed.append(p.ticket)
        if not broker.fail:broker.positions.remove(p)
        return not broker.fail
    monkeypatch.setattr(bm.core,"mt5",NS(copy_rates_from_pos=rates,positions_get=lambda **kw:broker.positions))
    monkeypatch.setattr(bm.core,"timeframe_to_mt5",lambda tf:tf)
    monkeypatch.setattr(bm.core,"ensure_symbol",lambda s:NS())
    monkeypatch.setattr(bm.core,"close_position",close)
    m._record_live_close=lambda ss:None
    m._sync_live_snapshot=lambda ss:None
    return m,ss,broker


def test_cutoff_closes_only_timing_and_blocks_all_entry_paths(monkeypatch):
    m,ss,b=setup(monkeypatch)
    m._enforce_timing_session(ss)
    assert not ss.timing_session_blocked and not b.closed
    b.hour,b.minute=8,0
    m._enforce_timing_session(ss)
    assert b.closed==[1] and [p.ticket for p in b.positions]==[2]
    assert ss.timing_session_blocked and not ss.timing_close_pending
    assert not ss.timing_armed_buy
    ss.execution.virtual_position=0
    assert m._process_timing_direct_tick(ss) is None
    m.state.timing_entry_mode="retest"
    ss.timing_armed_buy=True
    assert m._process_timing_retest_tick(ss) is None
    m._enforce_timing_session(ss)
    assert b.closed==[1]


def test_rejected_close_retries_and_stays_blocked(monkeypatch):
    m,ss,b=setup(monkeypatch)
    b.hour,b.minute,b.fail=8,0,True
    m._enforce_timing_session(ss)
    assert ss.timing_close_pending and ss.timing_session_blocked
    assert "pending" in ss.timing_session_message
    b.fail=False
    m._enforce_timing_session(ss)
    assert not ss.timing_close_pending and b.closed==[1,1]


def test_missing_clock_blocks_entries_and_unknown_positions_retry(monkeypatch):
    m,ss,b=setup(monkeypatch)
    b.missing=True
    m._enforce_timing_session(ss)
    assert ss.timing_session_blocked and not b.closed
    b.missing=False;b.hour,b.minute=8,0;b.positions=None
    m._enforce_timing_session(ss)
    assert ss.timing_close_pending and ss.timing_session_blocked


def test_paper_close_uses_quote_and_end_time_reason(monkeypatch):
    m,ss,b=setup(monkeypatch,"paper")
    b.hour,b.minute=8,0
    calls=[]
    m._paper_price=lambda side, strategy=None:105
    def close(state,price,reason):
        calls.append((price,reason));state.paper_position=0
    m._paper_close=close
    m._enforce_timing_session(ss)
    assert calls==[(105,"TIMING END TIME")]
    assert ss.execution.virtual_position==0 and not b.closed
    m._enforce_timing_session(ss)
    assert len(calls)==1


def test_next_session_does_not_override_manual_entry_pause(monkeypatch):
    m,ss,b=setup(monkeypatch)
    b.hour,b.minute=3,59
    m._enforce_timing_session(ss)
    assert ss.timing_session_blocked
    ss.execution.entries_enabled=False
    b.hour,b.minute=4,0
    m._enforce_timing_session(ss)
    assert not ss.timing_session_blocked and not ss.execution.entries_enabled


@pytest.mark.parametrize("end",["03:59","04:00","25:00","08:60","invalid"])
def test_invalid_end_rejected_before_state_mutation(end):
    m=bm.BotManager.__new__(bm.BotManager)
    with pytest.raises(ValueError,match="Timing end time"):
        m.configure(["timing"],"TEST","M1",.01,"paper",timing_hour=4,timing_minute=0,timing_end_time=end)
