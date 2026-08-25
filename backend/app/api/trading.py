from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from backend.app.bot_manager import manager

router=APIRouter(prefix="/api/trading", tags=["trading"])

class ConfigIn(BaseModel):
    strategies: List[str]=[]
    symbol: str="XAUUSD"
    timeframe: str="M1"
    lot: float=0.01
    mode: str="paper"
    timing_hour: int=4
    timing_minute: int=0
    timing_end_time: Optional[str]="08:00"
class BacktestIn(BaseModel):
    from_date: str; to_date: str; symbol: str="XAUUSD"; timeframe: str="M1"; lot: float=0.01
    max_trades_per_day: int=0; daily_target_points: float=0.0; timing_hour: int=4; timing_minute: int=0; timing_end_time: Optional[str]=None
class ManualIn(BaseModel):
    side: str; lot: float=0.01; sl: Optional[float]=None; target: Optional[float]=None

@router.post('/configure')
def configure(p:ConfigIn):
    try: manager.configure(**p.model_dump())
    except Exception as e: raise HTTPException(422,str(e))
    return {"ok":True,"message":"Configuration saved."}
@router.post('/mt5-test')
def mt5_test(): return manager.mt5_test()
@router.post('/start')
def start():
    try: manager.start()
    except Exception as e: raise HTTPException(400,str(e))
    return {"ok":True}
@router.post('/stop')
def stop(): manager.stop(); return {"ok":True}
@router.post('/stop-trading')
def stop_trading(): manager.stop_trading(); return {"ok":True}
@router.post('/resume-trading')
def resume():
    try: manager.resume_trading()
    except Exception as e: raise HTTPException(400,str(e))
    return {"ok":True}
@router.post('/close/{strategy}')
def close(strategy:str):
    try: return manager.close_strategy(strategy)
    except Exception as e: raise HTTPException(400,str(e))
@router.post('/close-all')
def close_all(): return manager.close_all()
@router.get('/status')
def status(): return manager.status_payload()
@router.get('/trades')
def trades():
    # Stable response shape expected by the frontend trade-log renderer.
    return {"trades": manager.state.trades}
@router.post('/manual/order')
def manual(p:ManualIn):
    try: return manager.manual_order(p.side,p.lot,p.sl,p.target)
    except Exception as e: raise HTTPException(400,str(e))
@router.post('/manual/close')
def manual_close(): return manager.close_manual()
@router.post('/backtest/strategic')
def strategic_bt(p:BacktestIn): return manager.strategic_backtest(p.from_date,p.to_date,p.lot,p.max_trades_per_day,p.daily_target_points,p.symbol,p.timeframe)
@router.post('/backtest/timing')
def timing_bt(p:BacktestIn): return manager.timing_backtest(p.from_date,p.to_date,p.lot,p.max_trades_per_day,p.daily_target_points,p.symbol,p.timeframe,p.timing_hour,p.timing_minute,p.timing_end_time)
