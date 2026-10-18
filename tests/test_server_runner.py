import asyncio
from unittest.mock import AsyncMock, Mock
import pytest
from backend.app import server_runner as sr


@pytest.mark.parametrize("ready", [True, False])
def test_browser_opens_only_when_server_started(monkeypatch, ready):
    server = sr.DashboardServer(sr.uvicorn.Config("unused:app"))
    async def startup(*args, **kwargs):
        server.started = ready
    monkeypatch.setattr(sr.uvicorn.Server, "startup", startup)
    browser = Mock(return_value=True)
    monkeypatch.setattr(sr.webbrowser, "open", browser)
    asyncio.run(server.startup())
    assert browser.call_count == int(ready)
    if ready:
        browser.assert_called_once_with(sr.DASHBOARD_URL, new=2)


def test_browser_failure_does_not_stop_server(monkeypatch):
    server = sr.DashboardServer(sr.uvicorn.Config("unused:app"))
    server.started = True
    monkeypatch.setattr(sr.uvicorn.Server, "startup", AsyncMock())
    monkeypatch.setattr(sr.webbrowser, "open", Mock(side_effect=OSError("No browser")))
    asyncio.run(server.startup())
    assert server.started
