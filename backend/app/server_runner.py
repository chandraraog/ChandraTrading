"""Start the local dashboard and open it after Uvicorn is listening."""
import asyncio
import logging
import webbrowser

import uvicorn

DASHBOARD_URL = "http://127.0.0.1:8000/"


class DashboardServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            try:
                opened = await asyncio.to_thread(webbrowser.open, DASHBOARD_URL, new=2)
                if not opened:
                    logging.getLogger("uvicorn.error").warning("Open the dashboard manually: %s", DASHBOARD_URL)
            except Exception:
                logging.getLogger("uvicorn.error").warning("Could not open browser. Dashboard: %s", DASHBOARD_URL)


def run_dashboard(app):
    DashboardServer(uvicorn.Config(app, host="127.0.0.1", port=8000,
                                   reload=False, access_log=False)).run()
