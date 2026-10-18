from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from backend.app.strategies.pob import POBStrategy, calculate_levels
from backend.app.services.pob_execution import POBExecution, MAGIC, half_volume, valid_volume


def row(n=0, high=114, low=111, close=113):
    return dict(time=datetime(2026, 9, 23, 10) + timedelta(minutes=n), high=high, low=low, close=close)


class Broker:
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    POSITION_TYPE_BUY = ORDER_TYPE_BUY = DEAL_ENTRY_IN = ORDER_FILLING_FOK = 0
    ORDER_TYPE_SELL = DEAL_ENTRY_OUT = ORDER_FILLING_IOC = ORDER_TIME_GTC = TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_STATE_FILLED = 4

    def __init__(self):
        self.info = NS(volume_min=.01, volume_max=100, volume_step=.01, point=.01,
                       trade_tick_size=.01, digits=2, trade_contract_size=100)
        self.tick = NS(bid=112.36, ask=112.36, time=datetime(2026, 9, 23, 10, 1, tzinfo=timezone.utc).timestamp())
        self.positions = []
        self.deals = []
        self.requests = []
        self.next_volume = None
        self.unknown = False
        self.reject = None
        self.delayed_deals = False
        self.hidden = []
        self.margin_mode = 2

    def account_info(self):
        return NS(login=123, server="Test", margin_mode=self.margin_mode)

    def symbol_info_tick(self, symbol):
        return self.tick

    def positions_get(self, **kwargs):
        return self.positions

    def history_deals_get(self, *args, **kwargs):
        return self.deals

    def history_orders_get(self, **kwargs):
        return [NS(state=4)]

    def price(self, price):
        self.tick.bid = self.tick.ask = price
        self.tick.time += 1

    def order_send(self, request):
        self.requests.append(request.copy())
        if self.reject:
            return NS(retcode=self.reject, comment="Rejected", volume=0, order=0, deal=0)
        if self.unknown:
            return None
        if request["action"] == self.TRADE_ACTION_SLTP:
            self.positions[0].sl = request["sl"]
            return NS(retcode=10009, volume=0, order=0, deal=0)
        vol = self.next_volume if self.next_volume is not None else request["volume"]
        self.next_volume = None
        number = len(self.requests) + 100
        if "position" in request:
            p = self.positions[0]
            profit = (request["price"] - p.price_open) * (1 if p.type == 0 else -1) * vol * 100
            p.volume = round(p.volume - vol, 10)
            if p.volume == 0:
                self.positions = []
            entry = 1
        else:
            entry, profit = 0, 0
            self.positions = [NS(ticket=7, identifier=7, magic=MAGIC, price_open=request["price"],
                                 volume=vol, sl=request["sl"], tp=request["tp"], type=request["type"])]
        deal = NS(ticket=number, order=number, entry=entry, position_id=7, symbol="TEST", magic=MAGIC,
                  volume=vol, price=request["price"], profit=profit, comment=request["comment"], time_msc=number)
        (self.hidden if self.delayed_deals else self.deals).append(deal)
        return NS(retcode=10009 if vol == request["volume"] else 10010, order=number, deal=number, volume=vol)


def execution(tmp_path, mode="paper", lot=.04, side="BUY", sl_mode="default"):
    broker = Broker()
    core = NS(mt5=broker, ensure_symbol=lambda symbol: broker.info, supported_filling_modes=lambda info: [0, 1])
    e = POBExecution(tmp_path / "pob.json", core)
    e.configure(dict(symbol="TEST", mode=mode, lot=lot, pob_high=110, pob_low=100,
                     pob_entry_mode="retest", pob_sl_mode=sl_mode))
    e.bind("TEST")
    levels = e.strategy.levels[side.lower()]
    broker.price(levels["entry"])
    e.enter(dict(side=side, pob_levels=levels), broker.tick, broker.info)
    if mode == "live":
        e.reconcile(broker.info)
    return e, broker, levels


def test_pine_formulas():
    levels = calculate_levels(110, 100)
    assert levels["buy"] == pytest.approx(dict(entry=112.36, stop=107.64, t1=118.76, t2=124.6, t3=133.6))
    assert levels["sell"] == pytest.approx(dict(entry=97.64, stop=102.36, t1=91.24, t2=85.84, t3=76.4))


@pytest.mark.parametrize("high,low", [(100, 100), (90, 100), (100, 0), (float("nan"), 100), (110, float("inf")), (True, .5), (1000, 1)])
def test_invalid_levels(high, low):
    with pytest.raises(ValueError):
        calculate_levels(high, low)


