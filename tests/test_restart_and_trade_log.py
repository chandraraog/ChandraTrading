from pathlib import Path
from types import SimpleNamespace

from backend.app.bot_manager import BotManager


def test_restart_reuses_existing_live_trade_by_ticket():
    p = Path(__file__).parents[1] / "backend" / "app" / "bot_manager.py"
    s = p.read_text(encoding="utf-8")
    assert "_find_open_trade_by_ticket" in s
    assert "duplicate OPEN entry" in s


def test_trade_log_poll_bypasses_browser_cache():
    p = Path(__file__).parents[1] / "frontend" / "index.html"
    s = p.read_text(encoding="utf-8")
    assert "/api/trading/trades?_=${Date.now()}" in s
    assert "await loadTrades()" in s


def test_live_reconciliation_uses_one_snapshot():
    manager = BotManager.__new__(BotManager)
    state = SimpleNamespace(execution=SimpleNamespace(magic_number=1))
    positions = []
    manager._live_positions = lambda magic: positions
    seen = []
    manager._sync_live_snapshot = lambda ss, positions: seen.append(positions)
    manager._reconcile_live_snapshot(state)
    assert seen == [positions]
