from datetime import datetime, timezone
from types import SimpleNamespace as NS
import pandas as pd
from backend.app.services.pob_execution import POBExecution
from test_strategy_settings import make, config


def test_candle_wait_capture_restart_and_next_day(tmp_path):
    now=[datetime(2026,10,3,10,4,tzinfo=timezone.utc)]
    calls=[]
    def history(symbol,tf,*args):
        calls.append((symbol,tf))
        return pd.DataFrame([dict(time=datetime(2026,10,3,10),high=110,low=100)])
    core=NS(mt5=NS(copy_rates_from_pos=lambda *a:[{"time":now[0].timestamp()}]),
        timeframe_to_mt5=lambda tf:tf,timeframe_minutes=lambda tf:5,get_rates_range=history)
    p=POBExecution(tmp_path/"pob.json",core)
    p.configure(dict(symbol="XAGUSD",timeframe="M5",lot=.01,mode="paper",pob_high=None,pob_low=None,pob_sl_mode="levels",pob_entry_mode="direct",pob_level_source="candle",pob_candle_time="10:00"))
    p.symbol="XAGUSD.v"
    assert not p._prepare_candle_range() and not calls
    assert p.strategy.levels is None
    now[0]=datetime(2026,10,3,10,5,tzinfo=timezone.utc)
    assert p._prepare_candle_range()
    assert calls==[("XAGUSD.v","M5")]
    levels=p.strategy.levels
    assert levels["high"]==110 and levels["low"]==100
    assert levels["buy"]["stop"]==levels["sell"]["entry"]
    restored=POBExecution(p.path,core)
    assert restored.strategy.levels==levels
    now[0]=datetime(2026,10,4,9,tzinfo=timezone.utc)
    p.data["position"]={"ticket":1}
    assert not p._prepare_candle_range() and p.strategy.levels==levels
    p.data["position"]=None
    assert not p._prepare_candle_range() and p.strategy.levels is None


def test_pob_candle_config_does_not_change_timing_instances(tmp_path,monkeypatch):
    m=make(tmp_path,monkeypatch)
    config(m,{"timing":{},"timing2":{"timing_hour":12,"timing_end_time":"18:00"},"pob":{}},
        timing_hour=9,timing_minute=30,pob_level_source="candle",pob_candle_time="10:00")
    assert m.state.timing_hour==9 and m.state.timing_minute==30
    assert m.state.strategy_settings["timing2"]["timing_hour"]==12
    assert m.state.pob_high is None and m._pob.strategy.levels is None
    assert m.state.pob_candle_time=="10:00"
