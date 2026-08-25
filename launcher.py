"""Chandra Trading Windows application launcher."""

from __future__ import annotations

import sys

import uvicorn

from backend.app.licensing.license_service import LicenseService
from backend.app.main import app


def main() -> int:
    try:
        # Validate the license before starting the application.
        LicenseService().require()

    except Exception:
        # LicenseService already prints the clean user-facing
        # licensing message. Do not expose a Python traceback.
        return 1

    # License is valid. Start the existing FastAPI application.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        reload=False,
        access_log=False,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())