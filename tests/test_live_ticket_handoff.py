from types import SimpleNamespace as NS

import pytest

from backend.app import bot_manager as bm


def test_fast_ticket_handoff_and_delayed_history_count_both_losses_once(monkeypatch):
    m = bm.BotManager.__new__(bm.BotManager)
    m.state = NS(mode="live")
    m._strategy_lot = lambda _: .03
    m._find_open_trade_by_ticket = lambda _: None
    records = []
    m._record = lambda *a, **kw: records.append((a, kw))
    ss = bm.StrategyState("timing", NS(magic_number=123, virtual_position=0))
    history = {}
    monkeypatch.setattr(bm.core, "mt5", NS(
        POSITION_TYPE_BUY=0, DEAL_ENTRY_OUT=1,
        history_deals_get=lambda position: history.get(position, [])))
    def position(ticket, side, price, sl):
        return NS(ticket=ticket, type=side, price_open=price, sl=sl,
                  tp=0, volume=.03, profit=0)
    buy = position(168439455, 0, 4158.61, 4156.65)
    sell = position(168439865, 1, 4156.35, 4158.44)
    m._sync_live_snapshot(ss, [buy])
    buy_id = ss.active_trade_id
    # The SELL is visible before the BUY exit arrives in broker history.
    m._sync_live_snapshot(ss, [sell])
    assert ss.live_ticket == str(buy.ticket)
    assert ss.live_exit_pending["ticket"] == str(buy.ticket)
    assert not m._place_timing_pending(ss, "BUY", 4158.6, 4156.65)
    history[buy.ticket] = [NS(entry=1, price=4156.65, profit=-5.88, time=1)]
    m._sync_live_snapshot(ss, [sell])
    sell_id = ss.active_trade_id
    assert sell_id != buy_id
    assert ss.paper_entry_price == 4156.35
    history[sell.ticket] = [NS(entry=1, price=4158.44, profit=-6.27, time=2)]
    m._sync_live_snapshot(ss, [])
    m._sync_live_snapshot(ss, [])
    closes = [(a, kw) for a, kw in records if a[2] == "CLOSE"]
    assert len(closes) == 2
    assert [kw["trade_id"] for a, kw in closes] == [buy_id, sell_id]
    assert [a[1] for a, kw in closes] == ["BUY", "SELL"]
    assert ss.session_realized_points == pytest.approx(-4.05)
    assert ss.session_realized_pnl == pytest.approx(-12.15)
    assert ss.execution.virtual_position == 0


def test_missing_history_preserves_entry_on_empty_snapshot(monkeypatch):
    m = bm.BotManager.__new__(bm.BotManager)
    ss = bm.StrategyState("timing", NS(magic_number=123, virtual_position=1))
    ss.live_ticket, ss.active_trade_id = "10", "trade-10"
    ss.paper_position, ss.paper_entry_side, ss.paper_entry_price = 1, "BUY", 100
    monkeypatch.setattr(bm.core, "mt5", NS(history_deals_get=lambda **kw: None))
    m._sync_live_snapshot(ss, [])
    assert ss.paper_entry_price == 100
    assert ss.live_ticket == "10"
    assert ss.execution.virtual_position == 1
