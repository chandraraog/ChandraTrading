from pathlib import Path


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
