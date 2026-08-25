from fastapi import APIRouter
from backend.app.licensing.license_service import LicenseService

router = APIRouter(prefix="/api/license", tags=["license"])
_service = LicenseService()

@router.get('/status')
def status():
    return _service.status()