@pytest.mark.parametrize("side,close", [("BUY", 113), ("SELL", 97)])
def test_retest_requires_closed_breakout_then_later_touch(side, close):
    s = POBStrategy(110, 100)
    assert s.decide(row(close=close)) is None
    entry = s.levels[side.lower()]["entry"]
    assert s.retest(row()["time"], entry) is None
    assert s.retest(row(1)["time"], entry)["side"] == side
    assert s.retest(row(1)["time"], entry) is None


def test_candle_fallback_and_invalid_retest():
    s = POBStrategy(110, 100)
    s.decide(row())
    assert s.decide(row()) is None
    assert s.decide(row(1))["reason"] == "POB candle-close retest fallback"
    s.decide(row(2))
    assert s.retest(row(3)["time"], 125) is None
    assert s.armed is None
    s.decide(row(4))
    assert s.decide(row(5, high=115, low=106)) is None


@pytest.mark.parametrize("side,before,after", [("BUY", 112, 112.36), ("SELL", 98, 97.64)])
def test_direct_requires_fresh_crossing(side, before, after):
    s = POBStrategy(110, 100)
    assert s.crossing(row()["time"], before, before) is None
    assert s.crossing(row(1)["time"], after, after)["side"] == side
    assert s.crossing(row(2)["time"], after, after) is None


@pytest.mark.parametrize("step,volume,expected", [(.01, .02, .01), (.01, .01, None), (.01, .03, None), (.001, .03, .015), (.25, 1.5, .75)])
def test_fractional_broker_volume(step, volume, expected):
    info = NS(volume_min=step, volume_max=100, volume_step=step)
    assert valid_volume(volume, info)
    assert half_volume(volume, info) == expected


@pytest.mark.parametrize("mode", ["paper", "live"])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("sl_mode", ["default", "levels"])
def test_t2_half_restart_and_t3(tmp_path, mode, side, sl_mode):
    e, b, levels = execution(tmp_path, mode=mode, side=side, sl_mode=sl_mode)
    b.price(levels["t2"])
    e.poll(None, False)
    if mode == "live":
        assert not e.data["position"]["t2_done"]
        e.poll(None, False)
        e.poll(None, False)
    p = e.data["position"]
    assert p["volume"] == .02 and p["t2_done"]
    assert p["sl"] == levels["entry"]
    restored = POBExecution(e.path, e.core)
    restored.bind("TEST")
    restored.poll(None, False)
    assert restored.data["position"]["volume"] == .02
    b.price(levels["t3"])
    restored.poll(None, False)
    if mode == "live":
        restored.poll(None, False)
    assert restored.data["position"] is None
    exits = [x for x in restored.data["events"] if x["action"] != "OPEN"]
    assert all(x["symbol"] == "TEST" and x["timeframe"] == restored.data["config"].get("timeframe") for x in exits)
    assert [x["volume"] for x in exits] == [.02, .02]
    assert restored.status()["target_completed"]
    paused = POBExecution(e.path, e.core)
    paused.bind("TEST")
    paused.enter(None, b.tick, b.info)
    assert paused.data["position"] is None
    assert paused.status()["target_completed"]


@pytest.mark.parametrize("mode", ["paper", "live"])
@pytest.mark.parametrize("lot", [.01, .03])
def test_unsplittable_keeps_full_volume_and_moves_sl(tmp_path, mode, lot):
    e, b, levels = execution(tmp_path, mode=mode, lot=lot)
    b.price(levels["t2"])
    e.poll(None, False)
    e.poll(None, False)
    p = e.data["position"]
    assert p["volume"] == lot and p["sl"] == levels["entry"]
    assert not [x for x in e.data["events"] if x["action"] != "OPEN"]
    if mode == "live":
        assert b.positions[0].sl == pytest.approx(levels["entry"])
    b.price(levels["entry"])
    e.poll(None, False)
    if mode == "live":
        e.poll(None, False)
    assert e.data["position"] is None


def test_t2_partial_fill_retries_only_unfilled_half_after_restart(tmp_path):
    e, b, levels = execution(tmp_path, mode="live")
    b.next_volume = .01
    b.price(levels["t2"])
    e.poll(None, False)
    assert e.data["position"]["sl"] == levels["stop"]
    e = POBExecution(e.path, e.core)
    e.bind("TEST")
    e.poll(None, False)
    assert b.requests[-1]["volume"] == .01
    assert e.data["position"]["t2_filled"] == .01
    e.poll(None, False)
    assert e.data["position"]["t2_filled"] == .02
    assert e.data["position"]["volume"] == .02
    assert e.data["position"]["sl"] == levels["entry"]


