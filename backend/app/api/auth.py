from fastapi import APIRouter
router=APIRouter(prefix="/api/auth",tags=["auth"])
@router.post('/logout')
def logout(): return {"ok":True}
@router.post('/google')
def google(): return {"ok":True}
