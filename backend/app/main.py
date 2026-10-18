import asyncio
from contextlib import asynccontextmanager
from backend.app.bot_manager import manager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from backend.app.runtime_paths import bundle_root
from backend.app.api.trading import router as trading_router
from backend.app.api.broker import router as broker_router
from backend.app.api.auth import router as auth_router
from backend.app.api.license import router as license_router

@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(manager.resume_after_server_restart)
    yield


app=FastAPI(title="Chandra Trading Modular", lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])
app.include_router(trading_router); app.include_router(broker_router); app.include_router(auth_router); app.include_router(license_router)
root=bundle_root()
frontend=root/'frontend'
app.mount('/',StaticFiles(directory=str(frontend),html=True),name='frontend')