def test_delayed_history_does_not_double_close_or_advance_t2(tmp_path):
    e, b, levels = execution(tmp_path, mode="live")
    b.delayed_deals = True
    b.price(levels["t2"])
    e.poll(None, False)
    count = len(b.requests)
    for _ in range(3):
        e.poll(None, False)
    assert len(b.requests) == count
    assert not e.data["position"]["t2_done"]
    b.deals.extend(b.hidden)
    b.hidden = []
    e.poll(None, False)
    assert e.data["position"]["t2_done"]


def test_unknown_close_survives_restart_without_duplicate(tmp_path):
    e, b, levels = execution(tmp_path, mode="live")
    b.unknown = True
    b.price(levels["t2"])
    e.poll(None, False)
    e = POBExecution(e.path, e.core)
    e.bind("TEST")
    e.poll(None, False)
    assert len(b.requests) == 2
    assert e.data["pending"] and not e.data["position"]["t2_done"]


def test_partial_entry_uses_actual_filled_volume(tmp_path):
    e, b, levels = execution(tmp_path)
    e.data["position"] = None
    e.data["config"]["mode"] = "live"
    e.bind("TEST")
    b.next_volume = .02
    e.enter(dict(side="BUY", pob_levels=levels), b.tick, b.info)
    e.reconcile(b.info)
    assert e.data["position"]["initial_volume"] == .02
    assert e.data["position"]["half"] == .01


def test_no_entries_when_paused_but_targets_still_managed(tmp_path):
    e, b, levels = execution(tmp_path)
    b.price(levels["t2"])
    e.poll(row(), False)
    assert e.data["position"]["volume"] == .02
    b.price(levels["t3"])
    e.poll(row(1), False)
    assert e.data["position"] is None
    e.poll(row(2), False)
    assert e.strategy.armed is None


def test_live_netting_and_account_switch_rejected(tmp_path):
    e, b, levels = execution(tmp_path, mode="live")
    b.margin_mode = 0
    with pytest.raises(ValueError, match="hedging"):
        e.bind("TEST")
    b.margin_mode = 2
    with pytest.raises(ValueError, match="another account"):
        e.bind("OTHER")


def test_configure_pob_api_and_restart(tmp_path, monkeypatch):
    from backend.app import bot_manager as bm
    from backend.app.api.trading import ConfigIn
    monkeypatch.setattr(bm, "POB_FILE", tmp_path / "state.json")
    monkeypatch.setattr(bm, "HISTORY_FILE", tmp_path / "trades.json")
    monkeypatch.setattr(bm.LicenseService, "require", lambda self: {"features": ["*"], "max_lot": 100})
    manager = bm.BotManager()
    cfg = ConfigIn(strategies=["pob", "timing"], pob_high=110, pob_low=100, pob_entry_mode="direct")
    manager.configure(**cfg.model_dump())
    restored = bm.BotManager()
    assert restored.state.strategies == ["pob", "timing"]
    assert restored.state.pob_entry_mode == "direct"
    assert restored.status_payload()["pob"]["levels"]["buy"]["t2"] == pytest.approx(124.6)
    assert bm.MAGIC_BY_STRATEGY["pob"] != bm.MAGIC_BY_STRATEGY["timing"]


def test_unknown_entry_never_repeats_and_recovers_later_fill(tmp_path):
    e, b, levels = execution(tmp_path)
    e.data["position"] = None
    e.data["config"]["mode"] = "live"
    e.bind("TEST")
    b.unknown = True
    e.enter(dict(side="BUY", pob_levels=levels), b.tick, b.info)
    pending_request = b.requests[0].copy()
    e = POBExecution(e.path, e.core)
    e.bind("TEST")
    for _ in range(3):
        e.poll(row(), True)
    assert len(b.requests) == 1 and e.data["pending"]
    b.unknown = False
    # Simulate broker completion of the original request after a timeout.
    b.order_send(pending_request)
    e.reconcile(b.info)
    assert e.data["position"]["volume"] == .04 and e.data["pending"] is None


def test_entry_and_stop_between_polls_recovered_from_history(tmp_path):
    e, b, levels = execution(tmp_path)
    e.data["position"] = None
    e.data["config"]["mode"] = "live"
    e.bind("TEST")
    e.enter(dict(side="BUY", pob_levels=levels), b.tick, b.info)
    b.price(levels["stop"])
    b.order_send(dict(action=1, position=7, type=1, volume=.04, price=b.tick.bid, comment="broker sl"))
    e.reconcile(b.info)
    assert e.data["position"] is None and e.data["pending"] is None
    assert e.data["events"][-1]["action"] == "CLOSE"


