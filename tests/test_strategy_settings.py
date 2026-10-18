from datetime import datetime
from types import SimpleNamespace as NS
import pandas as pd
import pytest
from backend.app import bot_manager as bm


def make(tmp_path,monkeypatch):
    monkeypatch.setattr(bm.LicenseService,"require",lambda self:{"features":["*"],"max_lot":100})
    monkeypatch.setattr(bm,"HISTORY_FILE",tmp_path/"history.json")
    monkeypatch.setattr(bm,"POB_FILE",tmp_path/"pob.json")
    monkeypatch.setattr(bm.BotManager,"_resolve_strategy_markets",lambda self,settings:({n:c["symbol"] for n,c in settings.items()},{n:1.0 for n in settings}))
    return bm.BotManager()


def config(m,settings,**kwargs):
    m.configure(list(settings),"TEST","M1",.01,"paper",strategy_settings=settings,**kwargs)


def test_different_lots_used_for_paper_orders_and_pob_journal(tmp_path,monkeypatch):
    m=make(tmp_path,monkeypatch)
    config(m,{"strategic":{"lot":.02,"timeframe":"M1"},"magical":{"lot":.05,"timeframe":"M5"},"pob":{"lot":.03,"timeframe":"M15"}},pob_high=110,pob_low=100)
    m._record=lambda *a,**k:None
    m._mt5_chart_log_time=lambda:"now"
    for name,size in [("strategic",.02),("magical",.05)]:
        m._paper_open(m.state.strategy_states[name],"BUY",105,sl_override=100)
        assert m.state.strategy_states[name].volume==size
    assert m._pob.data["config"]["lot"]==.03
    assert m._pob.data["config"]["timeframe"]=="M15"


def test_live_timing_uses_own_lot(tmp_path,monkeypatch):
    m=make(tmp_path,monkeypatch)
    config(m,{"timing":{"lot":.07,"timeframe":"M5"}})
    m.state.mode="live"
    calls=[]
    monkeypatch.setattr(bm.core,"open_position",lambda *a,**k:calls.append(a) or True)
    m._place_timing_pending(m.state.strategy_states["timing"],"BUY",110,100)
    assert calls[0][2]==.07


def test_independent_timeframes_and_new_bars(tmp_path,monkeypatch):
    m=make(tmp_path,monkeypatch)
    config(m,{"strategic":{"lot":.02,"timeframe":"M1"},"magical":{"lot":.05,"timeframe":"M5"}})
    cycle=[0];requests=[];processed=[]
    class Engine:
        def __init__(self,symbol,tf,bars):self.tf=tf
        def initialize(self):pass
        def calculate_frame(self):
            requests.append((cycle[0],self.tf))
            minute=cycle[0] if self.tf=="M1" else 0
            return pd.DataFrame([dict(time=datetime(2026,9,24,10,minute),close=105)])
    monkeypatch.setattr(bm,"ChandraTrendEngine",Engine)
    monkeypatch.setattr(bm.core,"signal_from_row",lambda r:NS(buy_condition=False,sell_condition=False,strategic_buy=False,strategic_sell=False,magical_buy=False,magical_sell=False,magical_invalid=False,l100=100,close=105))
    m._update_manual_paper=lambda:None
    m._enforce_strategy_targets=lambda:None
    m._process_strategy_bar=lambda name,ss,row,signal:processed.append((name,row["time"].minute,m._strategy_lot(name))) or []
    def sleep(_):
        cycle[0]+=1
        if cycle[0]==2:m._stop.set()
    monkeypatch.setattr(bm.time,"sleep",sleep)
    m._run()
    assert m.state.error is None
    assert requests==[(0,"M1"),(0,"M5"),(1,"M1"),(1,"M5")]
    assert processed==[("strategic",0,.02),("magical",0,.05),("strategic",1,.02)]


