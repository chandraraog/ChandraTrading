"""POB's MT5/paper lifecycle. No MT5 initialization or orders at import time.

Journal intent before sending. Reconcile deals and position volume before
advancing targets. An ambiguous request remains pending across restarts.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
import threading
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

from backend.app.strategies.pob import POBStrategy, calculate_levels

MAGIC = 26081205
EPS = 1e-8


def valid_volume(volume, info):
    values = (volume, info.volume_min, info.volume_max, info.volume_step)
    if not all(math.isfinite(float(x)) and float(x) > 0 for x in values):
        return False
    v, minimum, maximum, step = map(lambda x: Decimal(str(x)), values)
    return minimum <= v <= maximum and abs(float(v / step) - round(float(v / step))) < EPS


def half_volume(volume, info):
    half = float(Decimal(str(volume)) / 2)
    return half if valid_volume(half, info) and valid_volume(volume - half, info) else None


class POBExecution:
    def __init__(self, path: Path, core):
        self.path, self.core = Path(path), core
        self.lock = threading.RLock()
        self.data = {"config": None, "position": None, "pending": None,
                     "signal": {}, "events": [], "account": None, "error": None}
        if self.path.exists():
            # Fail closed: a corrupt journal may describe an in-flight order.
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or loaded.get("version") != 1:
                raise ValueError("Invalid POB recovery journal; inspect broker exposure before recovery")
            self.data.update(loaded)
        cfg = self.data["config"] or {}
        if cfg:
            cfg.setdefault("pob_sl_mode", "default")
        source = (self.data.get("candle_range") or {}) if cfg.get("pob_level_source") == "candle" else {}
        self.strategy = POBStrategy(source.get("high", cfg.get("pob_high")), source.get("low", cfg.get("pob_low")), cfg.get("pob_sl_mode", "default"))
        levels = self.strategy.levels
        self.strategy.__dict__.update(self.data.get("signal", {}))
        self.strategy.levels = levels
        # Never infer a crossing over a disconnected interval.
        self.strategy.last_quote = None

    @property
    def active(self):
        return bool(self.data["position"] or self.data["pending"])

    def save(self):
        # Serialize all writers, including direct saves outside poll/configure.
        with self.lock:
            self.data["signal"] = self.strategy.__dict__.copy()
            self.data["version"] = 1
            payload = json.dumps(self.data, indent=2, allow_nan=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = None
            try:
                # Same directory keeps replacement atomic. Unique names prevent
                # writers from replacing or removing each other's staging file.
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                        dir=self.path.parent, prefix=self.path.name + ".",
                        suffix=".tmp", delete=False) as stream:
                    tmp = Path(stream.name)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                for attempt in range(7):
                    try:
                        tmp.replace(self.path)
                        break
                    except OSError as exc:
                        transient = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32, 33)
                        if not transient or attempt == 6:
                            raise
                        time.sleep(min(.05 * 2 ** attempt, .5))
            finally:
                if tmp is not None:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass  # Never hide the original persistence error.

    def configure(self, config):
        with self.lock:
            config = dict(config)
            config.setdefault("pob_sl_mode", "levels")
            if config["pob_sl_mode"] not in ("default", "levels"):
                raise ValueError("POB SL must use Default or Buy/Sell levels")
            config.setdefault("pob_level_source", "manual")
            config.setdefault("pob_candle_time", "04:00")
            if config["pob_level_source"] == "manual":
                calculate_levels(config["pob_high"], config["pob_low"])
            previous = dict(self.data["config"] or {})
            previous.setdefault("pob_level_source", "manual")
            previous.setdefault("pob_candle_time", "04:00")
            keys = ("symbol", "timeframe", "lot", "mode", "pob_high", "pob_low", "pob_entry_mode", "pob_sl_mode", "pob_level_source", "pob_candle_time")
            changed = any(config.get(k) != previous.get(k) for k in keys)
            if self.active and changed:
                raise ValueError("POB has a position or unresolved request; retain its saved configuration")
            if changed:
                self.data["target_completed"] = False
                self.strategy = POBStrategy(config["pob_high"], config["pob_low"], config["pob_sl_mode"])
                self.data["account"] = None
                self.data["candle_range"] = None
            self.data["config"] = config.copy()
            self.save()

    def bind(self, symbol):
        with self.lock:
            cfg = self.data["config"]
            if cfg["mode"] == "live":
                account = self.core.mt5.account_info()
                if account is None:
                    raise RuntimeError("POB: account information unavailable")
                if account.margin_mode != getattr(self.core.mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2):
                    raise ValueError("Live POB requires an MT5 hedging account to keep strategy positions independent")
                identity = [str(account.login), str(account.server), symbol]
                if self.active and self.data["account"] != identity:
                    raise ValueError("POB recovery belongs to another account/server/symbol")
                self.data["account"] = identity
            self.symbol = symbol
            self.strategy.last_quote = None
            info = self.core.ensure_symbol(symbol)
            if not self.active and not valid_volume(cfg["lot"], info):
                raise ValueError("POB lot must match broker minimum, maximum and volume step")
            self.save()

    def _event(self, action, price, volume, pnl=0.0, reason="", event_id=None):
        p = self.data["position"]
        event_id = event_id or uuid.uuid4().hex
        if any(e["event_id"] == event_id for e in self.data["events"]):
            return
        self.data["events"].append(dict(
            event_id=event_id, time=datetime.now().isoformat(timespec="seconds"),
            strategy="pob", side=p["side"], action=action, price=price, volume=volume,
            pnl=pnl if action != "OPEN" else None, mode=self.data["config"]["mode"],
            status="LIVE" if self.data["config"]["mode"] == "live" else "SIMULATED",
            message=reason, sl=p["sl"], target=p["levels"]["t3"],
            symbol=self.symbol, timeframe=self.data["config"].get("timeframe"),
            trade_id=p["trade_id"], ticket=str(p.get("ticket", ""))))

    def _send(self, request, kind, **metadata):
        """One submission; never retry an unknown or partially filled request."""
        comment = "POB-" + uuid.uuid4().hex[:20]
        request["comment"] = comment
        pending = dict(kind=kind, comment=comment, request=request,
                       sent_at=datetime.now(timezone.utc).timestamp(), **metadata)
        self.data["pending"] = pending
        self.save()
        result = self.core.mt5.order_send(request)
        # If order_send raises, durable pending intent still prevents duplicates.
        if result is not None:
            pending.update(retcode=int(result.retcode), order=int(getattr(result, "order", 0)),
                           deal=int(getattr(result, "deal", 0)),
                           filled=float(getattr(result, "volume", 0) or 0))
            # Only these explicit pre-execution rejections are safe to discard.
            rejected = {10004, 10006, 10013, 10014, 10015, 10016, 10017,
                        10018, 10019, 10021, 10022, 10026, 10027, 10030, 10035}
            if result.retcode in rejected:
                self.data["error"] = f"POB request rejected: {result.retcode} {getattr(result, 'comment', '')}"
                self.data["pending"] = None
        else:
            self.data["error"] = "POB request outcome unknown; awaiting broker reconciliation"
        self.save()

    def _request(self, side, volume, info, ticket=None):
        mt5 = self.core.mt5
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            raise RuntimeError("POB: quote unavailable")
        modes = [m for m in self.core.supported_filling_modes(info)
                 if m in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC)]
        if not modes:
            raise ValueError("POB requires FOK or IOC filling support")
        request = dict(action=mt5.TRADE_ACTION_DEAL, symbol=self.symbol, volume=volume,
                       type=mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
                       price=float(tick.ask if side == "BUY" else tick.bid), deviation=30,
                       magic=MAGIC, type_time=mt5.ORDER_TIME_GTC, type_filling=modes[0])
        if ticket is not None:
            request["position"] = int(ticket)
        return request

    def _price(self, value, info):
        step = float(getattr(info, "trade_tick_size", 0) or info.point)
        return round(round(value / step) * step, int(info.digits))

    def enter(self, decision, tick, info):
        if self.data.get("target_completed"):
            return
        if self.active:
            return
        cfg = self.data["config"]
        side = decision["side"]
        levels = dict(self.strategy.levels[side.lower()])
        price = float(tick.ask if side == "BUY" else tick.bid)
        if not (levels["stop"] < price < levels["t2"] if side == "BUY"
                else levels["t2"] < price < levels["stop"]):
            return
        if not valid_volume(cfg["lot"], info):
            raise ValueError("POB lot violates broker volume rules")
        plan = dict(side=side, levels=levels, sl=levels["stop"], entry=price,
                    sl_mode=cfg.get("pob_sl_mode", "default"),
                    volume=cfg["lot"], initial_volume=cfg["lot"],
                    half=half_volume(cfg["lot"], info), t2_triggered=False,
                    t2_filled=0.0, t2_done=False, trade_id=uuid.uuid4().hex,
                    seen_deals=[], ticket=None, identifier=None, exit_reason=None)
        self.data["error"] = None
        if cfg["mode"] == "paper":
            self.data["position"] = plan
            self._event("OPEN", price, cfg["lot"], reason="PAPER — POB")
            self.save()
        else:
            request = self._request(side, cfg["lot"], info)
            request.update(sl=self._price(levels["stop"], info), tp=self._price(levels["t3"], info))
            self._send(request, "entry", plan=plan)

    def reconcile(self, info):
        mt5 = self.core.mt5
        positions = mt5.positions_get(symbol=self.symbol)
        if positions is None:
            raise RuntimeError("POB: broker positions unavailable; no orders submitted")
        owned = [p for p in positions if p.magic == MAGIC]
        if len(owned) > 1:
            raise RuntimeError("POB: multiple owned positions require broker review")
        pending, p = self.data["pending"], self.data["position"]
        if pending and pending["kind"] == "entry" and not p:
            if owned:
                broker = owned[0]
                p = pending["plan"]
                p.update(ticket=int(broker.ticket), identifier=int(getattr(broker, "identifier", broker.ticket)),
                         entry=float(broker.price_open), volume=float(broker.volume))
                self.data["position"] = p
            else:
                # An entry may fill and hit its broker SL before the first snapshot.
                deals = mt5.history_deals_get(datetime.fromtimestamp(pending["sent_at"] - 60, timezone.utc),
                                              datetime.now(timezone.utc))
                matching = [d for d in deals or () if getattr(d, "magic", None) == MAGIC
                            and getattr(d, "symbol", None) == self.symbol
                            and (getattr(d, "comment", "") == pending["comment"] or
                                 pending.get("order") and getattr(d, "order", 0) == pending["order"])]
                if matching:
                    p = pending["plan"]
                    p.update(ticket=int(matching[0].position_id), identifier=int(matching[0].position_id))
                    self.data["position"] = p
        if owned and not p:
            raise RuntimeError("POB: existing broker position has no saved trade plan; inspect recovery journal")
        if not p:
            return
        if owned and int(getattr(owned[0], "identifier", owned[0].ticket)) != p["identifier"]:
            raise RuntimeError("POB: broker position does not match recovery journal")
        deals = mt5.history_deals_get(position=p["identifier"])
        if deals is None:
            raise RuntimeError("POB: broker deal history unavailable")
        ins = [d for d in deals if d.entry == getattr(mt5, "DEAL_ENTRY_IN", 0)]
        outs = [d for d in deals if d.entry in (getattr(mt5, "DEAL_ENTRY_OUT", 1), getattr(mt5, "DEAL_ENTRY_OUT_BY", 3))]
        initial = sum(float(d.volume) for d in ins)
        closed = sum(float(d.volume) for d in outs)
        actual = float(owned[0].volume) if owned else 0.0
        # Position and deal feeds can arrive out of order. Do not act until equal.
        if not initial or abs(initial - closed - actual) > EPS:
            self.data["error"] = "POB: awaiting consistent broker fills and position volume"
            self.save()
            return
        p.update(initial_volume=initial, volume=actual, half=half_volume(initial, info))
        p["entry"] = sum(float(d.price) * float(d.volume) for d in ins) / initial
        if owned:
            p["ticket"] = int(owned[0].ticket)
            p["broker_sl"] = float(owned[0].sl)
            p["unrealized_pnl"] = float(getattr(owned[0], "profit", 0) or 0)
        for d in sorted(deals, key=lambda d: (getattr(d, "time_msc", 0), int(d.ticket))):
            if str(d.ticket) in p["seen_deals"]:
                continue
            if d in ins:
                self._event("OPEN", float(d.price), float(d.volume), reason="LIVE — POB fill",
                            event_id=f"{p['trade_id']}-{d.ticket}")
            elif d in outs:
                t2 = pending and pending["kind"] == "t2" and (
                    getattr(d, "comment", "") == pending["comment"] or
                    pending.get("order") and getattr(d, "order", 0) == pending["order"])
                if t2:
                    p["t2_filled"] += float(d.volume)
                pnl = sum(float(getattr(d, k, 0) or 0) for k in ("profit", "commission", "swap", "fee"))
                self._event("CLOSE" if actual <= EPS and d == outs[-1] else "PARTIAL_CLOSE",
                            float(d.price), float(d.volume), pnl, "LIVE — POB T2" if t2 else "LIVE — POB broker exit",
                            event_id=f"{p['trade_id']}-{d.ticket}")
            p["seen_deals"].append(str(d.ticket))
        if pending:
            if pending["kind"] == "sl":
                if not owned or abs(float(owned[0].sl) - pending["request"]["sl"]) <= float(info.point) / 2:
                    self.data["pending"] = None
            else:
                matching = [d for d in deals if getattr(d, "comment", "") == pending["comment"] or
                            pending.get("order") and getattr(d, "order", 0) == pending["order"]]
                filled = sum(float(d.volume) for d in matching)
                terminal = pending.get("retcode") in (10009, 10010)
                if not terminal and matching:
                    orders = mt5.history_orders_get(ticket=int(matching[0].order))
                    terminal = bool(orders and orders[-1].state in (
                        getattr(mt5, "ORDER_STATE_FILLED", 4), getattr(mt5, "ORDER_STATE_CANCELED", 2)))
                if terminal and matching and filled + EPS >= pending.get("filled", 0):
                    self.data["pending"] = None
        if p["half"] is not None and p["t2_filled"] + EPS >= p["half"]:
            p["t2_done"] = True
            p["sl"] = p["levels"]["entry"]
        self.data["error"] = None
        if actual <= EPS and not self.data["pending"]:
            if p.get("exit_reason") == "POB T3" or any(
                    getattr(d, "reason", None) == getattr(mt5, "DEAL_REASON_TP", 5) for d in outs):
                self.data["target_completed"] = True
            self.data["position"] = None
            self.strategy.armed = self.strategy.arm_time = None
            self.data["wait_for_new_bar"] = True
        self.save()

    def _paper_close(self, volume, price, reason, info):
        p = self.data["position"]
        pnl = (price - p["entry"]) * (1 if p["side"] == "BUY" else -1) * volume * float(info.trade_contract_size)
        p["volume"] = round(p["volume"] - volume, 10)
        if reason == "POB T2":
            p["t2_filled"] += volume
            if p["half"] is not None and p["t2_filled"] + EPS >= p["half"]:
                p["t2_done"] = True
                p["sl"] = p["levels"]["entry"]
        self._event("CLOSE" if p["volume"] <= EPS else "PARTIAL_CLOSE", price, volume, pnl, "PAPER — " + reason)
        if p["volume"] <= EPS:
            if reason == "POB T3":
                self.data["target_completed"] = True
            self.data["position"] = None
            self.data["wait_for_new_bar"] = True
        self.save()

    def close(self, reason="MANUAL CLOSE", volume=None):
        with self.lock:
            p = self.data["position"]
            if not p or self.data["pending"]:
                return False
            info = self.core.ensure_symbol(self.symbol)
            volume = min(p["volume"], volume or p["volume"])
            if not valid_volume(volume, info):
                raise ValueError("POB close volume violates broker rules")
            if volume < p["volume"] - EPS and not valid_volume(p["volume"] - volume, info):
                raise ValueError("POB remaining volume violates broker rules")
            if self.data["config"]["mode"] == "paper":
                tick = self.core.mt5.symbol_info_tick(self.symbol)
                self._paper_close(volume, float(tick.bid if p["side"] == "BUY" else tick.ask), reason, info)
                return True
            opposite = "SELL" if p["side"] == "BUY" else "BUY"
            if reason != "POB T2":
                p["exit_reason"] = reason
            self._send(self._request(opposite, volume, info, p["ticket"]),
                       "t2" if reason == "POB T2" else "exit")
            return False  # Submission is not a confirmed close.

    def manage(self, tick, info):
        p = self.data["position"]
        if not p or self.data["pending"] or self.data["error"]:
            return
        price = float(tick.bid if p["side"] == "BUY" else tick.ask)
        buy = p["side"] == "BUY"
        reached = lambda level: price >= level if buy else price <= level
        stopped = price <= p["sl"] if buy else price >= p["sl"]
        if stopped or reached(p["levels"]["t3"]) or p.get("exit_reason"):
            p["exit_reason"] = p.get("exit_reason") or ("POB SL" if stopped else "POB T3")
            self.save()
            self.close(p["exit_reason"])
            return
        if reached(p["levels"]["t2"]):
            p["t2_triggered"] = True
        if p["t2_triggered"] and not p["t2_done"]:
            if p["half"] is None:
                # User-selected forex fallback: keep full volume for T3.
                p["t2_done"] = True
                p["sl"] = p["levels"]["entry"]
                self.save()
            else:
                remaining = round(p["half"] - p["t2_filled"], 10)
                self.save()
                self.close("POB T2", remaining)
                return
        if p["t2_done"] and self.data["config"]["mode"] == "live":
            desired = self._price(p["sl"], info)
            broker_sl = p.get("broker_sl", 0)
            already = broker_sl >= desired if buy else 0 < broker_sl <= desired
            if not already:
                self._send(dict(action=self.core.mt5.TRADE_ACTION_SLTP, symbol=self.symbol,
                                position=p["ticket"], sl=desired,
                                tp=self._price(p["levels"]["t3"], info), magic=MAGIC), "sl")

    def _prepare_candle_range(self):
        """Use POB's own symbol/timeframe and a completed MT5 chart-time bar."""
        cfg = self.data["config"]
        if cfg.get("pob_level_source", "manual") != "candle":
            return True
        if self.active:
            return False  # Never change levels protecting an existing position.
        try:
            clock = self.core.mt5.copy_rates_from_pos(self.symbol, self.core.timeframe_to_mt5("M1"), 0, 1)
            if clock is None or not len(clock):
                raise ValueError("MT5 chart time unavailable")
            now = datetime.fromtimestamp(int(clock[-1]["time"]), timezone.utc).replace(tzinfo=None)
            h, m = map(int, cfg["pob_candle_time"].split(":"))
            start = now.replace(hour=h, minute=m, second=0, microsecond=0)
            key = start.isoformat()
            saved = self.data.get("candle_range") or {}
            if saved.get("time") == key and self.strategy.levels:
                self.data["candle_message"] = "POB levels from candle " + key
                return True
            # Yesterday's levels must not arm today's entries.
            self.strategy = POBStrategy()
            self.data["candle_range"] = None
            end = start + timedelta(minutes=self.core.timeframe_minutes(cfg["timeframe"]))
            if now < end:
                self.data["candle_message"] = f"Waiting for POB {cfg['pob_candle_time']} candle to close"
                return False
            rates = self.core.get_rates_range(self.symbol, cfg["timeframe"],
                (start - timedelta(days=1)).replace(tzinfo=timezone.utc),
                (end + timedelta(days=1)).replace(tzinfo=timezone.utc))
            if rates is None or rates.empty:
                raise ValueError("Candle history unavailable")
            matches = rates[rates["time"].map(lambda t: t.replace(tzinfo=None) == start)]
            if matches.empty:
                raise ValueError("Selected candle not available; choose a start time matching POB's timeframe")
            candle = matches.iloc[-1]
            high, low = float(candle["high"]), float(candle["low"])
            self.strategy = POBStrategy(high, low, cfg.get("pob_sl_mode", "default"))
            self.data["candle_range"] = dict(time=key, high=high, low=low)
            self.data["candle_message"] = "POB levels from candle " + key
            self.save()
            return True
        except (ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
            self.data["candle_message"] = "Waiting for POB candle: " + str(exc)
            return False

    def poll(self, row, entries_enabled):
        with self.lock:
            info = self.core.ensure_symbol(self.symbol)
            if self.data["config"]["mode"] == "live":
                self.reconcile(info)
            tick = self.core.mt5.symbol_info_tick(self.symbol)
            if tick is None or not all(math.isfinite(float(v)) and float(v) > 0 for v in (tick.bid, tick.ask)):
                return
            had_position = self.active
            self.manage(tick, info)
            if not self._prepare_candle_range():
                return
            if row is None:
                return
            if self.data.pop("wait_for_new_bar", False):
                self.strategy.last_time = str(row["time"])
                self.strategy.armed = self.strategy.arm_time = None
                self.strategy.last_quote = None
                self.save()
                return
            if self.active or had_position or not entries_enabled or self.data.get("target_completed"):
                self.strategy.last_time = str(row["time"])
                self.strategy.last_quote = None
                self.strategy.armed = self.strategy.arm_time = None
                self.save()
                return
            cfg = self.data["config"]
            # MT5 timestamps share the chart clock used by completed candles.
            timestamp = datetime.fromtimestamp(float(tick.time), timezone.utc).replace(tzinfo=None)
            if cfg["pob_entry_mode"] == "direct":
                decision = self.strategy.crossing(timestamp, float(tick.bid), float(tick.ask))
            else:
                decision = self.strategy.decide(row)
                if not decision:
                    price = float(tick.ask if self.strategy.armed == "BUY" else tick.bid)
                    decision = self.strategy.retest(timestamp, price)
            self.save()
            if decision:
                self.enter(decision, tick, info)

    def status(self):
        with self.lock:
            p = self.data["position"]
            return {"target_completed": bool(self.data.get("target_completed")), "levels": self.strategy.levels, "armed": self.strategy.armed,
                    "position": dict(p) if p else None,
                    "pending": self.data["pending"]["kind"] if self.data["pending"] else None,
                    "error": self.data["error"],
                    "candle_range": self.data.get("candle_range"),
                    "candle_message": self.data.get("candle_message")}