def test_partial_manual_exit_finishes_remaining_without_duplicate(tmp_path):
    e, b, levels = execution(tmp_path, mode="live")
    b.next_volume = .01
    e.close()
    e = POBExecution(e.path, e.core)
    e.bind("TEST")
    e.poll(None, False)
    assert b.requests[-1]["volume"] == .03
    e.poll(None, False)
    assert e.data["position"] is None
    assert sum(x["volume"] for x in e.data["events"] if x["action"] != "OPEN") == .04


def test_rejected_t2_does_not_move_stop_or_report_fill(tmp_path):
    e, b, levels = execution(tmp_path, mode="live")
    b.reject = 10014
    b.price(levels["t2"])
    e.poll(None, False)
    p = e.data["position"]
    assert p["volume"] == .04 and not p["t2_done"]
    assert p["sl"] == levels["stop"] and "rejected" in e.status()["error"]


def test_rejected_sl_keeps_desired_stop_and_retries(tmp_path):
    e, b, levels = execution(tmp_path, mode="live", lot=.01)
    b.reject = 10016
    b.price(levels["t2"])
    e.poll(None, False)
    assert e.data["position"]["sl"] == levels["entry"]
    assert b.positions[0].sl == levels["stop"]
    b.reject = None
    e.poll(None, False)
    e.poll(None, False)
    assert b.positions[0].sl == levels["entry"]


def test_broker_snapshot_failure_cannot_be_treated_as_flat(tmp_path):
    e, b, levels = execution(tmp_path, mode="live")
    b.positions_get = lambda **kwargs: None
    with pytest.raises(RuntimeError, match="positions unavailable"):
        e.poll(row(), True)
    assert len(b.requests) == 1 and e.active


