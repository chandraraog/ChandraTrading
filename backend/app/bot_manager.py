import json
import pandas as pd
import threading
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from backend.app.engine.chandra_trend_engine import ChandraTrendEngine
from backend.app.engine import chandra_core as core
from backend.app.strategies.timing_candle import breakout_arm, retest_signal

# Website V1 enables the engine's L100-based initial/trailing SL.
core.STOPLOSS_CONFIG["enabled"] = True
core.STOPLOSS_CONFIG["offset_points"] = 5.0

STRATEGIES = ("strategic", "magical", "timing")
MAGIC_BY_STRATEGY = {"strategic": 26081201, "magical": 26081202, "timing": 26081203, "manual": 26081204}
ENGINE_MODE = {"strategic": "buy_sell", "magical": "magical", "timing": "timing"}
DISPLAY_NAME = {"strategic": "Strategic Entry", "magical": "Magical Entry", "timing": "Timing Candle"}
HISTORY_FILE = Path(__file__).resolve().parents[3] / "data" / "trade_history.json"

@dataclass
class TradeRecord:
    time: str
    strategy: str
    side: str
    action: str
    price: float
    volume: float
    mode: str
    status: str
    pnl: float | None = None
    message: str = ""
    sl: float | None = None
    target: float | None = None

@dataclass
class StrategyState:
    strategy: str
    execution: core.ExecutionState
    paper_position: int = 0
    paper_entry_price: float | None = None
    paper_entry_side: str | None = None
    paper_sl: float | None = None
    last_l100: float | None = None
    current_pnl: float = 0.0
    realized_pnl: float = 0.0
    open_trade_time: str | None = None
    timing_date: str | None = None
    timing_high: float | None = None
    timing_low: float | None = None
    timing_armed_buy: bool = False
    timing_armed_sell: bool = False
    timing_arm_time: str | None = None
    target: float | None = None
    volume: float = 0.0
    active_trade_id: str | None = None
    live_ticket: str | None = None
    # Kept for backward compatibility with older clients that displayed a
    # Timing LIMIT order. Timing Candle now enters at market on a live touch.
    timing_pending_ticket: str | None = None
    timing_pending_side: str | None = None
    timing_pending_price: float | None = None
    timing_pending_sl: float | None = None

@dataclass
class BotState:
    running: bool = False
    strategies: list = field(default_factory=lambda: ["strategic"])
    symbol: str = "XAUUSD"
    timeframe: str = "M1"
    lot: float = 0.01
    mode: str = "paper"
    last_signal: dict | None = None
    error: str | None = None
    entries_enabled: bool = True
    safety_halt_reason: str | None = None
    mt5_connected: bool = False
    account: dict = field(default_factory=dict)
    mt5_symbol: str = "XAUUSD"
    timing_hour: int = 9
    timing_minute: int = 30
    timing_end_time: str | None = None
    contract_size: float = 1.0
    trades: list = field(default_factory=list)
    strategy_states: dict = field(default_factory=dict)
    manual_state: StrategyState | None = None

