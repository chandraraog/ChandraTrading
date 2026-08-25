import json
import pandas as pd
import threading
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from backend.app.engine.chandra_trend_engine import ChandraTrendEngine
from backend.app.engine import chandra_core as core
from backend.app.licensing.license_service import LicenseService
from backend.app.strategies.timing_candle import breakout_arm, retest_signal

# Website V1 enables the engine's L100-based initial/trailing SL.
core.STOPLOSS_CONFIG["enabled"] = True
core.STOPLOSS_CONFIG["offset_points"] = 5.0

STRATEGIES = ("strategic", "magical", "timing")
MAGIC_BY_STRATEGY = {"strategic": 26081201, "magical": 26081202, "timing": 26081203, "manual": 26081204}
ENGINE_MODE = {"strategic": "buy_sell", "magical": "magical", "timing": "timing"}
DISPLAY_NAME = {"strategic": "Strategic Entry", "magical": "Magical Entry", "timing": "Timing Candle"}
# Runtime data belongs to this project, not its parent directory.
# bot_manager.py -> app -> backend -> project root
from backend.app.runtime_paths import app_root

PROJECT_ROOT = app_root()
DATA_DIR = PROJECT_ROOT / "data"

HISTORY_FILE = DATA_DIR / "trade_history.json"
TIMING_RUNTIME_FILE = DATA_DIR / "timing_runtime_state.json"

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
    timing_session_blocked: bool = False
    timing_session_end: str | None = None
    target: float | None = None
    volume: float = 0.0
    active_trade_id: str | None = None
    live_ticket: str | None = None
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
    timing_hour: int = 4
    timing_minute: int = 0
    timing_end_time: str | None = "08:00"
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

    def configure(self, strategies, symbol, timeframe, lot, mode, timing_hour=4, timing_minute=0, timing_end_time="08:00"):
        if isinstance(strategies, str):
            strategies = [strategies]
        strategies = list(dict.fromkeys(strategies))
        # Empty strategy selection is a valid SAVED configuration.
        # The bot itself must still have at least one strategy before START.
        if any(s not in STRATEGIES for s in strategies):
            raise ValueError("Invalid strategy selection")
        if mode not in ("paper", "live"):
            raise ValueError("Invalid trading mode")
        if not (0 <= int(timing_hour) <= 23 and 0 <= int(timing_minute) <= 59):
            raise ValueError("Invalid timing candle time")
        timing_end_time = (str(timing_end_time).strip() if timing_end_time is not None else "") or None
        if timing_end_time is not None:
            try:
                eh, em = [int(x) for x in timing_end_time.split(":", 1)]
                if not (0 <= eh <= 23 and 0 <= em <= 59):
                    raise ValueError
                timing_end_time = f"{eh:02d}:{em:02d}"
            except Exception as exc:
                raise ValueError("Invalid Timing Candle end time. Use HH:MM or leave blank for broker session close") from exc
        if timing_end_time is not None:
            start_minutes = int(timing_hour) * 60 + int(timing_minute)
            end_minutes = int(timing_end_time[:2]) * 60 + int(timing_end_time[3:])
            if end_minutes <= start_minutes:
                raise ValueError("Timing Candle end time must be after the Timing Candle time")
        if self.state.running:
            raise RuntimeError("Stop the bot before changing configuration")
        self.state.strategies = strategies
        self.state.symbol = symbol
        self.state.timeframe = timeframe
        self.state.lot = lot
        self.state.mode = mode
        self.state.timing_hour = int(timing_hour)
        self.state.timing_minute = int(timing_minute)
        self.state.timing_end_time = timing_end_time
        self.state.entries_enabled = True
        self.state.safety_halt_reason = None
        self._clear_timing_runtime()
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

    def close_strategy(self, strategy: str, reason: str = "MANUAL CLOSE"):
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
            self._paper_close(ss, price, reason)
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
        if not self.state.strategies:
            raise RuntimeError("Select at least one strategy before starting the bot")
        # License is checked immediately before any trading thread starts.
        license_info = LicenseService().require()
        result = self.mt5_test()
        if not result.get("connected"):
            raise RuntimeError(result.get("error", "MT5 connection failed"))
        self._stop.clear()
        self.state.error = None
        self.state.license_status = license_info
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
        # A normal bot STOP must remove broker-side Timing LIMIT orders so no
        # unmanaged pending entry can execute after the bot is stopped.
        if self.live_enabled:
            ss = self.state.strategy_states.get("timing")
            if ss is not None and ss.timing_pending_ticket:
                self._cancel_timing_pending(ss, "BOT STOP")
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

    def _clear_timing_runtime(self):
        try:
            if TIMING_RUNTIME_FILE.exists():
                TIMING_RUNTIME_FILE.unlink()
        except Exception:
            pass

    def _save_timing_runtime(self, ss: StrategyState):
        """Persist Timing Candle setup state so a Python restart does not lose an armed BO/BD."""
        if ss.strategy != "timing":
            return
        try:
            TIMING_RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "date": ss.timing_date,
                "symbol": self.state.symbol,
                "timeframe": self.state.timeframe,
                "timing_hour": self.state.timing_hour,
                "timing_minute": self.state.timing_minute,
                "timing_end_time": self.state.timing_end_time,
                "timing_high": ss.timing_high,
                "timing_low": ss.timing_low,
                "timing_armed_buy": bool(ss.timing_armed_buy),
                "timing_armed_sell": bool(ss.timing_armed_sell),
                "timing_arm_time": ss.timing_arm_time,
                "timing_session_blocked": bool(ss.timing_session_blocked),
                "timing_session_end": ss.timing_session_end,
                "timing_pending_ticket": ss.timing_pending_ticket,
                "timing_pending_side": ss.timing_pending_side,
                "timing_pending_price": ss.timing_pending_price,
                "timing_pending_sl": ss.timing_pending_sl,
            }
            TIMING_RUNTIME_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_timing_runtime(self, ss: StrategyState):
        """Restore today's Timing Candle reference/setup after a backend restart."""
        if ss.strategy != "timing" or not TIMING_RUNTIME_FILE.exists():
            return
        try:
            data = json.loads(TIMING_RUNTIME_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            if data.get("date") != ss.timing_date:
                return
            if data.get("symbol") != self.state.symbol or data.get("timeframe") != self.state.timeframe:
                return
            if int(data.get("timing_hour", -1)) != int(self.state.timing_hour):
                return
            if int(data.get("timing_minute", -1)) != int(self.state.timing_minute):
                return
            ss.timing_high = float(data["timing_high"]) if data.get("timing_high") is not None else None
            ss.timing_low = float(data["timing_low"]) if data.get("timing_low") is not None else None
            ss.timing_armed_buy = bool(data.get("timing_armed_buy", False))
            ss.timing_armed_sell = bool(data.get("timing_armed_sell", False))
            ss.timing_arm_time = data.get("timing_arm_time")
            ss.timing_session_blocked = bool(data.get("timing_session_blocked", False))
            ss.timing_session_end = data.get("timing_session_end")
            ss.timing_pending_ticket = str(data.get("timing_pending_ticket")) if data.get("timing_pending_ticket") else None
            ss.timing_pending_side = data.get("timing_pending_side")
            ss.timing_pending_price = float(data["timing_pending_price"]) if data.get("timing_pending_price") is not None else None
            ss.timing_pending_sl = float(data["timing_pending_sl"]) if data.get("timing_pending_sl") is not None else None
            print(
                f"TIMING STATE RECOVERED | H={ss.timing_high} L={ss.timing_low} | "
                f"BUY_ARMED={ss.timing_armed_buy} SELL_ARMED={ss.timing_armed_sell}"
            )
        except Exception as exc:
            print(f"TIMING STATE RECOVERY ERROR | {exc}")

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

    def _find_open_trade_by_ticket(self, ticket):
        """Return the persisted OPEN trade for a broker ticket, if one exists."""
        if ticket is None:
            return None
        ticket = str(ticket)
        with self._lock:
            for item in self.state.trades:
                if item.get("action") != "OPEN":
                    continue
                if str(item.get("ticket") or "") != ticket:
                    continue
                trade_id = item.get("trade_id")
                if not trade_id:
                    continue
                # A matching CLOSE means this is no longer the active history row.
                closed = any(
                    x.get("action") == "CLOSE" and
                    str(x.get("trade_id") or "") == str(trade_id)
                    for x in self.state.trades
                )
                if not closed:
                    return item
        return None

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
            # A pending Timing LIMIT may have just filled. Move ownership from
            # the pending ticket to the real MT5 position ticket and record the
            # OPEN exactly once.
            if ss.strategy == "timing" and ss.timing_pending_ticket:
                ss.timing_pending_ticket = None
                ss.timing_pending_side = None
                ss.timing_pending_price = None
                ss.timing_pending_sl = None
                ss.timing_armed_buy = False
                ss.timing_armed_sell = False
                ss.timing_arm_time = None
                self._save_timing_runtime(ss)
            # Keep execution state synchronized with the broker position.
            # This allows Timing Candle to re-enter after a broker-side SL.
            ss.execution.virtual_position = 1 if side == "BUY" else -1

            # Restart-safe recovery: reuse the existing OPEN trade record when
            # this broker ticket is already in trade_history.json. Never create a
            # duplicate OPEN entry just because the Python process restarted.
            if ss.strategy != "manual":
                existing = self._find_open_trade_by_ticket(ss.live_ticket)
                if existing:
                    ss.active_trade_id = str(existing.get("trade_id"))
                    print(
                        f"LIVE POSITION RECOVERED | strategy={ss.strategy} "
                        f"ticket={ss.live_ticket} trade_id={ss.active_trade_id}"
                    )
                elif ss.active_trade_id:
                    # This is normally a pending-limit fill. The trade_id was
                    # reserved when the pending order was placed. Record OPEN now
                    # that MT5 confirms the position actually exists.
                    self._record(
                        ss.strategy, side, "OPEN", ss.paper_entry_price, "LIVE",
                        message="LIVE — TIMING LIMIT FILLED" if ss.strategy == "timing" else "LIVE — POSITION RECOVERED",
                        sl=ss.paper_sl, target=ss.target,
                        volume=ss.volume or self.state.lot, trade_id=ss.active_trade_id, ticket=ss.live_ticket
                    )
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
            # A broker-side SL/TP/manual close only ends the current trade.
            # It must NOT disable the strategy or its future BO/BD entries.
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

    def _timing_broker_dt(self, value):
        """Return the MT5 candle timestamp without timezone conversion.

        Timing Candle uses the same clock exposed by MT5 for Paper/Live and
        Backtest. The timestamp is treated as the broker/server wall-clock
        value exactly as returned by the engine. No IST/UTC conversion is
        performed here.
        """
        if isinstance(value, pd.Timestamp):
            value = value.to_pydatetime()
        if value.tzinfo is not None:
            return value.replace(tzinfo=None)
        return value

    # Backward-compatible internal alias. Existing strategy code can continue
    # calling the old helper while all Timing Candle timestamps now follow the
    # broker/server clock with no timezone conversion.
    _timing_local_dt = _timing_broker_dt

    def _timing_reset_if_needed(self, ss: StrategyState, local_dt):
        day = local_dt.date().isoformat()
        if ss.timing_date != day:
            if self.live_enabled and ss.timing_pending_ticket:
                self._cancel_timing_pending(ss, "NEW BROKER DATE")
            ss.timing_date = day
            ss.timing_high = None
            ss.timing_low = None
            ss.timing_armed_buy = False
            ss.timing_armed_sell = False
            ss.timing_arm_time = None
            ss.timing_session_blocked = False
            ss.timing_session_end = None
            ss.timing_pending_ticket = None
            ss.timing_pending_side = None
            ss.timing_pending_price = None
            ss.timing_pending_sl = None
            self._save_timing_runtime(ss)

    def _broker_session_end_from_history(self, df, current_day):
        """Infer broker session end from MT5 server candle timestamps only.

        MetaTrader5's Python package does not expose the MQL5
        SymbolInfoSessionTrade() session schedule directly. We therefore use the
        timestamps returned by MT5 and take the most recent completed session for
        the same weekday. This avoids the PC/local clock and avoids hard-coding a
        market close.
        """
        try:
            times = [self._timing_local_dt(v) for v in df["time"].tail(10000)]
            candidates = [
                t for t in times
                if t.date() < current_day and t.weekday() == current_day.weekday()
            ]
            if not candidates:
                candidates = [t for t in times if t.date() < current_day]
            if not candidates:
                return None

            previous_day = max(t.date() for t in candidates)
            last_bar = max(t for t in candidates if t.date() == previous_day)
            minutes = max(1, int(core.timeframe_minutes(self.state.timeframe)))
            return last_bar + timedelta(minutes=minutes)
        except Exception:
            return None

    def _timing_session_control(self, ss: StrategyState, local_dt, df):
        """Apply optional user end time or broker-derived session cutoff.

        User end time: stop entries and close Timing Candle exactly at that time.
        No user end time: derive broker session end from MT5 candle timestamps and
        stop entries/close open Timing Candle 5 minutes before that broker session end.
        """
        self._timing_reset_if_needed(ss, local_dt)
        day = local_dt.date().isoformat()

        if ss.timing_session_end is None:
            if self.state.timing_end_time:
                eh, em = [int(x) for x in self.state.timing_end_time.split(":")]
                session_end = local_dt.replace(hour=eh, minute=em, second=0, microsecond=0)
            else:
                session_end = self._broker_session_end_from_history(df, local_dt.date())

            if session_end is not None:
                ss.timing_session_end = session_end.isoformat(timespec="minutes")

        if not ss.timing_session_end:
            return False

        session_end = datetime.fromisoformat(ss.timing_session_end)
        cutoff = session_end if self.state.timing_end_time else session_end - timedelta(minutes=5)
        if local_dt >= cutoff:
            if not ss.timing_session_blocked:
                ss.timing_session_blocked = True
                ss.timing_armed_buy = False
                ss.timing_armed_sell = False
                self._cancel_timing_pending(ss, "SESSION END")
                self._save_timing_runtime(ss)
                reason = ("TIMING END TIME" if self.state.timing_end_time
                          else "BROKER SESSION CLOSE - 5 MIN")
                if ss.execution.virtual_position != 0 or ss.paper_position != 0 or self._live_positions(ss.execution.magic_number):
                    try:
                        result = self.close_strategy("timing", reason=reason)
                        print(f"TIMING SESSION CLOSE | {reason} | {result.get('message', '')}")
                    except Exception as exc:
                        print(f"TIMING SESSION CLOSE ERROR | {reason} | {exc}")
                else:
                    print(f"TIMING SESSION ENDED | {reason}")
            return True
        return False

    def _cancel_timing_pending(self, ss: StrategyState, reason: str):
        ticket = ss.timing_pending_ticket
        if ticket:
            try:
                core.cancel_pending_order(ticket, self.live_enabled)
            except Exception as exc:
                print(f"TIMING PENDING CANCEL ERROR | ticket={ticket} | {exc}")
        if ticket:
            print(f"TIMING PENDING ORDER CLEARED | ticket={ticket} | reason={reason}")
        ss.timing_pending_ticket = None
        ss.timing_pending_side = None
        ss.timing_pending_price = None
        ss.timing_pending_sl = None
        self._save_timing_runtime(ss)

    def _place_timing_pending(self, ss: StrategyState):
        """Place a broker-side LIMIT order immediately after BO/BD is armed."""
        if ss.strategy != "timing" or not self.live_enabled:
            return False
        if ss.timing_pending_ticket or ss.execution.virtual_position != 0:
            return False
        side = "BUY" if ss.timing_armed_buy else "SELL" if ss.timing_armed_sell else None
        if side is None:
            return False
        level = ss.timing_high if side == "BUY" else ss.timing_low
        sl = ss.timing_low if side == "BUY" else ss.timing_high
        if level is None or sl is None:
            return False
        tick = core.mt5.symbol_info_tick(self.state.mt5_symbol)
        if tick is None:
            print("TIMING PENDING ORDER -> no market tick")
            return False
        market = float(tick.ask if side == "BUY" else tick.bid)
        # For a retest, the LIMIT must be behind the current market.
        if side == "BUY" and level >= market:
            print(f"TIMING BUY LIMIT NOT PLACED | level={level} current_ask={market}")
            return False
        if side == "SELL" and level <= market:
            print(f"TIMING SELL LIMIT NOT PLACED | level={level} current_bid={market}")
            return False
        result = core.place_limit_order(
            self.state.mt5_symbol, side, self.state.lot, level, sl, True,
            ss.execution.magic_number,
        )
        if not result:
            # Consume this exact failed setup; do not retry every tick.
            ss.timing_armed_buy = False
            ss.timing_armed_sell = False
            ss.timing_arm_time = None
            self._save_timing_runtime(ss)
            print(f"TIMING {side} LIMIT REJECTED -> setup consumed; waiting for next BO/BD")
            return False
        ss.timing_pending_ticket = result.get("ticket")
        ss.timing_pending_side = side
        ss.timing_pending_price = float(result.get("price"))
        ss.timing_pending_sl = float(result.get("sl")) if result.get("sl") is not None else None
        ss.active_trade_id = uuid.uuid4().hex
        self._save_timing_runtime(ss)
        print(
            f"TIMING {side} LIMIT ARMED | ENTRY={ss.timing_pending_price:.3f} "
            f"SL={ss.timing_pending_sl:.3f}"
        )
        return True

    def _sync_timing_pending(self, ss: StrategyState):
        """Recover/validate the broker-side Timing Candle pending order."""
        if ss.strategy != "timing" or not self.live_enabled:
            return
        try:
            orders = core.get_pending_orders(self.state.mt5_symbol, ss.execution.magic_number)
            timing_orders = [o for o in orders if getattr(o, "type", None) in (
                getattr(core.mt5, "ORDER_TYPE_BUY_LIMIT", -999),
                getattr(core.mt5, "ORDER_TYPE_SELL_LIMIT", -998),
            )]
            if ss.timing_pending_ticket:
                match = next((o for o in timing_orders if str(getattr(o, "ticket", "")) == str(ss.timing_pending_ticket)), None)
                if match is not None:
                    ss.timing_pending_price = float(getattr(match, "price_open", ss.timing_pending_price or 0.0))
                    ss.timing_pending_sl = float(getattr(match, "sl", ss.timing_pending_sl or 0.0)) if getattr(match, "sl", 0) else ss.timing_pending_sl
                else:
                    # Order was filled, cancelled, or removed. A live position sync
                    # below will take ownership if it was filled.
                    if not self._live_positions(ss.execution.magic_number):
                        print(f"TIMING PENDING GONE | ticket={ss.timing_pending_ticket}")
                        ss.timing_pending_ticket = None
                        ss.timing_pending_side = None
                        ss.timing_pending_price = None
                        ss.timing_pending_sl = None
                        ss.active_trade_id = None
                        ss.timing_armed_buy = False
                        ss.timing_armed_sell = False
                        ss.timing_arm_time = None
                        self._save_timing_runtime(ss)
            elif timing_orders:
                o = timing_orders[0]
                side = "BUY" if getattr(o, "type", None) == core.mt5.ORDER_TYPE_BUY_LIMIT else "SELL"
                ss.timing_pending_ticket = str(getattr(o, "ticket", ""))
                ss.timing_pending_side = side
                ss.timing_pending_price = float(getattr(o, "price_open", 0.0))
                ss.timing_pending_sl = float(getattr(o, "sl", 0.0)) if getattr(o, "sl", 0) else None
                ss.active_trade_id = ss.active_trade_id or uuid.uuid4().hex
                ss.timing_armed_buy = side == "BUY"
                ss.timing_armed_sell = side == "SELL"
                self._save_timing_runtime(ss)
                print(f"TIMING PENDING RECOVERED | ticket={ss.timing_pending_ticket} side={side} price={ss.timing_pending_price}")
        except Exception as exc:
            print(f"TIMING PENDING SYNC ERROR | {exc}")

    def _process_timing_candle(self, ss: StrategyState, row):
        """Process one COMPLETED candle for Timing Candle BO/BD detection.

        Strategy decisions are delegated to strategies.timing_candle.  This method
        only supplies runtime state and persists the resulting arm state.
        """
        local_dt = self._timing_local_dt(row["time"])
        self._timing_reset_if_needed(ss, local_dt)
        if hasattr(self, "_timing_session_df"):
            self._timing_session_control(ss, local_dt, self._timing_session_df)

        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        is_timing = (
            local_dt.hour == self.state.timing_hour
            and local_dt.minute == self.state.timing_minute
        )
        bar_key = str(row["time"])

        if is_timing:
            # RAW price values are stored. Formatting below is logging only.
            print(
                f"TIMING CANDLE CLOSED | {local_dt.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"O={float(row['open']):.3f} H={high:.3f} L={low:.3f} C={close:.3f}"
            )
            print(f"TIMING LEVELS LOCKED | HIGH={high:.3f} LOW={low:.3f}")
            ss.timing_high = high
            ss.timing_low = low
            ss.timing_armed_buy = False
            ss.timing_armed_sell = False
            ss.timing_arm_time = None
            self._save_timing_runtime(ss)
            return None

        if ss.timing_high is None or ss.timing_low is None:
            return None

        decision = breakout_arm(
            close=close,
            timing_high=ss.timing_high,
            timing_low=ss.timing_low,
            bar_key=bar_key,
            already_armed_buy=ss.timing_armed_buy,
            already_armed_sell=ss.timing_armed_sell,
            position_flat=(ss.execution.virtual_position == 0),
            entries_enabled=(self.state.entries_enabled and ss.execution.entries_enabled and not ss.timing_session_blocked),
        )
        if decision is not None:
            # If a new opposite breakout appears, remove any older pending LIMIT
            # before arming the new side.
            if self.live_enabled and ss.timing_pending_ticket:
                if (decision.side == "BUY" and ss.timing_pending_side == "SELL") or (decision.side == "SELL" and ss.timing_pending_side == "BUY"):
                    self._cancel_timing_pending(ss, "OPPOSITE BREAKOUT")
            ss.timing_armed_buy = decision.side == "BUY"
            ss.timing_armed_sell = decision.side == "SELL"
            ss.timing_arm_time = bar_key
            self._save_timing_runtime(ss)
            print(f"TIMING {decision.side} ARMED | {decision.reason}")
            if self.live_enabled:
                self._place_timing_pending(ss)

        return None

    def _process_timing_retest_tick(self, ss: StrategyState, completed_row=None):
        """Check live tick + completed candle OHLC for a future Timing retest."""
        if ss.strategy != "timing":
            return None
        # LIVE Timing Candle uses a broker-side pending LIMIT order. Retest
        # detection remains available for PAPER/backtest compatibility.
        if self.live_enabled:
            return None
        if ss.execution.virtual_position != 0:
            return None
        if not self.state.entries_enabled or not ss.execution.entries_enabled:
            return None
        if ss.timing_high is None or ss.timing_low is None:
            return None
        if ss.timing_session_blocked:
            return None
        if not (ss.timing_armed_buy or ss.timing_armed_sell):
            return None

        tick = core.mt5.symbol_info_tick(self.state.mt5_symbol)
        bid = float(getattr(tick, "bid", 0.0) or 0.0) if tick is not None else None
        ask = float(getattr(tick, "ask", 0.0) or 0.0) if tick is not None else None
        if bid is not None and bid <= 0:
            bid = None
        if ask is not None and ask <= 0:
            ask = None

        candle_high = None
        candle_low = None
        bar_key = None
        if completed_row is not None:
            bar_key = str(completed_row["time"])
            candle_high = float(completed_row["high"])
            candle_low = float(completed_row["low"])

        # IMPORTANT: calculate the CURRENT FORMING candle key from the MT5 tick.
        # calculate_frame() intentionally removes the forming candle, so
        # completed_row remains the breakout candle during the entire next candle.
        # Using completed_row as the live candle key would incorrectly suppress
        # the intrabar retest until the next candle closes.
        live_bar_key = None
        if tick is not None:
            tick_seconds = getattr(tick, "time", None)
            if tick_seconds:
                try:
                    tick_dt = datetime.fromtimestamp(float(tick_seconds), tz=timezone.utc).replace(tzinfo=None)
                    minutes = int(core.timeframe_minutes(self.state.timeframe))
                    total_minutes = tick_dt.hour * 60 + tick_dt.minute
                    bucket = (total_minutes // minutes) * minutes
                    live_bar_dt = tick_dt.replace(
                        hour=bucket // 60,
                        minute=bucket % 60,
                        second=0,
                        microsecond=0,
                    )
                    live_bar_key = str(live_bar_dt)
                except (TypeError, ValueError, OverflowError):
                    live_bar_key = None

        decision = retest_signal(
            armed_buy=ss.timing_armed_buy,
            armed_sell=ss.timing_armed_sell,
            timing_high=ss.timing_high,
            timing_low=ss.timing_low,
            bar_key=bar_key,
            arm_time=ss.timing_arm_time,
            live_bar_key=live_bar_key,
            bid=bid,
            ask=ask,
            candle_high=candle_high,
            candle_low=candle_low,
        )
        if decision is None:
            return None

        side = decision.side
        entry_price = ask if side == "BUY" else bid
        if entry_price is None:
            return None
        sl = ss.timing_low if side == "BUY" else ss.timing_high

        if self.live_enabled:
            ss.active_trade_id = uuid.uuid4().hex
            ok = core.open_position(
                self.state.mt5_symbol, side, self.state.lot, True,
                sl=sl, magic_number=ss.execution.magic_number,
            )
            if not ok:
                # The broker rejected the order. Consume this exact retest setup
                # so the same signal is NOT submitted again every polling cycle.
                # A new breakout is required before another Timing Candle entry.
                ss.active_trade_id = None
                ss.timing_armed_buy = False
                ss.timing_armed_sell = False
                ss.timing_arm_time = None
                self._save_timing_runtime(ss)
                print(
                    f"TIMING RETEST {side} ORDER REJECTED -> "
                    "setup consumed; waiting for next BO/BD"
                )
                return None
            self._sync_live_snapshot(ss)
            if ss.paper_position == 0:
                return None
            self._record(
                "timing", side, "OPEN", ss.paper_entry_price or entry_price, "LIVE",
                message="LIVE — TIMING RETEST ENTRY",
                sl=ss.paper_sl or sl,
                volume=ss.volume or self.state.lot,
                trade_id=ss.active_trade_id,
                ticket=ss.live_ticket,
            )
        else:
            ss.execution.virtual_position = 1 if side == "BUY" else -1
            self._paper_open(ss, side, entry_price, sl_override=sl)

        # Consume the setup only after the entry succeeded. SL later returns the
        # strategy to FLAT; a NEW BO/BD is then required for another trade.
        ss.timing_armed_buy = False
        ss.timing_armed_sell = False
        self._save_timing_runtime(ss)
        print(
            f"TIMING RETEST -> {side} | Entry={entry_price:.3f} | "
            f"H={ss.timing_high:.3f} L={ss.timing_low:.3f} | {decision.reason}"
        )
        return f"{DISPLAY_NAME['timing']}: {side}"

    def _restore_timing_reference_from_history(self, ss: StrategyState, df):
        """Recover today's completed Timing Candle reference levels after restart.

        This does NOT invent an armed BO/BD setup. Armed state is restored from the
        persisted runtime file. The purpose here is to restore the reference High/Low
        and keep an existing broker position/dashboard consistent after restart.
        """
        if ss.strategy != "timing" or ss.timing_high is not None or ss.timing_low is not None:
            return
        try:
            if len(df) < 2:
                return
            completed = df.iloc[:-1]
            matches = []
            for _, r in completed.tail(500).iterrows():
                dt = self._timing_local_dt(r["time"])
                if (dt.date().isoformat() == ss.timing_date and
                    dt.hour == int(self.state.timing_hour) and
                    dt.minute == int(self.state.timing_minute)):
                    matches.append(r)
            if not matches:
                return
            r = matches[-1]
            ss.timing_high = float(r["high"])
            ss.timing_low = float(r["low"])
            self._save_timing_runtime(ss)
            print(
                f"TIMING REFERENCE RECOVERED | H={ss.timing_high:.3f} | "
                f"L={ss.timing_low:.3f}"
            )
        except Exception as exc:
            print(f"TIMING REFERENCE RECOVERY ERROR | {exc}")

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
                self._timing_session_df = df
                if len(df) < 2:
                    time.sleep(1.0)
                    continue

                # IMPORTANT:
                # ChandraTrendEngine.calculate_frame() already returns the latest
                # COMPLETED candle as its final row. It does NOT expose the live
                # forming candle as df.iloc[-1].
                #
                # Therefore Timing Candle must use df.iloc[-1].
                # Using df.iloc[-2] was the cause of the one-candle delay.
                completed_row = df.iloc[-1]
                bar_time = str(completed_row["time"])

                # Timing Candle retest is a tick event, so it must be evaluated
                # every polling cycle, even when there is no new completed candle.
                timing_tick_trigger = None
                if "timing" in self.state.strategy_states:
                    tss = self.state.strategy_states["timing"]
                    if self.live_enabled:
                        self._sync_timing_pending(tss)
                        timing_positions = self._live_positions(tss.execution.magic_number)
                        if not timing_positions and tss.live_ticket:
                            # Record broker-side SL/TP/manual close before flattening state.
                            self._record_live_close(tss)
                        self._sync_live_snapshot(tss)
                    else:
                        # Paper SL remains candle-based; retest entry itself is tick-based.
                        self._paper_check_sl(tss, completed_row)
                        tss.execution.virtual_position = tss.paper_position

                    if tss.timing_high is None and tss.timing_low is None:
                        self._load_timing_runtime(tss)
                    if tss.timing_high is None or tss.timing_low is None:
                        self._restore_timing_reference_from_history(tss, df)

                    local_now = self._timing_local_dt(completed_row["time"])
                    self._timing_session_control(tss, local_now, df)
                    timing_tick_trigger = self._process_timing_retest_tick(tss, completed_row)
                    if timing_tick_trigger:
                        if self.state.last_signal is None:
                            self.state.last_signal = {}
                        self.state.last_signal["triggered"] = timing_tick_trigger
                        self.state.last_signal["timing_buy"] = bool(tss.timing_armed_buy)
                        self.state.last_signal["timing_sell"] = bool(tss.timing_armed_sell)
                        self.state.last_signal["timing_high"] = tss.timing_high
                        self.state.last_signal["timing_low"] = tss.timing_low

                if bar_time != last_bar:
                    signal = core.signal_from_row(completed_row)
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
                            self._paper_check_sl(ss, completed_row)
                            ss.execution.virtual_position = ss.paper_position
                        else:
                            before_live = self._live_positions(ss.execution.magic_number)
                            self._sync_live_snapshot(ss)
                        before = ss.execution.virtual_position
                        if name == "timing":
                            if not self.live_enabled:
                                self._paper_check_sl(ss, completed_row)
                                ss.execution.virtual_position = ss.paper_position
                            else:
                                # Live position state was already synchronized at the
                                # top of this polling cycle. Keep Timing Candle enabled
                                # after broker-side SL/TP/manual exits.
                                self._sync_live_snapshot(ss)
                            timing_trigger = self._process_timing_candle(ss, completed_row)
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
                            self.state.mt5_symbol, completed_row, signal, self.state.lot,
                            self.live_enabled, ss.execution
                        )
                        after = ss.execution.virtual_position
                        if not self.live_enabled:
                            if before == 0 and after != 0:
                                side = "BUY" if after == 1 else "SELL"
                                self._paper_open(ss, side, self._paper_price(side) or float(completed_row["close"]), signal.l100)
                                triggered.append(f"{DISPLAY_NAME[name]}: {side}")
                            elif before != 0 and after == 0:
                                side = "BUY" if before == 1 else "SELL"
                                self._paper_close(ss, self._paper_price("SELL" if before == 1 else "BUY") or float(completed_row["close"]), "SIGNAL EXIT")
                            elif before != after and before != 0 and after != 0:
                                side = "BUY" if after == 1 else "SELL"
                                self._paper_open(ss, side, self._paper_price(side) or float(completed_row["close"]), signal.l100)
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
                        timing_hour: int = 4, timing_minute: int = 0,
                        timing_end_time: str | None = None):
        """Backtest Timing Candle using MT5 broker-time candle levels and retest entries."""
        if not (0 <= int(timing_hour) <= 23 and 0 <= int(timing_minute) <= 59):
            raise ValueError("Invalid timing candle time")

        end_minutes = None
        if timing_end_time:
            try:
                eh, em = [int(x) for x in str(timing_end_time).strip().split(":")[:2]]
                if not (0 <= eh <= 23 and 0 <= em <= 59):
                    raise ValueError
                end_minutes = eh * 60 + em
            except (ValueError, TypeError):
                raise ValueError("Invalid Timing Candle end time. Use HH:MM.")

        try:
            # Backtest dates are broker/server calendar dates. Keep them naive
            # and compare directly with the MT5 timestamps returned by the
            # historical data loader. Do not convert through IST or UTC.
            broker_start = datetime.fromisoformat(start_date)
            broker_end = datetime.fromisoformat(end_date) + timedelta(days=1) - timedelta(microseconds=1)
        except ValueError as exc:
            raise ValueError("Invalid date range") from exc
        if broker_end <= broker_start: raise ValueError("To date must be on or after From date")
        if lot <= 0: raise ValueError("Lot size must be greater than zero")
        test_symbol = (symbol or self.state.symbol).strip()
        test_timeframe = (timeframe or self.state.timeframe).strip().upper()
        core.timeframe_minutes(test_timeframe)
        if not core.mt5.initialize(): raise RuntimeError(f"MT5 initialize failed: {core.mt5.last_error()}")
        try:
            resolved_symbol = test_symbol
            if core.mt5.symbol_info(resolved_symbol) is None and resolved_symbol.upper() == "XAUUSD":
                candidates=["XAUUSD.sd","XAUUSDm","XAUUSD.a","XAUUSD.r"]
                resolved_symbol=next((c for c in candidates if core.mt5.symbol_info(c) is not None), None)
            if not resolved_symbol: raise RuntimeError(f"MT5 symbol not found for '{test_symbol}'")
            info=core.ensure_symbol(resolved_symbol)
            df=core.get_rates_range(resolved_symbol,test_timeframe,broker_start,broker_end)
            # Enforce the user's selected broker-date range explicitly. This
            # prevents a provider/session boundary from leaking candles from
            # the previous/next broker calendar day into the report.
            df["time"] = pd.to_datetime(df["time"])
            df = df[(df["time"] >= broker_start) & (df["time"] <= broker_end)].copy()
            df.sort_values("time", inplace=True)
            df.reset_index(drop=True, inplace=True)
            if len(df)<2: raise RuntimeError("Not enough historical candles for this range")
            point=float(getattr(info,"point",0.01) or 0.01); contract=float(getattr(info,"trade_contract_size",1.0) or 1.0)
            position=0; entry=None; entry_time=None; sl=None; trades=[]; equity=0.0; peak=0.0; dd=0.0
            timing_day=None; th=None; tl=None; armed_buy=False; armed_sell=False; arm_time=None
            day_count=0; day_points=0.0; target_hit=False; daily={}
            day_diag={}
            def close_trade(price,t,reason):
                nonlocal position,entry,entry_time,sl,equity,peak,dd,day_points,target_hit
                if position==0 or entry is None:return
                side="BUY" if position==1 else "SELL"; move=(price-entry) if position==1 else (entry-price); pnl=move*lot*contract
                equity+=pnl; peak=max(peak,equity); dd=max(dd,peak-equity); day_points+=move
                dk=self._timing_local_dt(t).date().isoformat(); ds=daily.setdefault(dk,{"trades":0,"wins":0,"losses":0,"pnl":0.0,"points":0.0,"target_reached":False}); ds["trades"]+=1; ds["pnl"]+=pnl; ds["points"]+=move
                if pnl>0:ds["wins"]+=1
                elif pnl<0:ds["losses"]+=1
                if daily_target_points>0 and day_points>=daily_target_points:target_hit=True;ds["target_reached"]=True
                trades.append({"side":side,"entry":round(entry,int(getattr(info,"digits",2))),"entry_time":str(entry_time),"exit":round(price,int(getattr(info,"digits",2))),"exit_time":str(t),"reason":reason,"pnl":round(pnl,2),"initial_sl":round(sl,int(getattr(info,"digits",2))) if sl is not None else None,"reference":round(th if side=="BUY" else tl,int(getattr(info,"digits",2))) if (th if side=="BUY" else tl) is not None else None})
                position=0;entry=None;entry_time=None;sl=None
            for _,row in df.iterrows():
                t=row["time"]; local=self._timing_local_dt(t); dk=local.date().isoformat(); close=float(row["close"]); high=float(row["high"]); low=float(row["low"])
                if timing_day!=dk:
                    timing_day=dk; th=None;tl=None;armed_buy=False;armed_sell=False;arm_time=None;day_count=0;day_points=0.0;target_hit=False;daily.setdefault(dk,{"trades":0,"wins":0,"losses":0,"pnl":0.0,"points":0.0,"target_reached":False})
                    day_diag[dk] = {
                        "timing_found": False,
                        "timing_time": None,
                        "timing_high": None,
                        "timing_low": None,
                        "breakout": None,
                        "breakout_time": None,
                        "retest": None,
                        "retest_time": None,
                        "reason": "NO TIMING CANDLE"
                    }

                # Optional daily end time: after this time no new Timing Candle
                # entries are allowed and any open position is closed at the
                # completed candle close. Blank means full available session.
                if end_minutes is not None:
                    current_minutes = local.hour * 60 + local.minute
                    if current_minutes >= end_minutes:
                        if position != 0:
                            close_trade(close, t, "END_TIME")
                        if dk in day_diag and day_diag[dk]["reason"] in ("NO TIMING CANDLE", "BREAKOUT NOT FOUND", "RETEST NOT FOUND"):
                            day_diag[dk]["reason"] = "END TIME REACHED"
                        armed_buy = False
                        armed_sell = False
                        continue

                if position==1 and sl is not None and low<=sl: close_trade(sl,t,"TIMING_CANDLE_SL")
                elif position==-1 and sl is not None and high>=sl: close_trade(sl,t,"TIMING_CANDLE_SL")
                is_timing=local.hour==int(timing_hour) and local.minute==int(timing_minute)
                if is_timing:
                    th=high;tl=low;armed_buy=False;armed_sell=False;arm_time=str(t)
                    day_diag[dk].update({
                        "timing_found": True,
                        "timing_time": str(t),
                        "timing_high": th,
                        "timing_low": tl,
                        "reason": "WAITING FOR BREAKOUT"
                    })
                    print(
                        f"TIMING CANDLE FOUND | {local.strftime('%Y-%m-%d %H:%M:%S')} | "
                        f"O={float(row['open']):.3f} H={th:.3f} L={tl:.3f} C={close:.3f}"
                    )
                    continue
                if th is None or tl is None: continue
                # A CLOSED candle beyond the timing level arms the breakout.
                # The breakout candle itself cannot be the retest/entry candle.
                if close>th:
                    if not armed_buy:
                        armed_buy=True;armed_sell=False;arm_time=str(t)
                        day_diag[dk]["breakout"] = "BUY"
                        day_diag[dk]["breakout_time"] = str(t)
                        day_diag[dk]["reason"] = "WAITING FOR BUY RETEST"
                elif close<tl:
                    if not armed_sell:
                        armed_sell=True;armed_buy=False;arm_time=str(t)
                        day_diag[dk]["breakout"] = "SELL"
                        day_diag[dk]["breakout_time"] = str(t)
                        day_diag[dk]["reason"] = "WAITING FOR SELL RETEST"

                if arm_time==str(t):
                    continue

                # TIMING CANDLE ONLY: retest/touch is enough to trigger entry.
                # No requirement for the retest candle to close back on the
                # breakout side. Entry is taken at the timing level.
                blocked=target_hit or (max_trades_per_day>0 and day_count>=max_trades_per_day)
                if position==0 and not blocked:
                    if armed_buy and low<=th:
                        position=1;entry=th;entry_time=t;sl=tl;day_count+=1;armed_buy=False;armed_sell=False
                        day_diag[dk]["retest"] = "BUY"
                        day_diag[dk]["retest_time"] = str(t)
                        day_diag[dk]["reason"] = "TRADE TRIGGERED"
                    elif armed_sell and high>=tl:
                        position=-1;entry=tl;entry_time=t;sl=th;day_count+=1;armed_sell=False;armed_buy=False
                        day_diag[dk]["retest"] = "SELL"
                        day_diag[dk]["retest_time"] = str(t)
                        day_diag[dk]["reason"] = "TRADE TRIGGERED"
            if position!=0: close_trade(float(df.iloc[-1]["close"]),df.iloc[-1]["time"],"END_OF_TEST")
            wins=sum(t["pnl"]>0 for t in trades);losses=sum(t["pnl"]<0 for t in trades)
            for dk, info_diag in day_diag.items():
                if daily.get(dk, {}).get("trades", 0) > 0:
                    info_diag["reason"] = "TRADE TRIGGERED"
                elif info_diag["timing_found"] and info_diag["breakout"] is None:
                    info_diag["reason"] = "BREAKOUT NOT FOUND"
                elif info_diag["timing_found"] and info_diag["breakout"] and info_diag["retest"] is None:
                    info_diag["reason"] = "RETEST NOT FOUND"
            return {"ok":True,"strategy":"timing","symbol":resolved_symbol,"timeframe":test_timeframe,"from":start_date,"to":end_date,"candles":len(df),"timing_hour":timing_hour,"timing_minute":timing_minute,"timing_end_time":timing_end_time or "","trades":len(trades),"buy_trades":sum(t["side"]=="BUY" for t in trades),"sell_trades":sum(t["side"]=="SELL" for t in trades),"wins":wins,"losses":losses,"win_rate":round(wins/len(trades)*100 if trades else 0,2),"net_pnl":round(equity,2),"max_drawdown":round(dd,2),"best_trade":round(max((t["pnl"] for t in trades),default=0),2),"worst_trade":round(min((t["pnl"] for t in trades),default=0),2),"max_trades_per_day":max_trades_per_day,"daily_target_points":daily_target_points,"daily_stats":[{"date":d,**{k:round(v,2) if isinstance(v,float) else v for k,v in x.items()},"diagnostic":day_diag.get(d,{})} for d,x in sorted(daily.items())],"profitable_days":sum(v["pnl"]>0 for v in daily.values()),"losing_days":sum(v["pnl"]<0 for v in daily.values()),"trading_days":len(daily),"trades_detail":trades}
        finally:
            try: core.mt5.shutdown()
            except Exception: pass

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
                "timing_session_blocked": ss.timing_session_blocked,
                "timing_session_end": ss.timing_session_end,
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