from types import SimpleNamespace as NS
from datetime import datetime, timezone
import pytest
from test_strategy_settings import make, config
from backend.app import bot_manager as bm


def test_two_timing_instances_route_entries_and_lock_settings(tmp_path, monkeypatch):
    m=make(tmp_path,monkeypatch)
    settings={"timing":{"symbol":"XAUUSD","lot":.03},
              "timing2":{"symbol":"XAGUSD","lot":.05,"timeframe":"M5",
                         "timing_hour":10,"timing_minute":30,"timing_end_time":"18:00","timing_entry_mode":"direct"}}
    config(m,settings,timing_entry_mode="direct")
    a,b=[m.state.strategy_states[n] for n in ("timing","timing2")]
    assert a.execution.magic_number != b.execution.magic_number
    assert m._timing_option(b,"timing_hour")==10
    calls=[]
    m._place_timing_pending=lambda ss,side,price,sl: calls.append((ss.strategy,m._strategy_symbol(ss.strategy),m._strategy_lot(ss.strategy))) or True
    monkeypatch.setattr(bm.core,"mt5",NS(symbol_info_tick=lambda symbol:NS(bid=111,ask=112)))
    for ss in (a,b):
        ss.timing_high,ss.timing_low,ss.timing_last_quote=110,100,(105,106)
        assert m._process_timing_direct_tick(ss)
    assert calls==[("timing","XAUUSD",.03),("timing2","XAGUSD",.05)]
    m.state.running=True
    changed={**settings,"timing2":{**settings["timing2"],"timing_end_time":"19:00"}}
    with pytest.raises(ValueError,match="locked"):
        config(m,changed,timing_entry_mode="direct")
    assert m.state.strategy_states["timing"] is a


def test_timing2_cutoff_does_not_close_timing1(tmp_path,monkeypatch):
    m=make(tmp_path,monkeypatch)
    config(m,{"timing":{"symbol":"XAUUSD"},"timing2":{"symbol":"XAGUSD","timing_hour":4,"timing_minute":0,"timing_end_time":"08:00"}})
    a,b=[m.state.strategy_states[n] for n in ("timing","timing2")]
    a.paper_position=b.paper_position=1
    monkeypatch.setattr(bm.core,"mt5",NS(copy_rates_from_pos=lambda *args:[{"time":int(datetime(2026,10,2,8,tzinfo=timezone.utc).timestamp())}]))
    monkeypatch.setattr(bm.core,"timeframe_to_mt5",lambda tf:tf)
    m._paper_price=lambda *args:100
    closed=[]
    m._paper_close=lambda ss,*args:closed.append(ss.strategy)
    m._enforce_timing_session(b)
    assert closed==["timing2"] and a.paper_position==1
    assert b.timing_session_blocked


def test_timing2_api_retains_options():
    from backend.app.api.trading import ConfigIn
    payload=ConfigIn(strategies=["timing2"],strategy_settings={"timing2":{"symbol":"XAGUSD","timing_hour":12,"timing_entry_mode":"direct"}}).model_dump()
    assert payload["strategy_settings"]["timing2"]["timing_hour"]==12