def test_add_new_settings_keeps_running_settings_locked(tmp_path,monkeypatch):
    m=make(tmp_path,monkeypatch)
    settings={"strategic":{"lot":.02,"timeframe":"M5"}}
    config(m,settings);m.state.running=True
    old=m.state.strategy_states["strategic"]
    settings["timing"]={"lot":.07,"timeframe":"M15"}
    config(m,settings)
    assert m.state.strategy_states["strategic"] is old
    assert m._strategy_lot("timing")==.07
    settings["strategic"]={"lot":.04,"timeframe":"M5"}
    with pytest.raises(ValueError,match="locked"):config(m,settings)
    assert m._strategy_lot("strategic")==.02


@pytest.mark.parametrize("setting",[{"lot":0},{"lot":float("nan")},{"timeframe":"BAD"}])
def test_invalid_individual_settings(tmp_path,monkeypatch,setting):
    m=make(tmp_path,monkeypatch)
    with pytest.raises(ValueError):config(m,{"strategic":setting})


def test_strategy_symbols_route_quotes_positions_and_pnl(tmp_path,monkeypatch):
    m=make(tmp_path,monkeypatch)
    config(m,{"strategic":{"symbol":"EURUSD","lot":.02,"timeframe":"M1"},"timing":{"symbol":"XAUUSD","lot":.05,"timeframe":"M5"}})
    m.state.strategy_contract_sizes={"strategic":100000,"timing":100}
    quotes=[];positions=[];orders=[]
    def quote(symbol):quotes.append(symbol);return NS(bid=105,ask=106)
    def get_positions(**kwargs):positions.append(kwargs["symbol"]);return []
    monkeypatch.setattr(bm.core,"mt5",NS(symbol_info_tick=quote,positions_get=get_positions))
    monkeypatch.setattr(bm.core,"open_position",lambda *a,**k:orders.append(a) or True)
    assert m._paper_price("BUY","strategic")==106
    m._live_positions(bm.MAGIC_BY_STRATEGY["timing"])
    assert quotes==["EURUSD"] and positions==["XAUUSD"]
    m.state.mode="live"
    m._place_timing_pending(m.state.strategy_states["timing"],"BUY",106,100)
    assert orders[0][0:3]==("XAUUSD","BUY",.05)
    m.state.mode="paper";m._record=lambda *a,**k:None
    ss=m.state.strategy_states["strategic"]
    ss.paper_position=1;ss.paper_entry_side="BUY";ss.paper_entry_price=1.0;ss.volume=.02
    m._paper_close(ss,1.001)
    assert ss.realized_pnl==pytest.approx(2)


def test_same_timeframe_different_symbols_do_not_share_frames(tmp_path,monkeypatch):
    m=make(tmp_path,monkeypatch)
    config(m,{"strategic":{"symbol":"EURUSD","timeframe":"M1","lot":.02},"magical":{"symbol":"XAUUSD","timeframe":"M1","lot":.05}})
    frames=[];processed=[]
    class Engine:
        def __init__(self,symbol,tf,bars):self.symbol=symbol;self.tf=tf
        def initialize(self):pass
        def calculate_frame(self):
            frames.append((self.symbol,self.tf))
            return pd.DataFrame([dict(time=datetime(2026,9,24,10),close=1 if self.symbol=="EURUSD" else 4000)])
    monkeypatch.setattr(bm,"ChandraTrendEngine",Engine)
    monkeypatch.setattr(bm.core,"signal_from_row",lambda r:NS(buy_condition=False,sell_condition=False,strategic_buy=False,strategic_sell=False,magical_buy=False,magical_sell=False,magical_invalid=False,l100=0,close=r["close"]))
    monkeypatch.setattr(bm.time,"sleep",lambda _:m._stop.set())
    m._update_manual_paper=lambda:None;m._enforce_strategy_targets=lambda:None
    m._process_strategy_bar=lambda name,ss,row,signal:processed.append((name,row["close"])) or []
    m._run()
    assert m.state.error is None
    assert frames==[("EURUSD","M1"),("XAUUSD","M1")]
    assert processed==[("strategic",1),("magical",4000)]