def test_corrupt_journal_cannot_reset_exposure(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text('{"position":')
    with pytest.raises(ValueError):
        POBExecution(path, None)


def test_saved_paper_t2_is_atomic(tmp_path):
    e, b, levels = execution(tmp_path)
    b.price(levels["t2"])
    e.close("POB T2", .02)
    recovered = POBExecution(e.path, e.core)
    assert recovered.data["position"]["t2_filled"] == .02
    assert recovered.data["position"]["t2_done"]
    assert recovered.data["position"]["volume"] == .02


def test_manager_loop_routes_pob_without_legacy_execution(tmp_path, monkeypatch):
    from backend.app import bot_manager as bm
    monkeypatch.setattr(bm, "POB_FILE", tmp_path / "pob.json")
    monkeypatch.setattr(bm, "HISTORY_FILE", tmp_path / "history.json")
    b = Broker()
    b.shutdown = lambda: None
    monkeypatch.setattr(bm.core, "mt5", b)
    monkeypatch.setattr(bm.core, "ensure_symbol", lambda symbol: b.info)
    monkeypatch.setattr(bm.LicenseService, "require", lambda self: {"features": ["*"], "max_lot": 100})
    manager = bm.BotManager()
    manager.configure(["pob"], "TEST", "M1", .04, "paper", pob_high=110, pob_low=100)
    manager.state.mt5_symbol = "TEST"
    manager._pob.bind("TEST")
    # Confirm breakout at 113 on the closed bar, then current ask retests entry.
    frame = __import__("pandas").DataFrame([row()])
    monkeypatch.setattr(bm, "ChandraTrendEngine", lambda *args: NS(initialize=lambda: None, calculate_frame=lambda: frame))
    monkeypatch.setattr(bm.core, "signal_from_row", lambda row: NS(
        buy_condition=False, sell_condition=False, strategic_buy=False, strategic_sell=False,
        magical_buy=False, magical_sell=False, magical_invalid=False, close=113, l100=110))
    monkeypatch.setattr(bm.core, "process_candle", lambda *args: pytest.fail("POB entered legacy strategy execution"))
    monkeypatch.setattr(bm.time, "sleep", lambda seconds: manager._stop.set())
    manager._run()
    assert manager.state.error is None
    assert manager._pob.data["position"]["side"] == "BUY"
    assert not b.requests  # Paper mode never sends an MT5 order.
    assert manager.status_payload()["strategy_status"]["pob"]["position"] == 1


def test_active_plan_cannot_be_reconfigured(tmp_path):
    e, b, levels = execution(tmp_path)
    with pytest.raises(ValueError, match="retain its saved"):
        e.configure(dict(e.data["config"], pob_high=111))


def test_exit_does_not_reuse_breakout_from_open_position(tmp_path):
    e, b, levels = execution(tmp_path)
    e.poll(row(), True)
    b.price(levels["t3"])
    e.poll(None, True)
    b.price(levels["entry"])
    e.poll(row(5), True)
    e.poll(row(5), True)
    assert e.data["position"] is None and e.strategy.armed is None


def test_trade_events_are_imported_once_including_partial_pnl(tmp_path, monkeypatch):
    from backend.app import bot_manager as bm
    e, b, levels = execution(tmp_path)
    monkeypatch.setattr(bm, "POB_FILE", tmp_path / "manager.json")
    monkeypatch.setattr(bm, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(bm.core, "mt5", b)
    monkeypatch.setattr(bm.LicenseService, "require", lambda self: {"features": ["*"], "max_lot": 100})
    manager = bm.BotManager()
    manager.state.strategies = ["pob"]
    manager._reset_strategy_states()
    manager._pob = e
    b.price(levels["t2"])
    e.poll(None, False)
    manager._sync_pob()
    manager._sync_pob()
    assert len(manager.state.trades) == 2
    assert manager.state.strategy_states["pob"].realized_pnl == pytest.approx((124.6 - 112.36) * .02 * 100)


def test_old_journal_defaults_to_original_stop_mode(tmp_path):
    import json
    e, b, levels = execution(tmp_path)
    data = json.loads(e.path.read_text())
    data["config"].pop("pob_sl_mode", None)
    e.path.write_text(json.dumps(data))
    restored = POBExecution(e.path, e.core)
    assert restored.data["config"]["pob_sl_mode"] == "default"
    assert restored.data["position"]["sl"] == levels["stop"]
    restored.configure(dict(restored.data["config"], pob_sl_mode="default"))


def test_invalid_stop_mode_rejected(tmp_path):
    from backend.app.api.trading import ConfigIn
    with pytest.raises(ValueError):
        ConfigIn(pob_sl_mode="invalid")
    e, b, levels = execution(tmp_path)
    with pytest.raises(ValueError, match="POB SL"):
        e.configure(dict(e.data["config"], pob_sl_mode="invalid"))


@pytest.mark.parametrize("mode", ["paper", "live"])
@pytest.mark.parametrize("side,opposite", [("BUY", "sell"), ("SELL", "buy")])
def test_opposite_entry_initial_stop_and_recovery(tmp_path, mode, side, opposite):
    e, b, levels = execution(tmp_path, mode=mode, side=side, sl_mode="levels")
    expected = calculate_levels(110, 100)[opposite]["entry"]
    assert e.data["position"]["sl"] == expected
    assert e.data["position"]["levels"]["stop"] == expected
    if mode == "live":
        assert b.requests[0]["sl"] == expected
    restored = POBExecution(e.path, e.core)
    restored.bind("TEST")
    assert restored.data["config"]["pob_sl_mode"] == "levels"
    assert restored.strategy.levels[side.lower()]["stop"] == expected
    assert restored.data["position"]["sl"] == expected
    b.price(expected)
    restored.poll(None, False)
    if mode == "live":
        restored.poll(None, False)
    assert restored.data["position"] is None


@pytest.mark.parametrize("side,close,price", [("BUY", 113, 106), ("SELL", 97, 104)])
def test_retest_invalidation_uses_selected_stop(side, close, price):
    original = POBStrategy(110, 100)
    opposite = POBStrategy(110, 100, "levels")
    original.decide(row(close=close))
    opposite.decide(row(close=close))
    assert original.retest(row(1)["time"], price) is None
    signal = opposite.retest(row(1)["time"], price)
    assert signal["side"] == side
    assert signal["spot_stop"] == opposite.levels[side.lower()]["stop"]


def test_stop_choice_persisted_by_manager_and_default_preserved(tmp_path, monkeypatch):
    from backend.app import bot_manager as bm
    from backend.app.api.trading import ConfigIn
    monkeypatch.setattr(bm, "POB_FILE", tmp_path / "state.json")
    monkeypatch.setattr(bm, "HISTORY_FILE", tmp_path / "history.json")
    assert ConfigIn().pob_sl_mode == "levels"
    assert ConfigIn(pob_sl_mode="default").pob_sl_mode == "default"
    monkeypatch.setattr(bm.LicenseService, "require", lambda self: {"features": ["*"], "max_lot": 100})
    manager = bm.BotManager()
    cfg = ConfigIn(strategies=["pob"], pob_high=110, pob_low=100, pob_sl_mode="levels")
    manager.configure(**cfg.model_dump())
    restored = bm.BotManager()
    status = restored.status_payload()
    assert status["pob_sl_mode"] == "levels"
    assert status["pob"]["levels"]["buy"]["stop"] == pytest.approx(97.64)
    assert status["pob"]["levels"]["sell"]["stop"] == pytest.approx(112.36)
