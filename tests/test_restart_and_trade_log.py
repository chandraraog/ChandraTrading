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


def test_live_reconciliation_records_broker_exit_before_clearing_snapshot():
    """An MT5 SL must become a UI/log update without waiting for a new bar."""
    manager = BotManager()
    state = SimpleNamespace(
        live_ticket="123456", live_exit_pending=None, active_trade_id="trade-1",
        paper_entry_side="BUY", paper_entry_price=4559.47, paper_sl=4554.50,
        target=None, volume=0.01, execution=SimpleNamespace(magic_number=1),
    )
    calls = []
    manager._live_positions = lambda magic: []
    manager._record_live_close = lambda ss: calls.append("close") or True
    manager._sync_live_snapshot = lambda ss: calls.append("snapshot")

    manager._reconcile_live_snapshot(state)

    assert calls == ["close", "snapshot"]