class BotManager:
    def __init__(self):
        self.state = BotState()
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._reset_strategy_states()
        self._reset_manual_state()
        self._load_trade_history()
        self._restore_realized_pnl()

    @property
    def live_enabled(self):
        return self.state.mode == "live"

    def _reset_strategy_states(self):
        self.state.strategy_states = {}
        for name in self.state.strategies:
            self.state.strategy_states[name] = StrategyState(
                strategy=name,
                execution=core.ExecutionState(
                    mode=ENGINE_MODE[name],
                    magic_number=MAGIC_BY_STRATEGY[name],
                    entries_enabled=self.state.entries_enabled,
                ),
            )

    def _reset_manual_state(self):
        self.state.manual_state = StrategyState(
            strategy="manual",
            execution=core.ExecutionState(
                mode="manual",
                magic_number=MAGIC_BY_STRATEGY["manual"],
                entries_enabled=True,
            ),
        )

    def _manual_state(self):
        if self.state.manual_state is None:
            self._reset_manual_state()
        return self.state.manual_state

    def configure(self, strategies, symbol, timeframe, lot, mode, timing_hour=9,
                  timing_minute=30, timing_end_time=None):
        if isinstance(strategies, str):
            strategies = [strategies]
        strategies = list(dict.fromkeys(strategies))
        if not strategies or any(s not in STRATEGIES for s in strategies):
            raise ValueError("Select at least one valid strategy")
        if mode not in ("paper", "live"):
            raise ValueError("Invalid trading mode")
        if not (0 <= int(timing_hour) <= 23 and 0 <= int(timing_minute) <= 59):
            raise ValueError("Invalid timing candle time")
        if self.state.running:
            raise RuntimeError("Stop the bot before changing configuration")
        self.state.strategies = strategies
        self.state.symbol = symbol
        self.state.timeframe = timeframe
        self.state.lot = lot
        self.state.mode = mode
        self.state.timing_hour = int(timing_hour)
        self.state.timing_minute = int(timing_minute)
        self.state.timing_end_time = timing_end_time or None
        self.state.entries_enabled = True
        self.state.safety_halt_reason = None
        self._reset_strategy_states()

    def stop_trading(self):
        """Disable new entries for all enabled strategies; keep exits/SL active."""
        self.state.entries_enabled = False
        self.state.safety_halt_reason = "Manual STOP TRADING"
        for ss in self.state.strategy_states.values():
            ss.execution.entries_enabled = False

    def resume_trading(self):
        """Re-enable new entries after a safety stop."""
        if not self.state.running:
            raise RuntimeError("Start the bot before resuming trading")
        self.state.entries_enabled = True
        self.state.safety_halt_reason = None
        for ss in self.state.strategy_states.values():
            ss.execution.entries_enabled = True

    def close_strategy(self, strategy: str):
        """Close only positions owned by one Chandra strategy."""
        if strategy not in STRATEGIES:
            raise ValueError("Invalid strategy")
        ss = self.state.strategy_states.get(strategy)
        if ss is None:
            return {"ok": True, "closed": 0, "message": f"{DISPLAY_NAME[strategy]} is not enabled"}

        if not self.live_enabled:
            if ss.paper_position == 0:
                return {"ok": True, "closed": 0, "message": f"No {DISPLAY_NAME[strategy]} paper position is open"}
            side = ss.paper_entry_side or ("BUY" if ss.paper_position == 1 else "SELL")
            price = self._paper_price("SELL" if side == "BUY" else "BUY") or ss.paper_entry_price
            self._paper_close(ss, price, "MANUAL CLOSE")
            ss.execution.virtual_position = 0
            return {"ok": True, "closed": 1, "strategy": strategy, "message": f"{DISPLAY_NAME[strategy]} paper position closed"}

        positions = self._live_positions(ss.execution.magic_number)
        if not positions:
            self._sync_live_snapshot(ss)
            return {"ok": True, "closed": 0, "message": f"No {DISPLAY_NAME[strategy]} live position is open"}
        info = core.ensure_symbol(self.state.mt5_symbol)
        closed = 0
        errors = []
        for position in positions:
            if core.close_position(position, info, True, ss.execution.magic_number):
                closed += 1
            else:
                errors.append(str(getattr(position, "ticket", "unknown")))
        if not self._live_positions(ss.execution.magic_number):
            self._record_live_close(ss)
        self._sync_live_snapshot(ss)
        if errors:
            return {"ok": False, "closed": closed, "message": f"Some positions failed to close: {', '.join(errors)}"}
        return {"ok": True, "closed": closed, "strategy": strategy, "message": f"{DISPLAY_NAME[strategy]} live position(s) closed"}

    def close_all(self):
        """Close only positions belonging to the enabled Chandra strategies."""
        results = []
        total = 0
        for strategy in list(self.state.strategy_states.keys()):
            result = self.close_strategy(strategy)
            results.append(result)
            total += int(result.get("closed", 0))
        manual_result = self.close_manual()
        results.append({"strategy": "manual", **manual_result})
        total += int(manual_result.get("closed", 0))
        ok = all(r.get("ok", False) for r in results)
        return {"ok": ok, "closed": total, "results": results, "message": f"Closed {total} Chandra position(s)"}

    def _resolve_mt5_symbol(self):
        requested = self.state.symbol.strip()
        if core.mt5.symbol_info(requested) is not None:
            return requested
        if requested.upper() == "XAUUSD":
            candidates = ["XAUUSD.sd", "XAUUSDm", "XAUUSD.a", "XAUUSD.r"]
            for candidate in candidates:
                if core.mt5.symbol_info(candidate) is not None:
                    return candidate
            matches = [s.name for s in (core.mt5.symbols_get() or []) if "XAUUSD" in s.name.upper()]
            if matches:
                return matches[0]
        raise RuntimeError(f"MT5 symbol not found for '{requested}'")

    def mt5_test(self):
        try:
            if not core.mt5.initialize():
                return {"connected": False, "error": str(core.mt5.last_error())}
            account = core.mt5.account_info()
            if account is None:
                return {"connected": False, "error": "MT5 terminal is open but no account is logged in"}
            self.state.mt5_symbol = self._resolve_mt5_symbol()
            info = core.ensure_symbol(self.state.mt5_symbol)
            tick = core.mt5.symbol_info_tick(self.state.mt5_symbol)
            if tick is None:
                return {"connected": False, "error": f"No market tick for {self.state.mt5_symbol}"}
            self.state.contract_size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
            self.state.mt5_connected = True
            self.state.account = {
                "login": getattr(account, "login", None),
                "server": getattr(account, "server", None),
                "balance": getattr(account, "balance", None),
                "equity": getattr(account, "equity", None),
                "currency": getattr(account, "currency", None),
                "bid": getattr(tick, "bid", None),
                "ask": getattr(tick, "ask", None),
                "mt5_symbol": self.state.mt5_symbol,
                "contract_size": self.state.contract_size,
                "volume_min": getattr(info, "volume_min", None),
                "volume_step": getattr(info, "volume_step", None),
                "sl_offset_points": core.STOPLOSS_CONFIG["offset_points"],
                "sl_enabled": core.STOPLOSS_CONFIG["enabled"],
            }
            return {"connected": True, **self.state.account}
        except Exception as exc:
            self.state.mt5_connected = False
            return {"connected": False, "error": str(exc)}

    def start(self):
        if self.state.running:
            raise RuntimeError("Engine is already running")
        result = self.mt5_test()
        if not result.get("connected"):
            raise RuntimeError(result.get("error", "MT5 connection failed"))
        self._stop.clear()
        self.state.error = None
        self.state.running = True
        self.state.entries_enabled = True
        self.state.safety_halt_reason = None
        self._reset_strategy_states()
        self._restore_realized_pnl()
        if self._manual_state().paper_position == 0:
            self._reset_manual_state()
        self._load_trade_history()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.state.running = False

    def _paper_price(self, side):
        tick = core.mt5.symbol_info_tick(self.state.mt5_symbol)
        if not tick:
            return None
        return float(tick.ask if side == "BUY" else tick.bid)

    def _paper_sl_from_l100(self, side, l100):
        if l100 is None:
            return None
        try:
            info = core.ensure_symbol(self.state.mt5_symbol)
            return core.compute_l100_stop_price(side, float(l100), info)
        except Exception:
            return None

    def _restore_realized_pnl(self):
        totals = {name: 0.0 for name in (*STRATEGIES, "manual")}
        for item in self.state.trades:
            if item.get("action") == "CLOSE" and item.get("pnl") is not None and item.get("strategy") in totals:
                totals[item["strategy"]] += float(item["pnl"] or 0.0)
        for name, value in totals.items():
            ss = self.state.manual_state if name == "manual" else self.state.strategy_states.get(name)
            if ss is not None:
                ss.realized_pnl = value

    def _load_trade_history(self):
        try:
            if HISTORY_FILE.exists():
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.state.trades = data[:200]
        except Exception:
            self.state.trades = []

    def _save_trade_history(self):
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_FILE.write_text(json.dumps(self.state.trades[:200], indent=2), encoding="utf-8")
        except Exception:
            pass

    def _record(self, strategy, side, action, price, status, pnl=None, message="", sl=None, target=None, volume=None, trade_id=None, ticket=None):
        rec = TradeRecord(
            datetime.now().isoformat(timespec="seconds"), strategy, side, action,
            float(price), float(volume if volume is not None else self.state.lot), self.state.mode, status, pnl, message, sl, target
        )
        item = rec.__dict__.copy()
        item["trade_id"] = trade_id
        item["ticket"] = str(ticket) if ticket is not None else None
        with self._lock:
            self.state.trades.insert(0, item)
            self.state.trades = self.state.trades[:200]
            self._save_trade_history()

    def _find_open_trade_by_ticket(self, ticket):
        """Reuse an existing broker OPEN record after a runtime restart."""
        if ticket is None:
            return None
        ticket = str(ticket)
        with self._lock:
            for item in self.state.trades:
                if item.get("action") != "OPEN" or str(item.get("ticket") or "") != ticket:
                    continue
                trade_id = item.get("trade_id")
                if not trade_id:
                    continue
                is_closed = any(
                    row.get("action") == "CLOSE"
                    and str(row.get("trade_id") or "") == str(trade_id)
                    for row in self.state.trades
                )
                if not is_closed:
                    return item
        return None

    def _paper_open(self, ss: StrategyState, side, price, l100=None, sl_override=None, target_override=None):
        target = 1 if side == "BUY" else -1
        if ss.paper_position == target:
            return
        if ss.paper_position != 0 and ss.paper_entry_price is not None:
            self._paper_close(ss, price, "REVERSE")
        ss.paper_position = target
        ss.active_trade_id = ss.active_trade_id or uuid.uuid4().hex
        ss.live_ticket = None
        ss.paper_entry_price = price
        ss.paper_entry_side = side
        ss.paper_sl = sl_override if sl_override is not None else self._paper_sl_from_l100(side, l100)
        ss.target = target_override
        ss.volume = float(self.state.lot)
        ss.open_trade_time = datetime.now().isoformat(timespec="seconds")
        self._record(ss.strategy, side, "OPEN", price, "SIMULATED", message="PAPER — NOT SENT TO BROKER", sl=ss.paper_sl, target=ss.target, trade_id=ss.active_trade_id)

    def _paper_close(self, ss: StrategyState, price, reason="EXIT"):
        if ss.paper_position == 0 or ss.paper_entry_price is None:
            return
        entry = ss.paper_entry_price
        side = ss.paper_entry_side
        pnl = ((price - entry) if side == "BUY" else (entry - price)) * (ss.volume or self.state.lot) * self.state.contract_size
        ss.realized_pnl += pnl
        ss.current_pnl = 0.0
        self._record(ss.strategy, side, "CLOSE", price, "SIMULATED", pnl=pnl,
                     message=f"PAPER — {reason} — NOT SENT TO BROKER", sl=ss.paper_sl, target=ss.target, volume=ss.volume or self.state.lot, trade_id=ss.active_trade_id)
        ss.paper_position = 0
        ss.paper_entry_price = None
        ss.paper_entry_side = None
        ss.paper_sl = None
        ss.target = None
        ss.volume = 0.0
        ss.open_trade_time = None
        ss.active_trade_id = None
        ss.live_ticket = None

    def _paper_check_sl(self, ss: StrategyState, row):
        if ss.paper_position == 0 or ss.paper_sl is None:
            return False
        low = float(row["low"]); high = float(row["high"])
        if ss.paper_position == 1 and low <= ss.paper_sl:
            self._paper_close(ss, ss.paper_sl, "SL HIT")
            ss.execution.virtual_position = 0
            return True
        if ss.paper_position == -1 and high >= ss.paper_sl:
            self._paper_close(ss, ss.paper_sl, "SL HIT")
            ss.execution.virtual_position = 0
            return True
        return False

    def _place_timing_pending(self, ss: StrategyState, side: str, entry_price: float, sl: float):
        """Compatibility hook: Timing retests use immediate market execution.

        The name remains so older integrations do not assume a missing order
        path. No pending broker order is created, avoiding stale LIMIT fills.
        """
        if side not in ("BUY", "SELL"):
            return False
        if self.live_enabled:
            return core.open_position(
                self.state.mt5_symbol, side, self.state.lot, True,
                sl=float(sl), magic_number=ss.execution.magic_number,
            )
        self._paper_open(ss, side, float(entry_price), sl_override=float(sl))
        ss.execution.virtual_position = 1 if side == "BUY" else -1
        return True

    def _sync_timing_pending(self, ss: StrategyState):
        """Clear legacy pending-order display state; no LIMIT order is used."""
        ss.timing_pending_ticket = None
        ss.timing_pending_side = None
        ss.timing_pending_price = None
        ss.timing_pending_sl = None

    def _paper_check_target(self, ss: StrategyState):
        if ss.paper_position == 0 or ss.target is None or ss.paper_entry_price is None:
            return False
        tick = core.mt5.symbol_info_tick(self.state.mt5_symbol)
        if tick is None:
            return False
        current = float(tick.bid if ss.paper_position == 1 else tick.ask)
        if ss.paper_position == 1 and current >= ss.target:
            self._paper_close(ss, ss.target, "TARGET HIT")
            ss.execution.virtual_position = 0
            return True
        if ss.paper_position == -1 and current <= ss.target:
            self._paper_close(ss, ss.target, "TARGET HIT")
            ss.execution.virtual_position = 0
            return True
        return False

    def _live_positions(self, magic):
        positions = core.mt5.positions_get(symbol=self.state.mt5_symbol) or []
        return [p for p in positions if p.magic == magic]

    def _record_live_close(self, ss: StrategyState):
        """Record a broker-side exit (SL/TP/manual) after a live position disappears."""
        ticket = ss.live_ticket
        if not ticket or not ss.active_trade_id:
            return False
        try:
            deals = core.mt5.history_deals_get(position=int(ticket)) or []
            out_entry = getattr(core.mt5, "DEAL_ENTRY_OUT", 1)
            exits = [d for d in deals if getattr(d, "entry", None) == out_entry]
            if not exits:
                return False
            exit_deal = max(exits, key=lambda d: getattr(d, "time_msc", getattr(d, "time", 0)))
            pnl = sum(float(getattr(d, "profit", 0.0) or 0.0) for d in exits)
            price = float(getattr(exit_deal, "price", 0.0) or 0.0)
            comment = str(getattr(exit_deal, "comment", "") or "").strip()
            reason = comment or "BROKER EXIT"
            self._record(
                ss.strategy, ss.paper_entry_side or ("BUY" if ss.paper_position == 1 else "SELL"),
                "CLOSE", price, "LIVE", pnl=pnl,
                message=f"LIVE — {reason}", sl=ss.paper_sl, target=ss.target,
                volume=ss.volume or self.state.lot, trade_id=ss.active_trade_id, ticket=ticket
            )
            ss.realized_pnl += pnl
            ss.active_trade_id = None
            ss.live_ticket = None
            return True
        except Exception:
            return False

    def _sync_live_snapshot(self, ss: StrategyState):
        positions = self._live_positions(ss.execution.magic_number)
        if positions:
            p = positions[0]
            side = "BUY" if p.type == core.mt5.POSITION_TYPE_BUY else "SELL"
            ss.paper_position = 1 if side == "BUY" else -1
            ss.paper_entry_price = float(p.price_open)
            ss.paper_entry_side = side
            ss.paper_sl = float(p.sl) if getattr(p, "sl", 0) else None
            ss.target = float(p.tp) if getattr(p, "tp", 0) else None
            ss.volume = float(getattr(p, "volume", 0.0) or 0.0)
            ss.current_pnl = float(getattr(p, "profit", 0.0) or 0.0)
            ss.live_ticket = str(getattr(p, "ticket", ""))
            ss.execution.virtual_position = 1 if side == "BUY" else -1
            if ss.strategy != "manual" and not ss.active_trade_id:
                existing = self._find_open_trade_by_ticket(ss.live_ticket)
                if existing:
                    # duplicate OPEN entry prevented during restart recovery
                    ss.active_trade_id = str(existing["trade_id"])
                else:
                    ss.active_trade_id = uuid.uuid4().hex
                    self._record(
                        ss.strategy, side, "OPEN", ss.paper_entry_price, "LIVE",
                        message="LIVE — POSITION RECOVERED", sl=ss.paper_sl, target=ss.target,
                        volume=ss.volume or self.state.lot, trade_id=ss.active_trade_id, ticket=ss.live_ticket
                    )
        else:
            ss.paper_position = 0
            ss.paper_entry_price = None
            ss.paper_entry_side = None
            ss.paper_sl = None
            ss.target = None
            ss.volume = 0.0
            ss.current_pnl = 0.0
            # A broker-side SL/TP/manual exit ends the position only.  The
            # next completed BO/BD may arm a fresh setup.
            ss.execution.virtual_position = 0

    def manual_order(self, side: str, lot: float, sl: float | None = None, target: float | None = None):
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("Manual side must be BUY or SELL")
        if lot <= 0:
            raise ValueError("Lot size must be greater than zero")
        if sl is not None and sl <= 0:
            sl = None
        if target is not None and target <= 0:
            target = None

        result = self.mt5_test()
        if not result.get("connected"):
            raise RuntimeError(result.get("error", "MT5 connection failed"))
        price = self._paper_price(side)
        if price is None:
            raise RuntimeError("No market price available")

        if side == "BUY":
            if sl is not None and sl >= price:
                raise ValueError("BUY SL must be below the current entry price")
            if target is not None and target <= price:
                raise ValueError("BUY target must be above the current entry price")
        else:
            if sl is not None and sl <= price:
                raise ValueError("SELL SL must be above the current entry price")
            if target is not None and target >= price:
                raise ValueError("SELL target must be below the current entry price")

        ss = self._manual_state()
        if ss.paper_position != 0:
            raise RuntimeError("A manual position is already open. Close it before placing another manual order.")

        magic = MAGIC_BY_STRATEGY["manual"]
        ss.active_trade_id = uuid.uuid4().hex
        if self.live_enabled:
            ok = core.open_position(self.state.mt5_symbol, side, lot, True, sl=sl, tp=target, magic_number=magic)
            if not ok:
                raise RuntimeError("MT5 rejected the manual order")
            self._sync_live_snapshot(ss)
            self._record("manual", side, "OPEN", ss.paper_entry_price or price, "LIVE", message="MANUAL ORDER — SENT TO BROKER", sl=ss.paper_sl, target=ss.target, volume=lot, trade_id=ss.active_trade_id, ticket=ss.live_ticket)
        else:
            ss.paper_position = 1 if side == "BUY" else -1
            ss.paper_entry_price = price
            ss.paper_entry_side = side
            ss.paper_sl = sl
            ss.target = target
            ss.volume = float(lot)
            ss.open_trade_time = datetime.now().isoformat(timespec="seconds")
            ss.execution.virtual_position = ss.paper_position
            self._record("manual", side, "OPEN", price, "SIMULATED", message="MANUAL PAPER ORDER — NOT SENT TO BROKER", sl=sl, target=target, volume=lot, trade_id=ss.active_trade_id)
        return {"ok": True, "side": side, "price": ss.paper_entry_price or price, "sl": ss.paper_sl, "target": ss.target, "mode": self.state.mode}

    def close_manual(self):
        ss = self._manual_state()
        if self.live_enabled:
            positions = self._live_positions(MAGIC_BY_STRATEGY["manual"])
            if not positions:
                self._sync_live_snapshot(ss)
                return {"ok": True, "closed": 0, "message": "No manual live position is open"}
            info = core.ensure_symbol(self.state.mt5_symbol)
            closed = 0
            for position in positions:
                if core.close_position(position, info, True, MAGIC_BY_STRATEGY["manual"]):
                    closed += 1
            if not self._live_positions(MAGIC_BY_STRATEGY["manual"]):
                self._record_live_close(ss)
            self._sync_live_snapshot(ss)
            return {"ok": closed == len(positions), "closed": closed, "message": f"Closed {closed} manual live position(s)"}
        if ss.paper_position == 0:
            return {"ok": True, "closed": 0, "message": "No manual paper position is open"}
        side = ss.paper_entry_side or ("BUY" if ss.paper_position == 1 else "SELL")
        price = self._paper_price("SELL" if side == "BUY" else "BUY") or ss.paper_entry_price
        self._paper_close(ss, price, "MANUAL CLOSE")
        ss.execution.virtual_position = 0
        return {"ok": True, "closed": 1, "message": "Manual paper position closed"}

    def _update_manual_paper(self):
        ss = self._manual_state()
        if ss.paper_position == 0 or ss.paper_entry_price is None:
            return
        tick = core.mt5.symbol_info_tick(self.state.mt5_symbol)
        if tick is None:
            return
        current = float(tick.bid if ss.paper_position == 1 else tick.ask)
        if ss.paper_sl is not None:
            if ss.paper_position == 1 and current <= ss.paper_sl:
                self._paper_close(ss, ss.paper_sl, "SL HIT")
                ss.execution.virtual_position = 0
                return
            if ss.paper_position == -1 and current >= ss.paper_sl:
                self._paper_close(ss, ss.paper_sl, "SL HIT")
                ss.execution.virtual_position = 0
                return
        if ss.target is not None:
            if ss.paper_position == 1 and current >= ss.target:
                self._paper_close(ss, ss.target, "TARGET HIT")
                ss.execution.virtual_position = 0
                return
            if ss.paper_position == -1 and current <= ss.target:
                self._paper_close(ss, ss.target, "TARGET HIT")
                ss.execution.virtual_position = 0
                return
        ss.current_pnl = ((current - ss.paper_entry_price) if ss.paper_position == 1 else (ss.paper_entry_price - current)) * (ss.volume or self.state.lot) * self.state.contract_size

    def _timing_local_dt(self, value):
        """Interpret MT5 candle timestamps as UTC and convert to India time."""
        if isinstance(value, pd.Timestamp):
            value = value.to_pydatetime()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(ZoneInfo("Asia/Kolkata"))

    def _timing_reset_if_needed(self, ss: StrategyState, local_dt):
        day = local_dt.date().isoformat()
        if ss.timing_date != day:
            ss.timing_date = day
            ss.timing_high = None
            ss.timing_low = None
            ss.timing_armed_buy = False
            ss.timing_armed_sell = False
            ss.timing_arm_time = None

    def _restore_timing_levels(self, ss: StrategyState, local_dt):
        """Restore today's completed timing candle when the bot starts late.

        The normal loop only sees the newest completed bar.  Consequently, a
        bot started after the configured time must explicitly read that day's
        historical timing bar before it can look for BO/BD.
        """
        if ss.timing_high is not None or ss.timing_low is not None:
            return

        timing_dt = local_dt.replace(
            hour=self.state.timing_hour,
            minute=self.state.timing_minute,
            second=0,
            microsecond=0,
        )
        if local_dt < timing_dt:
            return

        try:
            bar_minutes = core.timeframe_minutes(self.state.timeframe)
            rates = core.get_rates_range(
                self.state.mt5_symbol,
                self.state.timeframe,
                timing_dt.astimezone(timezone.utc),
                (timing_dt + timedelta(minutes=bar_minutes)).astimezone(timezone.utc),
            )
        except Exception:
            # A temporary history failure must not prevent normal processing;
            # the next completed candle will retry this recovery.
            return

        if rates is None or rates.empty:
            return
        matches = rates[
            rates["time"].map(
                lambda value: (
                    self._timing_local_dt(value).date() == timing_dt.date()
                    and self._timing_local_dt(value).hour == timing_dt.hour
                    and self._timing_local_dt(value).minute == timing_dt.minute
                )
            )
        ]
        if matches.empty:
            return

        timing_row = matches.iloc[-1]
        ss.timing_high = float(timing_row["high"])
        ss.timing_low = float(timing_row["low"])
        ss.timing_armed_buy = False
        ss.timing_armed_sell = False
        ss.timing_arm_time = None

    def _process_timing_retest_tick(self, ss: StrategyState, live_row=None):
        """Enter immediately when a future live price retests an armed level."""
        if (
            ss.strategy != "timing"
            or ss.execution.virtual_position != 0
            or not self.state.entries_enabled
            or not ss.execution.entries_enabled
            or not (ss.timing_armed_buy or ss.timing_armed_sell)
        ):
            return None

        tick = core.mt5.symbol_info_tick(self.state.mt5_symbol)
        if tick is None:
            return None
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0:
            return None

        decision = retest_signal(
            armed_buy=ss.timing_armed_buy,
            armed_sell=ss.timing_armed_sell,
            timing_high=ss.timing_high,
            timing_low=ss.timing_low,
            bar_key=None,
            arm_time=ss.timing_arm_time,
            live_bar_key=str(live_row["time"]) if live_row is not None else None,
            bid=bid,
            ask=ask,
        )
        if decision is None:
            return None

        side = decision.side
        entry_price = ask if side == "BUY" else bid
        sl = ss.timing_low if side == "BUY" else ss.timing_high
        if not self._place_timing_pending(ss, side, entry_price, sl):
            return None
        ss.execution.virtual_position = 1 if side == "BUY" else -1

        # A setup is consumed only after the broker/paper entry succeeds.
        ss.timing_armed_buy = False
        ss.timing_armed_sell = False
        return f"{DISPLAY_NAME['timing']}: {side}"

    def _process_timing_candle(self, ss: StrategyState, row):
        """Process the configurable India-time timing-candle strategy."""
        local_dt = self._timing_local_dt(row["time"])
        self._timing_reset_if_needed(ss, local_dt)
        close = float(row["close"]); high = float(row["high"]); low = float(row["low"])
        is_timing = local_dt.hour == self.state.timing_hour and local_dt.minute == self.state.timing_minute
        bar_key = str(row["time"])
        triggered = None

        # The timing candle itself only establishes the reference levels.
        if is_timing:
            ss.timing_high = high
            ss.timing_low = low
            ss.timing_armed_buy = False
            ss.timing_armed_sell = False
            ss.timing_arm_time = None
            return None

        self._restore_timing_levels(ss, local_dt)
        if ss.timing_high is None or ss.timing_low is None:
            return None

        arm = breakout_arm(
            close=close,
            timing_high=ss.timing_high,
            timing_low=ss.timing_low,
            bar_key=bar_key,
            already_armed_buy=ss.timing_armed_buy,
            already_armed_sell=ss.timing_armed_sell,
            position_flat=ss.execution.virtual_position == 0,
            entries_enabled=self.state.entries_enabled and ss.execution.entries_enabled,
        )
        if arm is not None:
            ss.timing_armed_buy = arm.side == "BUY"
            ss.timing_armed_sell = arm.side == "SELL"
            ss.timing_arm_time = bar_key

        return triggered

    def _run(self):
        engine = ChandraTrendEngine(self.state.mt5_symbol, self.state.timeframe, 3000)
        last_bar = None
        try:
            engine.initialize()
            while not self._stop.is_set():
                manual_ss = self._manual_state()
                if self.live_enabled:
                    self._sync_live_snapshot(manual_ss)
                else:
                    self._update_manual_paper()
                if self.live_enabled:
                    account = core.mt5.account_info()
                    tick = core.mt5.symbol_info_tick(self.state.mt5_symbol)
                    if account is None or tick is None:
                        if self.state.entries_enabled:
                            self.state.entries_enabled = False
                            self.state.safety_halt_reason = "MT5 connection lost — new entries disabled"
                            for ss0 in self.state.strategy_states.values():
                                ss0.execution.entries_enabled = False
                        time.sleep(1.0)
                        continue
                df = engine.calculate_frame()
                row = df.iloc[-1]
                bar_time = str(row["time"])

                # BO/BD is confirmed only on completed candles, but the retest
                # is a live-price event.  Check it every polling cycle so a
                # touch never has to wait for another candle close.
                timing_tick_trigger = None
                tss = self.state.strategy_states.get("timing")
                if tss is not None:
                    # ``row`` is the latest completed bar.  A tick observed
                    # now belongs to the following live bar, even while the
                    # breakout remains the last completed candle.
                    timing_tick_trigger = self._process_timing_retest_tick(tss)
                    if timing_tick_trigger and self.state.last_signal is not None:
                        self.state.last_signal["triggered"] = timing_tick_trigger
                        self.state.last_signal["timing_buy"] = bool(tss.timing_armed_buy)
                        self.state.last_signal["timing_sell"] = bool(tss.timing_armed_sell)
                        self.state.last_signal["timing_high"] = tss.timing_high
                        self.state.last_signal["timing_low"] = tss.timing_low
                if bar_time != last_bar:
                    signal = core.signal_from_row(row)
                    self.state.last_signal = {
                        "buy_condition": signal.buy_condition,
                        "sell_condition": signal.sell_condition,
                        "strategic_buy": signal.strategic_buy,
                        "strategic_sell": signal.strategic_sell,
                        "magical_buy": signal.magical_buy,
                        "magical_sell": signal.magical_sell,
                        "magical_invalid": signal.magical_invalid,
                        "triggered": None,
                        "close": signal.close,
                        "l100": signal.l100,
                        "time": bar_time,
                        "timing_buy": False,
                        "timing_sell": False,
                        "timing_high": None,
                        "timing_low": None,
                    }
                    triggered = []
                    if timing_tick_trigger:
                        triggered.append(timing_tick_trigger)
                    for name in self.state.strategies:
                        ss = self.state.strategy_states[name]
                        ss.last_l100 = float(signal.l100) if signal.l100 is not None else None
                        if not self.live_enabled:
                            self._paper_check_sl(ss, row)
                            ss.execution.virtual_position = ss.paper_position
                        else:
                            before_live = self._live_positions(ss.execution.magic_number)
                            self._sync_live_snapshot(ss)
                        before = ss.execution.virtual_position
                        if name == "timing":
                            if not self.live_enabled:
                                self._paper_check_sl(ss, row)
                                ss.execution.virtual_position = ss.paper_position
                            else:
                                self._sync_live_snapshot(ss)
                            timing_trigger = self._process_timing_candle(ss, row)
                            after = ss.execution.virtual_position
                            if timing_trigger:
                                triggered.append(timing_trigger)
                            # In paper mode, an SL hit is already handled above. Live mode
                            # relies on the broker-side timing-candle SL.
                            if not self.live_enabled and after != 0:
                                tick = core.mt5.symbol_info_tick(self.state.mt5_symbol)
                                if tick and ss.paper_entry_price is not None:
                                    current = float(tick.bid if after == 1 else tick.ask)
                                    ss.current_pnl = ((current - ss.paper_entry_price) if after == 1 else (ss.paper_entry_price - current)) * (ss.volume or self.state.lot) * self.state.contract_size
                            continue
                        if self.live_enabled and not before_live and ss.live_ticket:
                            # The broker may have hit SL/TP between polling bars.
                            # Record that exit before allowing a new signal to open.
                            self._record_live_close(ss)
                        core.process_candle(
                            self.state.mt5_symbol, row, signal, self.state.lot,
                            self.live_enabled, ss.execution
                        )
                        after = ss.execution.virtual_position
                        if not self.live_enabled:
                            if before == 0 and after != 0:
                                side = "BUY" if after == 1 else "SELL"
                                self._paper_open(ss, side, self._paper_price(side) or float(row["close"]), signal.l100)
                                triggered.append(f"{DISPLAY_NAME[name]}: {side}")
                            elif before != 0 and after == 0:
                                side = "BUY" if before == 1 else "SELL"
                                self._paper_close(ss, self._paper_price("SELL" if before == 1 else "BUY") or float(row["close"]), "SIGNAL EXIT")
                            elif before != after and before != 0 and after != 0:
                                side = "BUY" if after == 1 else "SELL"
                                self._paper_open(ss, side, self._paper_price(side) or float(row["close"]), signal.l100)
                                triggered.append(f"{DISPLAY_NAME[name]}: {side}")
                            elif after != 0:
                                # Strategic Entry keeps the initial L100 +/- 5 SL fixed.
                                # Magical retains its existing L100 synchronization.
                                if name == "magical":
                                    ss.paper_sl = self._paper_sl_from_l100("BUY" if after == 1 else "SELL", signal.l100)
                                tick = core.mt5.symbol_info_tick(self.state.mt5_symbol)
                                if tick and ss.paper_entry_price is not None:
                                    current = float(tick.bid if after == 1 else tick.ask)
                                    ss.current_pnl = ((current - ss.paper_entry_price) if after == 1 else (ss.paper_entry_price - current)) * (ss.volume or self.state.lot) * self.state.contract_size
                        else:
                            after_live = self._live_positions(ss.execution.magic_number)
                            if not before_live and after_live:
                                position = after_live[0]
                                side = "BUY" if position.type == core.mt5.POSITION_TYPE_BUY else "SELL"
                                ss.active_trade_id = uuid.uuid4().hex
                                ss.live_ticket = str(getattr(position, "ticket", ""))
                                self._record(
                                    name, side, "OPEN", float(position.price_open), "LIVE",
                                    message="LIVE — SENT TO BROKER", sl=float(position.sl) if getattr(position, "sl", 0) else None,
                                    target=float(position.tp) if getattr(position, "tp", 0) else None,
                                    volume=float(getattr(position, "volume", self.state.lot) or self.state.lot),
                                    trade_id=ss.active_trade_id, ticket=ss.live_ticket
                                )
                                triggered.append(f"{DISPLAY_NAME[name]}: {side}")
                            elif before_live and not after_live:
                                self._record_live_close(ss)
                            self._sync_live_snapshot(ss)
                    if "timing" in self.state.strategy_states:
                        tss = self.state.strategy_states["timing"]
                        self.state.last_signal["timing_buy"] = bool(tss.timing_armed_buy)
                        self.state.last_signal["timing_sell"] = bool(tss.timing_armed_sell)
                        self.state.last_signal["timing_high"] = tss.timing_high
                        self.state.last_signal["timing_low"] = tss.timing_low
                    self.state.last_signal["triggered"] = " • ".join(triggered) if triggered else None
                    last_bar = bar_time
                time.sleep(1.0)
        except Exception as exc:
            self.state.error = str(exc)
            self.state.running = False
        finally:
            try:
                core.mt5.shutdown()
            except Exception:
                pass

    def strategic_backtest(self, start_date: str, end_date: str, lot: float = 0.01, max_trades_per_day: int = 0, daily_target_points: float = 0.0, symbol: str | None = None, timeframe: str | None = None):
        """Run a deterministic Strategic Entry backtest on closed MT5 candles.

        Entry uses the Strategic Fib3 event on the signal candle at its close.
        Initial protection is L100 - 5 points for BUY / L100 + 5 points for SELL.
        Once an opposite BUY/SELL signal candle appears, its high/low becomes the
        dynamic reference; only a later candle close beyond that reference exits.
        """
        if self.state.running:
            raise RuntimeError("Stop the bot before running a backtest")
        try:
            start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1) - timedelta(microseconds=1)
        except ValueError as exc:
            raise ValueError("Invalid date range") from exc
        if end <= start:
            raise ValueError("To date must be on or after From date")
        if lot <= 0:
            raise ValueError("Lot size must be greater than zero")
        if max_trades_per_day < 0:
            raise ValueError("Max trades per day cannot be negative")
        if daily_target_points < 0:
            raise ValueError("Daily target points cannot be negative")
        test_symbol = (symbol or self.state.symbol).strip()
        test_timeframe = (timeframe or self.state.timeframe).strip().upper()
        if not test_symbol:
            raise ValueError("Instrument is required")
        core.timeframe_minutes(test_timeframe)

        if not core.mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {core.mt5.last_error()}")
        try:
            requested = test_symbol
            if core.mt5.symbol_info(requested) is not None:
                resolved_symbol = requested
            elif requested.upper() == "XAUUSD":
                candidates = ["XAUUSD.sd", "XAUUSDm", "XAUUSD.a", "XAUUSD.r"]
                resolved_symbol = next((c for c in candidates if core.mt5.symbol_info(c) is not None), None)
                if resolved_symbol is None:
                    matches = [s.name for s in (core.mt5.symbols_get() or []) if "XAUUSD" in s.name.upper()]
                    resolved_symbol = matches[0] if matches else None
            else:
                resolved_symbol = None
            if not resolved_symbol:
                raise RuntimeError(f"MT5 symbol not found for '{test_symbol}'")
            info = core.ensure_symbol(resolved_symbol)
            df = core.get_rates_range(resolved_symbol, test_timeframe, start, end)
            if len(df) < 2:
                raise RuntimeError("Not enough historical candles for this range")
            df = core.calculate_pine_engine(df)

            point = float(getattr(info, "point", 0.01) or 0.01)
            contract = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
            offset = 5.0 * point
            position = 0
            entry_price = None
            entry_time = None
            initial_sl = None
            reference = None
            reference_time = None
            trades = []
            equity = 0.0
            max_equity = 0.0
            max_drawdown = 0.0
            current_day = None
            daily_trade_count = 0
            daily_realized_points = 0.0
            daily_target_reached = False
            daily_stats = {}

            prev_strategic_buy = False
            prev_strategic_sell = False
            prev_sell_condition = False
            prev_buy_condition = False

            def close_trade(price, exit_time, reason):
                nonlocal position, entry_price, entry_time, initial_sl, reference, reference_time, equity, max_equity, max_drawdown
                if position == 0 or entry_price is None:
                    return
                side = "BUY" if position == 1 else "SELL"
                price_move_points = ((price - entry_price) if position == 1 else (entry_price - price))
                pnl = price_move_points * lot * contract
                equity += pnl
                max_equity = max(max_equity, equity)
                max_drawdown = max(max_drawdown, max_equity - equity)
                # Daily target is measured in price points, independent of lot/contract size.
                nonlocal daily_realized_points
                daily_realized_points += price_move_points
                day_key = str(exit_time)[:10]
                ds = daily_stats.setdefault(day_key, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "points": 0.0, "target_reached": False})
                ds["trades"] += 1
                ds["pnl"] += pnl
                ds["points"] += price_move_points
                if pnl > 0: ds["wins"] += 1
                elif pnl < 0: ds["losses"] += 1
                if daily_target_points > 0 and daily_realized_points >= daily_target_points:
                    daily_target_reached = True
                    ds["target_reached"] = True
                trades.append({
                    "side": side, "entry": round(entry_price, int(getattr(info, "digits", 2))),
                    "entry_time": str(entry_time), "exit": round(price, int(getattr(info, "digits", 2))),
                    "exit_time": str(exit_time), "reason": reason, "pnl": round(pnl, 2),
                    "initial_sl": round(initial_sl, int(getattr(info, "digits", 2))) if initial_sl is not None else None,
                    "reference": round(reference, int(getattr(info, "digits", 2))) if reference is not None else None,
                    "reference_time": str(reference_time) if reference_time is not None else None,
                })
                position = 0; entry_price = None; entry_time = None; initial_sl = None; reference = None; reference_time = None

            for _, row in df.iterrows():
                close = float(row["close"]); high = float(row["high"]); low = float(row["low"])
                t = row["time"]
                strategic_buy = bool(row["strategicBuy"])
                strategic_sell = bool(row["strategicSell"])
                new_buy = strategic_buy and not prev_strategic_buy
                new_sell = strategic_sell and not prev_strategic_sell
                new_sell_signal = bool(row["sellCondition"]) and not prev_sell_condition
                new_buy_signal = bool(row["buyCondition"]) and not prev_buy_condition

                day_key = str(t)[:10]
                if current_day != day_key:
                    current_day = day_key
                    daily_trade_count = 0
                    daily_realized_points = 0.0
                    daily_target_reached = False
                    daily_stats.setdefault(day_key, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "points": 0.0, "target_reached": False})

                if daily_target_points > 0 and daily_realized_points >= daily_target_points:
                    daily_target_reached = True
                    daily_stats[day_key]["target_reached"] = True

                # Initial broker-style protection: intrabar touch closes the trade.
                if position == 1 and initial_sl is not None and low <= initial_sl:
                    close_trade(initial_sl, t, "INITIAL_SL")
                elif position == -1 and initial_sl is not None and high >= initial_sl:
                    close_trade(initial_sl, t, "INITIAL_SL")

                # Opposite signal candle establishes reference; do not exit on same candle.
                if position == 1 and new_sell_signal:
                    reference = low; reference_time = t
                elif position == -1 and new_buy_signal:
                    reference = high; reference_time = t

                # Close-based dynamic exit only on a later candle.
                if position == 1 and reference is not None and t != reference_time and close < reference:
                    close_trade(close, t, "CLOSE_BELOW_SELL_SIGNAL_LOW")
                elif position == -1 and reference is not None and t != reference_time and close > reference:
                    close_trade(close, t, "CLOSE_ABOVE_BUY_SIGNAL_HIGH")

                # Fib3 entry. No L100 gate. Do not reverse an open position.
                # Daily controls affect NEW entries only; an open position remains managed.
                entries_blocked = daily_target_reached or (max_trades_per_day > 0 and daily_trade_count >= max_trades_per_day)
                if position == 0 and not entries_blocked:
                    if new_buy:
                        position = 1; entry_price = close; entry_time = t; initial_sl = float(row["l100"]) - offset if pd.notna(row["l100"]) else None; daily_trade_count += 1
                    elif new_sell:
                        position = -1; entry_price = close; entry_time = t; initial_sl = float(row["l100"]) + offset if pd.notna(row["l100"]) else None; daily_trade_count += 1

                prev_strategic_buy = strategic_buy; prev_strategic_sell = strategic_sell
                prev_sell_condition = bool(row["sellCondition"]); prev_buy_condition = bool(row["buyCondition"])

            if position != 0:
                last = df.iloc[-1]
                close_trade(float(last["close"]), last["time"], "END_OF_TEST")

            wins = sum(1 for t in trades if t["pnl"] > 0)
            losses = sum(1 for t in trades if t["pnl"] < 0)
            buys = sum(1 for t in trades if t["side"] == "BUY")
            sells = sum(1 for t in trades if t["side"] == "SELL")
            return {
                "ok": True, "strategy": "strategic", "symbol": resolved_symbol, "timeframe": test_timeframe,
                "from": start_date, "to": end_date, "candles": len(df), "trades": len(trades),
                "buy_trades": buys, "sell_trades": sells, "wins": wins, "losses": losses,
                "win_rate": round((wins / len(trades) * 100) if trades else 0, 2),
                "net_pnl": round(equity, 2), "max_drawdown": round(max_drawdown, 2),
                "best_trade": round(max((t["pnl"] for t in trades), default=0), 2),
                "worst_trade": round(min((t["pnl"] for t in trades), default=0), 2),
                "max_trades_per_day": max_trades_per_day,
                "daily_target_points": daily_target_points,
                "daily_stats": [
                    {"date": d, "trades": v["trades"], "wins": v["wins"], "losses": v["losses"],
                     "pnl": round(v["pnl"], 2), "points": round(v["points"], 2), "target_reached": v["target_reached"]}
                    for d, v in sorted(daily_stats.items())
                ],
                "profitable_days": sum(1 for v in daily_stats.values() if v["pnl"] > 0),
                "losing_days": sum(1 for v in daily_stats.values() if v["pnl"] < 0),
                "trading_days": len(daily_stats),
                "trades_detail": trades,
            }
        finally:
            try: core.mt5.shutdown()
            except Exception: pass

    def timing_backtest(self, start_date: str, end_date: str, lot: float = 0.01,
                        max_trades_per_day: int = 0, daily_target_points: float = 0.0,
                        symbol: str | None = None, timeframe: str | None = None,
                        timing_hour: int = 9, timing_minute: int = 30,
                        timing_end_time: str = "08:00"):
        """Backtest Timing Candle with optional daily end time and diagnostics.

        Timing Candle is evaluated in India time.  The optional ``end_time``
        limits new entries and closes any open Timing Candle position at that
        time.  Daily diagnostics explain why a day did not produce a trade.
        """
        if not (0 <= int(timing_hour) <= 23 and 0 <= int(timing_minute) <= 59):
            raise ValueError("Invalid timing candle time")

        end_time = timing_end_time
        if end_time is not None:
            try:
                end_hour, end_minute = [int(x) for x in str(end_time).strip().split(":", 1)]
            except (ValueError, TypeError):
                raise ValueError("Invalid Timing Candle end time. Use HH:MM.")
            if not (0 <= end_hour <= 23 and 0 <= end_minute <= 59):
                raise ValueError("Invalid Timing Candle end time. Use HH:MM.")
            timing_minutes = int(timing_hour) * 60 + int(timing_minute)
            end_minutes = end_hour * 60 + end_minute
            if end_minutes <= timing_minutes:
                raise ValueError("Timing Candle end time must be after the Timing Candle time")
        else:
            end_hour = end_minute = None

        try:
            local_start = datetime.fromisoformat(start_date).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            local_end = (
                datetime.fromisoformat(end_date).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                + timedelta(days=1) - timedelta(microseconds=1)
            )
        except ValueError as exc:
            raise ValueError("Invalid date range") from exc

        if local_end <= local_start:
            raise ValueError("To date must be on or after From date")
        if lot <= 0:
            raise ValueError("Lot size must be greater than zero")
        if max_trades_per_day < 0:
            raise ValueError("Max trades per day cannot be negative")
        if daily_target_points < 0:
            raise ValueError("Daily target points cannot be negative")

        test_symbol = (symbol or self.state.symbol).strip()
        test_timeframe = (timeframe or self.state.timeframe).strip().upper()
        core.timeframe_minutes(test_timeframe)

        if not core.mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {core.mt5.last_error()}")

        try:
            resolved_symbol = test_symbol
            if core.mt5.symbol_info(resolved_symbol) is None and resolved_symbol.upper() == "XAUUSD":
                candidates = ["XAUUSD.sd", "XAUUSDm", "XAUUSD.a", "XAUUSD.r"]
                resolved_symbol = next(
                    (candidate for candidate in candidates if core.mt5.symbol_info(candidate) is not None),
                    None,
                )
                if resolved_symbol is None:
                    matches = [
                        item.name for item in (core.mt5.symbols_get() or [])
                        if "XAUUSD" in item.name.upper()
                    ]
                    resolved_symbol = matches[0] if matches else None

            if not resolved_symbol:
                raise RuntimeError(f"MT5 symbol not found for '{test_symbol}'")

            info = core.ensure_symbol(resolved_symbol)
            start_utc = local_start.astimezone(timezone.utc)
            end_utc = local_end.astimezone(timezone.utc)
            df = core.get_rates_range(resolved_symbol, test_timeframe, start_utc, end_utc)
            if len(df) < 2:
                raise RuntimeError("Not enough historical candles for this range")

            point = float(getattr(info, "point", 0.01) or 0.01)
            contract = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
            digits = int(getattr(info, "digits", 2) or 2)

            position = 0
            entry = None
            entry_time = None
            sl = None
            trades = []
            equity = 0.0
            peak = 0.0
            dd = 0.0

            timing_day = None
            th = None
            tl = None
            armed_buy = False
            armed_sell = False
            arm_time = None
            day_count = 0
            day_points = 0.0
            target_hit = False
            daily = {}
            day_diag = {}

            def close_trade(price, t, reason):
                nonlocal position, entry, entry_time, sl, equity, peak, dd, day_points, target_hit
                if position == 0 or entry is None:
                    return

                side = "BUY" if position == 1 else "SELL"
                move = (price - entry) if position == 1 else (entry - price)
                pnl = move * lot * contract
                equity += pnl
                peak = max(peak, equity)
                dd = max(dd, peak - equity)
                day_points += move

                dk = self._timing_local_dt(t).date().isoformat()
                ds = daily.setdefault(
                    dk,
                    {
                        "trades": 0,
                        "wins": 0,
                        "losses": 0,
                        "pnl": 0.0,
                        "points": 0.0,
                        "target_reached": False,
                    },
                )
                ds["trades"] += 1
                ds["pnl"] += pnl
                ds["points"] += move
                if pnl > 0:
                    ds["wins"] += 1
                elif pnl < 0:
                    ds["losses"] += 1

                if daily_target_points > 0 and day_points >= daily_target_points:
                    target_hit = True
                    ds["target_reached"] = True

                trades.append(
                    {
                        "side": side,
                        "entry": round(entry, digits),
                        "entry_time": str(entry_time),
                        "exit": round(price, digits),
                        "exit_time": str(t),
                        "reason": reason,
                        "pnl": round(pnl, 2),
                        "initial_sl": round(sl, digits) if sl is not None else None,
                        "reference": round(th if side == "BUY" else tl, digits)
                        if (th if side == "BUY" else tl) is not None
                        else None,
                    }
                )

                position = 0
                entry = None
                entry_time = None
                sl = None

            for _, row in df.iterrows():
                t = row["time"]
                local = self._timing_local_dt(t)
                dk = local.date().isoformat()
                close = float(row["close"])
                high = float(row["high"])
                low = float(row["low"])

                if timing_day != dk:
                    timing_day = dk
                    th = None
                    tl = None
                    armed_buy = False
                    armed_sell = False
                    arm_time = None
                    day_count = 0
                    day_points = 0.0
                    target_hit = False
                    daily.setdefault(
                        dk,
                        {
                            "trades": 0,
                            "wins": 0,
                            "losses": 0,
                            "pnl": 0.0,
                            "points": 0.0,
                            "target_reached": False,
                        },
                    )
                    day_diag.setdefault(
                        dk,
                        {
                            "timing_found": False,
                            "timing_time": None,
                            "timing_high": None,
                            "timing_low": None,
                            "breakout": None,
                            "breakout_time": None,
                            "retest": None,
                            "retest_time": None,
                            "reason": "NO TIMING CANDLE",
                        },
                    )

                # The optional daily end time is checked before processing a candle.
                # Therefore the end-time candle itself cannot create a new entry.
                if end_hour is not None:
                    current_minutes = local.hour * 60 + local.minute
                    end_minutes = end_hour * 60 + end_minute
                    if current_minutes >= end_minutes:
                        if position != 0:
                            close_trade(close, t, "END_TIME")
                        diag = day_diag.setdefault(dk, {})
                        if diag.get("reason") in (
                            "NO TIMING CANDLE",
                            "BREAKOUT NOT FOUND",
                            "RETEST NOT FOUND",
                        ):
                            diag["reason"] = "END TIME REACHED"
                        armed_buy = False
                        armed_sell = False
                        continue

                # Broker-style SL: intrabar touch closes the position.
                if position == 1 and sl is not None and low <= sl:
                    close_trade(sl, t, "TIMING_CANDLE_SL")
                elif position == -1 and sl is not None and high >= sl:
                    close_trade(sl, t, "TIMING_CANDLE_SL")

                is_timing = local.hour == int(timing_hour) and local.minute == int(timing_minute)
                if is_timing:
                    th = high
                    tl = low
                    armed_buy = False
                    armed_sell = False
                    arm_time = str(t)
                    day_diag[dk].update(
                        {
                            "timing_found": True,
                            "timing_time": str(t),
                            "timing_high": th,
                            "timing_low": tl,
                            "reason": "WAITING FOR BREAKOUT",
                        }
                    )
                    continue

                if th is None or tl is None:
                    continue

                # A CLOSED candle beyond the timing level arms the breakout.
                # The breakout candle itself cannot be the retest/entry candle.
                if close > th:
                    if not armed_buy:
                        armed_buy = True
                        armed_sell = False
                        arm_time = str(t)
                        day_diag[dk].update(
                            {
                                "breakout": "BUY",
                                "breakout_time": str(t),
                                "reason": "WAITING FOR RETEST",
                            }
                        )
                elif close < tl:
                    if not armed_sell:
                        armed_sell = True
                        armed_buy = False
                        arm_time = str(t)
                        day_diag[dk].update(
                            {
                                "breakout": "SELL",
                                "breakout_time": str(t),
                                "reason": "WAITING FOR RETEST",
                            }
                        )

                if arm_time == str(t):
                    continue

                blocked = target_hit or (
                    max_trades_per_day > 0 and day_count >= max_trades_per_day
                )
                if position == 0 and not blocked:
                    if armed_buy and low <= th and close >= th:
                        position = 1
                        entry = th
                        entry_time = t
                        sl = tl
                        day_count += 1
                        armed_buy = False
                        day_diag[dk].update(
                            {
                                "retest": "BUY",
                                "retest_time": str(t),
                                "reason": "RETEST FOUND",
                            }
                        )
                    elif armed_sell and high >= tl and close <= tl:
                        position = -1
                        entry = tl
                        entry_time = t
                        sl = th
                        day_count += 1
                        armed_sell = False
                        day_diag[dk].update(
                            {
                                "retest": "SELL",
                                "retest_time": str(t),
                                "reason": "RETEST FOUND",
                            }
                        )

            # Do not carry a position beyond the requested test window.
            if position != 0:
                last = df.iloc[-1]
                close_trade(float(last["close"]), last["time"], "END_OF_TEST")

            # Give every requested trading day an explicit diagnostic, including
            # days with zero trades.
            all_days = sorted(set(daily) | set(day_diag))
            for d in all_days:
                day_diag.setdefault(
                    d,
                    {
                        "timing_found": False,
                        "timing_time": None,
                        "timing_high": None,
                        "timing_low": None,
                        "breakout": None,
                        "breakout_time": None,
                        "retest": None,
                        "retest_time": None,
                        "reason": "NO DATA",
                    },
                )
                ds = daily.setdefault(
                    d,
                    {
                        "trades": 0,
                        "wins": 0,
                        "losses": 0,
                        "pnl": 0.0,
                        "points": 0.0,
                        "target_reached": False,
                    },
                )
                if ds["trades"] == 0 and day_diag[d].get("reason") == "WAITING FOR BREAKOUT":
                    day_diag[d]["reason"] = "BREAKOUT NOT FOUND"
                elif ds["trades"] == 0 and day_diag[d].get("reason") == "WAITING FOR RETEST":
                    day_diag[d]["reason"] = "RETEST NOT FOUND"

            wins = sum(t["pnl"] > 0 for t in trades)
            losses = sum(t["pnl"] < 0 for t in trades)

            return {
                "ok": True,
                "strategy": "timing",
                "symbol": resolved_symbol,
                "timeframe": test_timeframe,
                "from": start_date,
                "to": end_date,
                "candles": len(df),
                "timing_hour": timing_hour,
                "timing_minute": timing_minute,
                "end_time": end_time,
                "trades": len(trades),
                "buy_trades": sum(t["side"] == "BUY" for t in trades),
                "sell_trades": sum(t["side"] == "SELL" for t in trades),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / len(trades) * 100 if trades else 0, 2),
                "net_pnl": round(equity, 2),
                "max_drawdown": round(dd, 2),
                "best_trade": round(max((t["pnl"] for t in trades), default=0), 2),
                "worst_trade": round(min((t["pnl"] for t in trades), default=0), 2),
                "max_trades_per_day": max_trades_per_day,
                "daily_target_points": daily_target_points,
                "daily_stats": [
                    {
                        "date": d,
                        **{
                            k: round(v, 2) if isinstance(v, float) else v
                            for k, v in x.items()
                        },
                        "diagnostic":day_diag.get(d,{}),
                    }
                    for d, x in sorted(daily.items())
                ],
                "profitable_days": sum(v["pnl"] > 0 for v in daily.values()),
                "losing_days": sum(v["pnl"] < 0 for v in daily.values()),
                "trading_days": len(daily),
                "trades_detail": trades,
            }
        finally:
            try:
                core.mt5.shutdown()
            except Exception:
                pass

    def status_payload(self):
        data = dict(self.state.__dict__)
        strategy_status = {}
        for name, ss in self.state.strategy_states.items():
            strategy_status[name] = {
                "name": DISPLAY_NAME[name],
                "position": ss.paper_position,
                "entry_price": ss.paper_entry_price,
                "entry_side": ss.paper_entry_side,
                "sl": ss.paper_sl,
                "l100": ss.last_l100,
                "pnl": ss.current_pnl,
                "realized_pnl": ss.realized_pnl,
                "total_pnl": ss.realized_pnl + ss.current_pnl,
                "timing_high": ss.timing_high,
                "timing_low": ss.timing_low,
                "timing_armed_buy": ss.timing_armed_buy,
                "timing_armed_sell": ss.timing_armed_sell,
            }
        manual = self._manual_state()
        strategy_status["manual"] = {
            "name": "Manual Order",
            "position": manual.paper_position,
            "entry_price": manual.paper_entry_price,
            "entry_side": manual.paper_entry_side,
            "sl": manual.paper_sl,
            "target": manual.target,
            "pnl": manual.current_pnl,
            "realized_pnl": manual.realized_pnl,
            "total_pnl": manual.realized_pnl + manual.current_pnl,
        }
        data["strategy_status"] = strategy_status
        data["combined_pnl"] = sum(v["total_pnl"] for v in strategy_status.values())
        data.pop("strategy_states", None)
        data.pop("manual_state", None)
        return data

manager = BotManager()
