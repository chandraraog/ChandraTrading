from datetime import datetime
from types import SimpleNamespace as NS
import pytest
from backend.app import bot_manager as bm

@pytest.mark.parametrize("side,close,bid,ask,sl", [("BUY",111,111,112,100),("SELL",99,98,99,110)])
@pytest.mark.parametrize("mode", ["direct", "retest"])
def test_entry_mode(side,close,bid,ask,sl,mode,monkeypatch):
    manager=bm.BotManager.__new__(bm.BotManager)
    manager.state=NS(mode="paper",timing_hour=4,timing_minute=0,timing_entry_mode=mode,entries_enabled=True,mt5_symbol="TEST")
    manager._timing_local_dt=lambda t:t
    manager._timing_reset_if_needed=lambda *a:None
    manager._restore_timing_levels=lambda *a:None
    ss=NS(strategy="timing",timing_high=110,timing_low=100,timing_armed_buy=False,timing_armed_sell=False,timing_arm_time=None,execution=NS(virtual_position=0,entries_enabled=True))
    calls=[]
    manager._place_timing_pending=lambda *args: calls.append(args) or True
    monkeypatch.setattr(bm.core,"mt5",NS(symbol_info_tick=lambda _:NS(bid=bid,ask=ask)))
    row=dict(time=datetime(2026,9,24,4,5),close=close,high=113,low=97)
    result=manager._process_timing_candle(ss,row)
    if mode=="direct":
        assert result is None and not calls  # no closed-candle entry
        ss.timing_last_quote=None
        # An outside first quote establishes a baseline, never an entry.
        assert manager._process_timing_direct_tick(ss) is None
        assert manager._process_timing_direct_tick(ss) is None
        assert not calls
        ss.timing_last_quote=(105,106)
        assert manager._process_timing_direct_tick(ss)
        assert calls[0][1:]==(side,ask if side=="BUY" else bid,sl)
        assert manager._process_timing_direct_tick(ss) is None
        assert len(calls)==1
    else:
        assert result is None and not calls
        assert ss.timing_armed_buy==(side=="BUY")
        assert ss.timing_armed_sell==(side=="SELL")


def test_api_mode_validation():
    from backend.app.api.trading import ConfigIn
    from pydantic import ValidationError
    assert ConfigIn().timing_entry_mode=="retest"
    assert ConfigIn(timing_entry_mode="direct").timing_entry_mode=="direct"
    with pytest.raises(ValidationError):
        ConfigIn(timing_entry_mode="invalid")


@pytest.mark.parametrize("bid,ask,side",[(111,112,"BUY"),(98,99,"SELL")])
def test_live_crossing_and_failed_request_not_repeated(bid,ask,side,monkeypatch):
    m=bm.BotManager.__new__(bm.BotManager)
    m.state=NS(mode="paper",timing_entry_mode="direct",entries_enabled=True,mt5_symbol="TEST")
    ss=NS(strategy="timing",timing_high=110,timing_low=100,timing_last_quote=None,
          execution=NS(virtual_position=0,entries_enabled=True))
    quote=NS(bid=105,ask=106)
    monkeypatch.setattr(bm.core,"mt5",NS(symbol_info_tick=lambda _:quote))
    calls=[]
    m._place_timing_pending=lambda *a:calls.append(a) or False
    assert m._process_timing_direct_tick(ss) is None and not calls
    quote.bid,quote.ask=bid,ask
    assert m._process_timing_direct_tick(ss) is None
    assert calls[0][1]==side
    assert m._process_timing_direct_tick(ss) is None
    assert len(calls)==1


def test_rejected_timing_entry_exposes_diagnostic(monkeypatch):
    m=bm.BotManager.__new__(bm.BotManager)
    m.state=NS(mode="live",mt5_symbol="TEST")
    m._strategy_lot=lambda _: .02
    ss=NS(strategy="timing",execution=NS(magic_number=123))
    def reject(*a,**kw):
        kw["diagnostic"]["message"]="MT5 retcode 10016: Invalid stops"
        return False
    monkeypatch.setattr(bm.core,"open_position",reject)
    assert not m._place_timing_pending(ss,"BUY",110,100)
    assert "10016" in ss.timing_entry_message


@pytest.mark.parametrize("old_position,bid,ask,side", [(1,98,99,"SELL"),(-1,111,112,"BUY")])
@pytest.mark.parametrize("history_pending", [False, True])
def test_stop_exit_requires_fresh_crossing(
        old_position,bid,ask,side,history_pending,monkeypatch):
    m=bm.BotManager.__new__(bm.BotManager)
    m.state=NS(mode="paper",timing_entry_mode="direct",entries_enabled=True,mt5_symbol="TEST")
    ss=NS(strategy="timing",timing_high=110,timing_low=100,
          timing_last_quote=(105,106),live_exit_pending=None,
          execution=NS(virtual_position=old_position,entries_enabled=True))
    quote=NS(bid=bid,ask=ask)
    monkeypatch.setattr(bm.core,"mt5",NS(symbol_info_tick=lambda _:quote))
    calls=[]
    m._place_timing_pending=lambda *a: calls.append(a) or True
    # Price has crossed the opposite level, but the old position is still tracked.
    assert m._process_timing_direct_tick(ss) is None
    assert not calls and ss.timing_last_quote == (bid,ask)
    ss.execution.virtual_position=0
    if history_pending:
        ss.live_exit_pending={"ticket":"old"}
        assert m._process_timing_direct_tick(ss) is None
        assert not calls and ss.timing_last_quote == (bid,ask)
        ss.live_exit_pending=None
    # Remaining outside after SL must not enter. Return inside, then recross.
    assert m._process_timing_direct_tick(ss) is None
    assert not calls
    quote.bid,quote.ask=105,106
    assert m._process_timing_direct_tick(ss) is None
    quote.bid,quote.ask=bid,ask
    assert m._process_timing_direct_tick(ss)
    assert calls[0][1]==side
    assert m._process_timing_direct_tick(ss) is None
    assert len(calls)==1


@pytest.mark.parametrize("old_position,bid,ask,side", [(-1,111,112,"BUY"),(1,98,99,"SELL")])
@pytest.mark.parametrize("closed", [True, False])
def test_live_crossing_refreshes_stale_position_before_entry(
        old_position,bid,ask,side,closed,monkeypatch):
    m=bm.BotManager.__new__(bm.BotManager)
    m.state=NS(mode="live",timing_entry_mode="direct",entries_enabled=True,mt5_symbol="TEST")
    ss=bm.StrategyState("timing",NS(virtual_position=old_position,entries_enabled=True))
    ss.timing_high,ss.timing_low=110,100
    ss.timing_last_quote=(105,106)
    calls=[]
    monkeypatch.setattr(bm.core,"mt5",NS(symbol_info_tick=lambda _:NS(bid=bid,ask=ask)))
    def reconcile(state):
        calls.append("reconcile")
        if closed:state.execution.virtual_position=0
    m._reconcile_live_snapshot=reconcile
    m._place_timing_pending=lambda *a: calls.append(a[1]) or True
    result=m._process_timing_direct_tick(ss)
    assert calls==(["reconcile",side] if closed else ["reconcile"])
    assert bool(result)==closed
    # No queued entry if the broker only confirms closure on a later poll.
    ss.execution.virtual_position=0
    assert m._process_timing_direct_tick(ss) is None
    assert calls==(["reconcile",side] if closed else ["reconcile"])
    if not closed:assert "still open" in ss.timing_entry_message
