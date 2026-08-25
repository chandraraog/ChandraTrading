from fastapi import APIRouter
router=APIRouter(prefix="/api/broker",tags=["broker"])
@router.post('/equiti')
def save_broker(payload:dict):
    # MT5 credentials are intentionally not persisted by this web app.
    # The bot connects to the already logged-in MT5 terminal.
    return {"ok":True,"message":"Broker settings received. Use the logged-in MT5 terminal."}
