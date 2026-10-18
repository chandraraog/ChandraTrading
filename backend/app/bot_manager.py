import json
import pandas as pd
import threading
import time
import uuid
import math
import os
import tempfile
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from backend.app.engine.chandra_trend_engine import ChandraTrendEngine
from backend.app.engine import chandra_core as core
from backend.app.strategies.timing_candle import breakout_arm, retest_signal
from backend.app.runtime_paths import app_root
from backend.app.licensing.license_service import LicenseService
from backend.app.services.pob_execution import POBExecution, MAGIC as POB_MAGIC
from backend.app.strategies.pob import calculate_levels

# Website V1 enables the engine's L100-based initial/trailing SL.
core.STOPLOSS_CONFIG["enabled"] = True
core.STOPLOSS_CONFIG["offset_points"] = 5.0

STRATEGIES = ("strategic", "magical", "timing", "timing2", "pob")
MAGIC_BY_STRATEGY = {"strategic": 26081201, "magical": 26081202, "timing": 26081203, "timing2": 26081206, "manual": 26081204, "pob": POB_MAGIC}
ENGINE_MODE = {"strategic": "buy_sell", "magical": "magical", "timing": "timing", "timing2": "timing", "pob": "pob"}
DISPLAY_NAME = {"strategic": "Strategic Entry", "magical": "Magical Entry", "timing": "Significant Time Candle A", "timing2": "Significant Time Candle B", "pob": "POB"}
HISTORY_FILE = app_root() / "data" / "trade_history.json"
POB_FILE = app_root() / "data" / "pob_state.json"
print(f"Chandra trade log file: {HISTORY_FILE}", flush=True)

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
    session_realized_pnl: float = 0.0
    # When the engine is started while a broker position already exists,
    # exclude its pre-start movement from this run's P&L/points.
    session_open_pnl_baseline: float = 0.0
    session_open_points_baseline: float = 0.0
    open_trade_time: str | None = None
    timing_date: str | None = None
    timing_high: float | None = None
    timing_low: float | None = None
    timing_armed_buy: bool = False
    timing_armed_sell: bool = False
    timing_arm_time: str | None = None
    timing_last_quote: tuple[float, float] | None = None
    timing_entry_message: str | None = None
    timing_session_blocked: bool = False
    timing_close_pending: bool = False
    timing_session_message: str | None = None
    target: float | None = None
    volume: float = 0.0
    active_trade_id: str | None = None
    live_ticket: str | None = None
    # Preserve the open details while MT5 publishes the broker exit deal.
    # History can lag positions_get() by one or more polling cycles.
    live_exit_pending: dict | None = None
    # Kept for backward compatibility with older clients that displayed a
    # Timing LIMIT order. Timing Candle now enters at market on a live touch.
    timing_pending_ticket: str | None = None
    timing_pending_side: str | None = None
    timing_pending_price: float | None = None
    timing_pending_sl: float | None = None
    session_target_points: float | None = None
    session_realized_points: float = 0.0
    session_target_reached: bool = False
    target_close_message: str | None = None
    last_bar: str | None = None

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
    timing_entry_mode: str = "retest"
    pob_level_source: str = "manual"
    pob_candle_time: str = "04:00"
    pob_high: float | None = None
    pob_low: float | None = None
    pob_entry_mode: str = "retest"
    pob_sl_mode: str = "levels"
    strategy_target_points: dict = field(default_factory=dict)
    strategy_settings: dict = field(default_factory=dict)
    strategy_symbols: dict = field(default_factory=dict)
    strategy_contract_sizes: dict = field(default_factory=dict)
    session_started_at: str | None = None
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
        self._config_lock = threading.RLock()
        self._restart_pending = False
        self._restart_error = None
        self._restart_identity = None
        self._pob = None
        self._pob_load_error = None
        self._pob_session_baseline = 0.0
        try:
            self._pob = POBExecution(POB_FILE, core)
            if self._pob.data["config"]:
                for key, value in self._pob.data["config"].items():
                    setattr(self.state, key, value)
        except Exception as exc:
            self._pob_load_error = str(exc)
        self._reset_strategy_states()
        self._reset_manual_state()
        self._load_trade_history()
        self._restore_realized_pnl()
        self._sync_pob()
        self._restore_runtime()

    def _save_runtime(self):
        """Checkpoint configuration and counters; trade history is a separate log."""
        path = HISTORY_FILE.with_name("strategy_runtime_state.json")
        excluded = {"trades", "strategy_states", "manual_state", "account", "mt5_connected", "running", "error"}
        payload = {"version": 1, "resume": self.state.running or self._restart_pending,
                   "state": {k: v for k, v in self.state.__dict__.items() if k not in excluded},
                   "strategies": {k: asdict(v) for k, v in self.state.strategy_states.items() if k != "pob"},
                   "account": [str(self.state.account.get("login")), str(self.state.account.get("server"))] if self.state.account else self._restart_identity}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                    prefix=path.name+".", suffix=".tmp", delete=False) as f:
                tmp = Path(f.name)
                json.dump(payload, f, default=lambda v: v.item(), indent=2)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(7):
                try:
                    tmp.replace(path)
                    break
                except PermissionError:
                    if attempt == 6:
                        raise
                    time.sleep(min(.05 * 2 ** attempt, .5))
        finally:
            if tmp:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def _restore_runtime(self):
        path = HISTORY_FILE.with_name("strategy_runtime_state.json")
        if not path.exists():
            return
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("version") != 1 or not isinstance(saved.get("state"), dict):
                raise ValueError("Invalid strategy recovery file")
            state = saved["state"]
            if not state.get("strategies") or any(n not in STRATEGIES for n in state["strategies"]):
                raise ValueError("Invalid saved strategies")
            for key, value in state.items():
                if key in BotState.__dataclass_fields__ and key not in {"running", "trades", "strategy_states", "manual_state", "account"}:
                    setattr(self.state, key, value)
            self._reset_strategy_states()
            if saved.get("resume"):
                for name, values in saved.get("strategies", {}).items():
                    if name not in self.state.strategy_states:
                        continue
                    ss = self.state.strategy_states[name]
                    for key, value in values.items():
                        if key == "execution":
                            for attr, item in value.items():
                                if attr in core.ExecutionState.__dataclass_fields__:
                                    setattr(ss.execution, attr, item)
                        elif key in StrategyState.__dataclass_fields__:
                            setattr(ss, key, value)
                    # No crossing may be inferred across server downtime.
                    ss.timing_last_quote = None
                self._restart_pending = True
            self._restart_identity = saved.get("account")
            self.state.running = False
            self._sync_pob()
        except Exception as exc:
            self._restart_error = "Strategy recovery requires review: " + str(exc)
            self.state.error = self._restart_error

    @property
    def live_enabled(self):
        return self.state.mode == "live"

    def _strategy_settings(self, name):
        return dict({"symbol": self.state.symbol, "timeframe": self.state.timeframe, "lot": self.state.lot}, **self.state.strategy_settings.get(name, {}))

    def _strategy_lot(self, name):
        return self._strategy_settings(name)["lot"]

    def _strategy_timeframe(self, name):
        return self._strategy_settings(name)["timeframe"]

    def _strategy_symbol(self, name):
        return getattr(self.state, "strategy_symbols", {}).get(name) or getattr(self.state, "strategy_settings", {}).get(name, {}).get("symbol") or self.state.mt5_symbol

    def _strategy_contract(self, name):
        return self.state.strategy_contract_sizes.get(name, self.state.contract_size)

    def _resolve_strategy_markets(self, settings):
        symbols, contracts = {}, {}
        for name, cfg in settings.items():
            symbols[name] = self._resolve_mt5_symbol(cfg["symbol"])
            info = core.ensure_symbol(symbols[name])
            contracts[name] = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
        return symbols, contracts

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
                session_target_points=self.state.strategy_target_points.get(name),
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

    def _authorize_strategies(self, strategies, settings):
        info = LicenseService().require()
        allowed = {str(feature).upper() for feature in info["features"]}
        features = {"strategic": "STRATEGIC_ENTRY", "magical": "MAGICAL_ENTRY",
                    "timing": "TIMING_CANDLE", "timing2": "TIMING_CANDLE", "pob": "POB"}
        for name in strategies:
            feature = features[name]
            if "*" not in allowed and feature not in allowed:
                raise PermissionError(f"License does not allow feature: {feature}")
            if float(settings[name]["lot"]) > float(info["max_lot"]) + 1e-12:
                raise PermissionError(f"License maximum lot is {info['max_lot']} for {DISPLAY_NAME[name]}")

    def _paper_switch_block_reason(self):
        """Require stopped execution and broker-confirmed flat before leaving Live."""
        if self.state.mode != "live":
            return None
        if self.state.running or (self._thread and self._thread.is_alive()):
            return "Stop the bot and close its live positions before switching to Paper."
        if self._pob_load_error or (self._pob and self._pob.active):
            return "Close or reconcile the POB position/request before switching to Paper."
        if any(ss.live_exit_pending for ss in self.state.strategy_states.values()):
            return "A live exit is awaiting confirmation. Reconcile it before switching to Paper."
        try:
            if core.mt5 is None or not core.mt5.initialize():
                return "Connect MT5 to verify that live positions are closed before switching to Paper."
            account = core.mt5.account_info()
            if account is None:
                return "MT5 account is unavailable; Live mode remains locked."
            for key in ("login", "server"):
                expected = self.state.account.get(key)
                if expected is not None and str(getattr(account, key, None)) != str(expected):
                    return "Reconnect the original MT5 account before switching to Paper."
            positions = core.mt5.positions_get()
            orders = core.mt5.orders_get()
            if positions is None or orders is None:
                return "Cannot verify MT5 positions/orders; Live mode remains locked."
            owned = set(MAGIC_BY_STRATEGY.values())
            if any(p.magic in owned for p in positions) or any(o.magic in owned for o in orders):
                return "Close all Chandra live positions and pending orders before switching to Paper."
        except Exception:
            return "Cannot verify MT5 positions/orders; Live mode remains locked."
        return None

    def configure(self, strategies, symbol, timeframe, lot, mode, timing_hour=9,
                  timing_minute=30, timing_end_time=None, strategy_target_points=None,
                  pob_high=None, pob_low=None, pob_entry_mode="retest", pob_sl_mode="levels", timing_entry_mode="retest", strategy_settings=None, pob_level_source="manual", pob_candle_time="04:00"):
        if isinstance(strategies, str):
            strategies = [strategies]
        strategies = list(dict.fromkeys(strategies))
        if not strategies or any(s not in STRATEGIES for s in strategies):
            raise ValueError("Select at least one valid strategy")
        if "timing" not in strategies:
            # Hidden settings from a removed strategy must not block deployment.
            timing_hour, timing_minute, timing_end_time, timing_entry_mode = 4, 0, None, "retest"
        if timing_entry_mode not in ("retest", "direct"):
            raise ValueError("Timing entry mode must be Retest or Without retest")
        if mode not in ("paper", "live"):
            raise ValueError("Invalid trading mode")
        if "pob" in strategies:
            if pob_level_source not in ("manual", "candle"):
                raise ValueError("Invalid POB High/Low source")
            if pob_level_source == "manual":
                calculate_levels(pob_high, pob_low)
            else:
                try:
                    h, m = map(int, pob_candle_time.split(":"))
                    if not (0 <= h <= 23 and 0 <= m <= 59):
                        raise ValueError()
                    pob_candle_time = f"{h:02d}:{m:02d}"
                except (ValueError, AttributeError):
                    raise ValueError("POB candle start time must be HH:MM")
                pob_high = pob_low = None
            if pob_entry_mode not in ("retest", "direct"):
                raise ValueError("POB entry mode must be Retest or Without retest")
            if pob_sl_mode not in ("default", "levels"):
                raise ValueError("POB SL must use Default or Buy/Sell levels")
            if not math.isfinite(lot) or lot <= 0:
                raise ValueError("POB lot must be finite and positive")
            if (strategy_target_points or {}).get("pob") is not None:
                raise ValueError("POB uses calculated T2/T3 targets")
        if not (0 <= int(timing_hour) <= 23 and 0 <= int(timing_minute) <= 59):
            raise ValueError("Invalid timing candle time")
        if timing_end_time:
            try:
                end_hour, end_minute = map(int, timing_end_time.split(":"))
                if not (0 <= end_hour <= 23 and 0 <= end_minute <= 59):
                    raise ValueError()
                if end_hour * 60 + end_minute <= int(timing_hour) * 60 + int(timing_minute):
                    raise ValueError()
            except (ValueError, AttributeError):
                raise ValueError("Timing end time must be HH:MM and after the start time")
            timing_end_time = f"{end_hour:02d}:{end_minute:02d}"
        target_points = strategy_target_points or {}
        if not isinstance(target_points, dict):
            raise ValueError("Strategy target points must be a map")
        unknown_targets = set(target_points) - set(STRATEGIES)
        if unknown_targets:
            raise ValueError("Invalid strategy target")
        normalized_targets = {}
        for name in strategies:
            value = target_points.get(name)
            if value is None or value == "":
                normalized_targets[name] = None
            elif float(value) > 0:
                normalized_targets[name] = float(value)
            else:
                raise ValueError("Target points must be greater than zero or left blank")
        raw_settings = strategy_settings or {}
        if not isinstance(raw_settings, dict) or set(raw_settings) - set(STRATEGIES):
            raise ValueError("Invalid strategy settings")
        normalized_settings = {}
        for name in strategies:
            setting = raw_settings.get(name, {})
            if not isinstance(setting, dict) or set(setting) - {"symbol", "timeframe", "lot", "timing_hour", "timing_minute", "timing_end_time", "timing_entry_mode"}:
                raise ValueError("Strategy settings require symbol, timeframe and lot")
            tf = str(setting.get("timeframe", timeframe)).upper()
            core.timeframe_minutes(tf)
            size = float(setting.get("lot", lot))
            if not math.isfinite(size) or size <= 0:
                raise ValueError("Each strategy lot must be finite and positive")
            instrument = str(setting.get("symbol", symbol)).strip()
            if not instrument:
                raise ValueError("Each strategy requires a symbol")
            normalized_settings[name] = {"symbol": instrument, "timeframe": tf, "lot": size}
            if name == "timing2":
                h, m = setting.get("timing_hour", 4), setting.get("timing_minute", 0)
                end = setting.get("timing_end_time", "08:00")
                entry = setting.get("timing_entry_mode", "retest")
                if not (isinstance(h, int) and isinstance(m, int) and 0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError("Significant Time Candle B requires a valid start time")
                if entry not in ("retest", "direct"):
                    raise ValueError("Invalid Significant Time Candle B entry mode")
                if end:
                    eh, em = map(int, end.split(":"))
                    if not (0 <= eh <= 23 and 0 <= em <= 59 and eh * 60 + em > h * 60 + m):
                        raise ValueError("Significant Time Candle B end must be after start")
                    end = f"{eh:02d}:{em:02d}"
                normalized_settings[name].update(timing_hour=h, timing_minute=m,
                    timing_end_time=end or None, timing_entry_mode=entry)

        with self._config_lock:
            if self.state.mode == "live" and mode == "paper":
                reason = self._paper_switch_block_reason()
                if reason:
                    raise ValueError(reason)
            was_running = self.state.running or self._restart_pending
            existing = set(self.state.strategies) if was_running else set()
            added = set(strategies) - existing
            if was_running:
                if not existing.issubset(strategies):
                    raise ValueError("Running strategies cannot be removed; stop the bot first")
                if (symbol, timeframe, lot, mode) != (self.state.symbol, self.state.timeframe, self.state.lot, self.state.mode):
                    raise ValueError("Symbol, timeframe, lot and mode are locked while the bot is running")
                if "timing" in existing and (int(timing_hour), int(timing_minute), timing_end_time or None, timing_entry_mode) != (self.state.timing_hour, self.state.timing_minute, self.state.timing_end_time, self.state.timing_entry_mode):
                    raise ValueError("Running Significant Time Candle A settings are locked")
                if "pob" in existing and (pob_high, pob_low, pob_entry_mode, pob_sl_mode, pob_level_source, pob_candle_time) != (self.state.pob_high, self.state.pob_low, self.state.pob_entry_mode, self.state.pob_sl_mode, self.state.pob_level_source, self.state.pob_candle_time):
                    raise ValueError("Running POB settings are locked")
                if any(normalized_settings[name] != self._strategy_settings(name) for name in existing):
                    raise ValueError("Running strategy symbol, timeframe and lot are locked")
                if any(normalized_targets.get(name) != self.state.strategy_target_points.get(name) for name in existing):
                    raise ValueError("Running strategy targets are locked")
            self._authorize_strategies(strategies, normalized_settings)
            resolved, contracts = {}, {}
            if was_running:
                resolved, contracts = self._resolve_strategy_markets({name: normalized_settings[name] for name in added})
            if self._pob_load_error and "pob" in strategies:
                raise ValueError("POB recovery journal needs review: " + self._pob_load_error)
            if "pob" in strategies:
                self._pob.configure(dict(strategies=strategies, symbol=normalized_settings["pob"]["symbol"], timeframe=normalized_settings["pob"]["timeframe"],
                    lot=normalized_settings["pob"]["lot"], strategy_settings=normalized_settings, mode=mode, timing_hour=int(timing_hour), timing_minute=int(timing_minute),
                    timing_end_time=timing_end_time or None, timing_entry_mode=timing_entry_mode, strategy_target_points=normalized_targets,
                    pob_high=pob_high, pob_low=pob_low, pob_entry_mode=pob_entry_mode, pob_sl_mode=pob_sl_mode, pob_level_source=pob_level_source, pob_candle_time=pob_candle_time))
            elif self._pob and self._pob.active:
                raise ValueError("Close or reconcile POB before removing it from the configuration")
            elif self._pob and self._pob.data["config"]:
                self._pob.data["config"] = None
                self._pob.save()
            if was_running and "pob" in added:
                self._pob.bind(resolved["pob"])
                self._pob_session_baseline = sum(float(e.get("pnl") or 0) for e in self._pob.data["events"])
            self.state.strategies = strategies
            self.state.symbol = symbol
            self.state.timeframe = timeframe
            self.state.lot = lot
            self.state.mode = mode
            self.state.timing_hour = int(timing_hour)
            self.state.timing_minute = int(timing_minute)
            self.state.timing_end_time = timing_end_time or None
            self.state.timing_entry_mode = timing_entry_mode
            self.state.strategy_target_points = normalized_targets
            self.state.strategy_settings = normalized_settings
            self.state.strategy_symbols.update(resolved)
            self.state.strategy_contract_sizes.update(contracts)
            self.state.pob_level_source, self.state.pob_candle_time = pob_level_source, pob_candle_time
            self.state.pob_high, self.state.pob_low = pob_high, pob_low
            self.state.pob_entry_mode = pob_entry_mode
            self.state.pob_sl_mode = pob_sl_mode
            if was_running:
                for name in strategies:
                    if name in existing:
                        continue
                    ss = StrategyState(strategy=name, execution=core.ExecutionState(
                        mode=ENGINE_MODE[name], magic_number=MAGIC_BY_STRATEGY[name],
                        entries_enabled=self.state.entries_enabled),
                        session_target_points=normalized_targets.get(name))
                    self.state.strategy_states[name] = ss
                    if name == "pob":
                        self._sync_pob()
                    elif self.live_enabled:
                        self._sync_live_snapshot(ss)
                        if ss.paper_position:
                            ss.session_open_pnl_baseline = ss.current_pnl
                            ss.session_open_points_baseline = self._strategy_open_points(ss)
            else:
                self.state.entries_enabled = True
                self.state.safety_halt_reason = None
                self._reset_strategy_states()
            self._save_runtime()

    def _sync_pob(self):
        ss = self.state.strategy_states.get("pob")
        if not ss or not self._pob:
            return
        with self._pob.lock:
            p = self._pob.data["position"]
            ss.paper_position = (1 if p["side"] == "BUY" else -1) if p else 0
            ss.execution.virtual_position = ss.paper_position
            ss.paper_entry_price = p["entry"] if p else None
            ss.paper_entry_side = p["side"] if p else None
            ss.paper_sl = p["sl"] if p else None
            ss.volume = p["volume"] if p else 0.0
            ss.target = p["levels"]["t3"] if p else None
            ss.current_pnl = 0.0
            if p and core.mt5 is not None:
                tick = core.mt5.symbol_info_tick(self._strategy_symbol(ss.strategy))
                if tick:
                    price = float(tick.bid if p["side"] == "BUY" else tick.ask)
                    ss.current_pnl = (price - p["entry"]) * ss.paper_position * ss.volume * self._strategy_contract(ss.strategy)
            if p and self.live_enabled:
                ss.current_pnl = p.get("unrealized_pnl", 0.0)
            ss.realized_pnl = sum(float(e.get("pnl") or 0) for e in self._pob.data["events"])
            ss.session_realized_pnl = ss.realized_pnl - self._pob_session_baseline
            with self._lock:
                known = {e.get("event_id") for e in self.state.trades}
                events = [e.copy() for e in self._pob.data["events"][-200:] if e["event_id"] not in known and e["event_id"] not in self._cleared_pob_events]
                if events:
                    self.state.trades = sorted(self.state.trades + events, key=lambda e: e["time"], reverse=True)[:200]
                    self._save_trade_history()

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
        self._authorize_strategies(self.state.strategies, {name: self._strategy_settings(name) for name in self.state.strategies})
        self.state.entries_enabled = True
        self.state.safety_halt_reason = None
        for ss in self.state.strategy_states.values():
            ss.execution.entries_enabled = not ss.session_target_reached

    def close_strategy(self, strategy: str):
        """Close only positions owned by one Chandra strategy."""
        if strategy not in STRATEGIES:
            raise ValueError("Invalid strategy")
        if strategy == "pob" and self._pob and self._pob.data["config"]:
            self._pob.bind(self._strategy_symbol(strategy))
            if self.live_enabled:
                with self._pob.lock:
                    self._pob.reconcile(core.ensure_symbol(self._strategy_symbol(strategy)))
            closed = self._pob.close()
            self._sync_pob()
            return {"ok": True, "closed": int(closed), "message": "POB close confirmed" if closed else "POB close awaiting confirmation, pending request, or already flat"}
        ss = self.state.strategy_states.get(strategy)
        if ss is None:
            return {"ok": True, "closed": 0, "message": f"{DISPLAY_NAME[strategy]} is not enabled"}

        if not self.live_enabled:
            if ss.paper_position == 0:
                return {"ok": True, "closed": 0, "message": f"No {DISPLAY_NAME[strategy]} paper position is open"}
            side = ss.paper_entry_side or ("BUY" if ss.paper_position == 1 else "SELL")
            price = self._paper_price("SELL" if side == "BUY" else "BUY", strategy) or ss.paper_entry_price
            self._paper_close(ss, price, "MANUAL CLOSE")
            ss.execution.virtual_position = 0
            return {"ok": True, "closed": 1, "strategy": strategy, "message": f"{DISPLAY_NAME[strategy]} paper position closed"}

        positions = self._live_positions(ss.execution.magic_number)
        if not positions:
            self._sync_live_snapshot(ss)
            return {"ok": True, "closed": 0, "message": f"No {DISPLAY_NAME[strategy]} live position is open"}
        info = core.ensure_symbol(self._strategy_symbol(strategy))
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

    def _resolve_mt5_symbol(self, requested=None):
        requested = (requested or self.state.symbol).strip()
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
            primary = self.state.strategies[0] if self.state.strategies else "manual"
            self.state.mt5_symbol = self._resolve_mt5_symbol(self._strategy_settings(primary)["symbol"])
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

    def resume_after_server_restart(self):
        """Resume only a checkpoint that was running, using normal start checks."""
        if not self._restart_pending or self.state.running:
            return False
        try:
            self.start()
            return True
        except Exception as exc:
            # Keep the dashboard available and preserve the checkpoint for retry.
            self.state.error = "Automatic recovery could not start: " + str(exc)
            return False

    def start(self):
        if self._restart_error:
            raise RuntimeError(self._restart_error)
        resuming = self._restart_pending
        if self.state.running:
            raise RuntimeError("Engine is already running")
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Previous engine is still stopping")
        self._authorize_strategies(self.state.strategies, {name: self._strategy_settings(name) for name in self.state.strategies})
        result = self.mt5_test()
        if not result.get("connected"):
            raise RuntimeError(result.get("error", "MT5 connection failed"))
        if resuming and self.live_enabled and self._restart_identity and self._restart_identity != [str(self.state.account.get("login")), str(self.state.account.get("server"))]:
            raise RuntimeError("Saved session belongs to another MT5 account/server")
        if self._pob_load_error and "pob" in self.state.strategies:
            raise RuntimeError("POB recovery journal needs review: " + self._pob_load_error)
        self.state.strategy_symbols, self.state.strategy_contract_sizes = self._resolve_strategy_markets(
            {name: self._strategy_settings(name) for name in self.state.strategies})
        if "pob" in self.state.strategies:
            self._pob.bind(self._strategy_symbol("pob"))
            if self.live_enabled:
                with self._pob.lock:
                    self._pob.reconcile(core.ensure_symbol(self._strategy_symbol("pob")))
            self._pob_session_baseline = sum(float(e.get("pnl") or 0) for e in self._pob.data["events"])
        try:
            self._stop.clear()
            self.state.error = None
            self.state.running = True
            if not resuming:
                self.state.session_started_at = self._mt5_chart_log_time()
                self.state.entries_enabled = True
                self.state.safety_halt_reason = None
                for ss in self.state.strategy_states.values():
                    ss.session_realized_points = 0.0
                    ss.session_target_reached = False
                    ss.target_close_message = None
                self._reset_strategy_states()
            if self.live_enabled:
                # A position opened by an earlier run belongs to its earlier run.
                # Snapshot it now so only movement after this Start Bot action is
                # counted towards this run's target.
                for ss in self.state.strategy_states.values():
                    if ss.strategy == "pob":
                        self._sync_pob()
                        continue
                    if resuming:
                        self._reconcile_live_snapshot(ss)
                    else:
                        self._sync_live_snapshot(ss)
                    if not resuming and ss.paper_position != 0:
                        ss.session_open_pnl_baseline = ss.current_pnl
                        ss.session_open_points_baseline = self._strategy_open_points(ss)
            self._restore_realized_pnl()
            if self._manual_state().paper_position == 0:
                self._reset_manual_state()
            self._load_trade_history()
            self._sync_pob()
            self._save_runtime()
            self._restart_pending = False
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        except Exception:
            self.state.running = False
            self._restart_pending = resuming
            raise

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                raise RuntimeError("Engine is still stopping; wait before restarting")
        self.state.running = False
        self._restart_pending = False
        self._save_runtime()

    def _paper_price(self, side, strategy="manual"):
        tick = core.mt5.symbol_info_tick(self._strategy_symbol(strategy))
        if not tick:
            return None
        return float(tick.ask if side == "BUY" else tick.bid)

    def _paper_sl_from_l100(self, side, l100, strategy="manual"):
        if l100 is None:
            return None
        try:
            info = core.ensure_symbol(self._strategy_symbol(strategy))
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
        self._cleared_pob_events = set()
        marker = HISTORY_FILE.with_name("trade_log_clear.json")
        if marker.exists():
            # Do not silently re-import cleared recovery events if metadata is corrupt.
            self._cleared_pob_events = set(json.loads(marker.read_text(encoding="utf-8"))["pob_event_ids"])
        try:
            if HISTORY_FILE.exists():
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.state.trades = data[:200]
        except Exception:
            self.state.trades = []

    def clear_trade_history(self):
        """Clear the log, retaining the separate POB execution journal."""
        # Match _sync_pob's lock order so imports cannot race with clearing.
        with self._pob.lock if self._pob else self._lock:
            with self._lock:
                ignored = set(self._cleared_pob_events)
                if self._pob:
                    ignored.update(e["event_id"] for e in self._pob.data["events"])
                HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                marker = HISTORY_FILE.with_name("trade_log_clear.json")
                marker_tmp = marker.with_name(marker.name + "." + uuid.uuid4().hex + ".tmp")
                history_tmp = HISTORY_FILE.with_name(HISTORY_FILE.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    marker_tmp.write_text(json.dumps({"pob_event_ids": sorted(ignored)}), encoding="utf-8")
                    history_tmp.write_text("[]", encoding="utf-8")
                    marker_tmp.replace(marker)
                    history_tmp.replace(HISTORY_FILE)
                finally:
                    marker_tmp.unlink(missing_ok=True)
                    history_tmp.unlink(missing_ok=True)
                self._cleared_pob_events = ignored
                self.state.trades = []
                return {"ok": True, "message": "Saved trade logs cleared."}

    def _save_trade_history(self):
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_FILE.write_text(json.dumps(self.state.trades[:200], indent=2), encoding="utf-8")
        except Exception:
            pass

    def _mt5_chart_log_time(self):
        """Return a timestamp in the same clock shown by the MT5 chart."""
        try:
            rates = core.mt5.copy_rates_from_pos(
                self.state.mt5_symbol,
                core.timeframe_to_mt5(self.state.timeframe),
                0,
                1,
            )
            if rates is not None and len(rates):
                # MT5's rate timestamp is the chart candle's timestamp.  Keep
                # it naive instead of applying a computer/IST conversion.
                return pd.to_datetime(int(rates[-1]["time"]), unit="s").to_pydatetime().isoformat(
                    timespec="seconds"
                )
        except Exception:
            pass
        return datetime.now().isoformat(timespec="seconds")

    def _record(self, strategy, side, action, price, status, pnl=None, message="", sl=None, target=None, volume=None, trade_id=None, ticket=None):
        rec = TradeRecord(
            self._mt5_chart_log_time(), strategy, side, action,
            float(price), float(volume if volume is not None else self._strategy_lot(strategy)), self.state.mode, status, pnl, message, sl, target
        )
        item = rec.__dict__.copy()
        item["symbol"] = self._strategy_symbol(strategy)
        item["timeframe"] = self._strategy_timeframe(strategy)
        item["trade_id"] = trade_id
        item["ticket"] = str(ticket) if ticket is not None else None
        with self._lock:
            self.state.trades.insert(0, item)
            self.state.trades = self.state.trades[:200]
            self._save_trade_history()

    def reconcile_saved_trade_logs(self):
        """Read broker exits for orphaned logs; never place orders or start a strategy."""
        with self._config_lock:
            if self._restart_pending:
                return 0  # Restore session counters before reconciling orphaned logs.
            now = time.monotonic()
            if now - getattr(self, "_last_log_reconcile", -100) < 15:
                return 0
            self._last_log_reconcile = now
            with self._lock:
                rows = list(self.state.trades)
            closed = {str(r.get("ticket")) for r in rows if r.get("mode") == "live" and r.get("action") == "CLOSE"}
            pending = {str(r["ticket"]): r for r in rows if r.get("mode") == "live" and r.get("action") == "OPEN" and r.get("ticket") and str(r["ticket"]) not in closed}
            # A running bot may have older orphaned OPEN rows. Exclude only
            # tickets currently owned by execution reconciliation, not all logs.
            owned = set()
            for ss in [*self.state.strategy_states.values(), self.state.manual_state]:
                if ss is not None:
                    owned.update(str(t) for t in (ss.live_ticket, (ss.live_exit_pending or {}).get("ticket")) if t)
            pending = {ticket: row for ticket, row in pending.items() if ticket not in owned and row.get("strategy") != "pob"}
            missing = {str(r["ticket"]) for r in rows if r.get("strategy") == "pob" and r.get("mode") == "live" and r.get("ticket") and not r.get("symbol")}
            if not (pending or missing) or not core.mt5.initialize():
                return 0
            symbols = {}
            for ticket in missing:
                deals = core.mt5.history_deals_get(position=int(ticket))
                matches = {d.symbol for d in (deals or []) if d.magic == MAGIC_BY_STRATEGY["pob"]}
                if len(matches) == 1:
                    symbols[ticket] = matches.pop()
            if symbols:
                with self._lock:
                    for item in self.state.trades:
                        if item.get("strategy") == "pob" and item.get("mode") == "live" and not item.get("symbol") and str(item.get("ticket")) in symbols:
                            item["symbol"] = symbols[str(item["ticket"])]
                    self._save_trade_history()
            recovered = []
            for ticket, row in pending.items():
                positions = core.mt5.positions_get(ticket=int(ticket))
                if positions is None or positions:
                    continue
                deals = core.mt5.history_deals_get(position=int(ticket))
                if not deals:
                    continue
                # Validate the saved entry too: tickets alone are not globally unique.
                entries = [d for d in deals if d.entry == getattr(core.mt5, "DEAL_ENTRY_IN", 0)]
                expected_type = 0 if row.get("side") == "BUY" else 1
                if not any(d.symbol == row.get("symbol") and d.type == expected_type and
                           abs(float(d.price)-float(row["price"])) < .000001 and
                           d.magic == MAGIC_BY_STRATEGY.get(row.get("strategy")) for d in entries):
                    continue
                exits = [d for d in deals if d.entry in (getattr(core.mt5, "DEAL_ENTRY_OUT", 1), getattr(core.mt5, "DEAL_ENTRY_OUT_BY", 3))]
                if not exits or sum(d.volume for d in exits) + 1e-8 < sum(d.volume for d in entries):
                    continue
                last = max(exits, key=lambda d: d.time_msc)
                total = sum(float(d.profit) for d in exits)
                # Existing partial rows already contribute to the displayed total.
                partial = sum(float(r.get("pnl") or 0) for r in rows if str(r.get("ticket")) == ticket and r.get("mode") == "live" and r.get("action") == "PARTIAL_CLOSE")
                item = dict(row, action="CLOSE", status="LIVE", price=float(last.price),
                            pnl=total-partial, time=pd.to_datetime(last.time, unit="s").isoformat(),
                            message="LIVE — recovered broker exit: " + (last.comment or "Broker exit"))
                recovered.append(item)
            if recovered:
                with self._lock:
                    self.state.trades = (recovered + self.state.trades)[:200]
                    self._save_trade_history()
                self._restore_realized_pnl()
            return len(recovered)

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
        ss.paper_sl = sl_override if sl_override is not None else self._paper_sl_from_l100(side, l100, ss.strategy)
        ss.target = target_override
        ss.volume = float(self._strategy_lot(ss.strategy))
        ss.open_trade_time = self._mt5_chart_log_time()
        self._record(ss.strategy, side, "OPEN", price, "SIMULATED", message="PAPER — NOT SENT TO BROKER", sl=ss.paper_sl, target=ss.target, trade_id=ss.active_trade_id)

    def _paper_close(self, ss: StrategyState, price, reason="EXIT"):
        if ss.paper_position == 0 or ss.paper_entry_price is None:
            return
        entry = ss.paper_entry_price
        side = ss.paper_entry_side
        pnl = ((price - entry) if side == "BUY" else (entry - price)) * (ss.volume or self._strategy_lot(ss.strategy)) * self._strategy_contract(ss.strategy)
        if ss.strategy in STRATEGIES:
            ss.session_realized_points += (price - entry) if side == "BUY" else (entry - price)
            ss.session_realized_pnl += pnl
        ss.realized_pnl += pnl
        ss.current_pnl = 0.0
        self._record(ss.strategy, side, "CLOSE", price, "SIMULATED", pnl=pnl,
                     message=f"PAPER — {reason} — NOT SENT TO BROKER", sl=ss.paper_sl, target=ss.target, volume=ss.volume or self._strategy_lot(ss.strategy), trade_id=ss.active_trade_id)
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
        if getattr(ss, "live_exit_pending", None):
            return False
        if self.live_enabled:
            diagnostic = {}
            opened = core.open_position(
                self._strategy_symbol(ss.strategy), side, self._strategy_lot(ss.strategy), True,
                sl=float(sl), magic_number=ss.execution.magic_number, diagnostic=diagnostic,
            )
            ss.timing_entry_message = None if opened else (
                f"{side} entry not confirmed: {diagnostic.get('message', 'MT5 did not confirm the order; check server output')}"
            )
            return opened
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
        tick = core.mt5.symbol_info_tick(self._strategy_symbol(ss.strategy))
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
        name = next((name for name, value in MAGIC_BY_STRATEGY.items() if value == magic), "manual")
        positions = core.mt5.positions_get(symbol=self._strategy_symbol(name))
        if positions is None:
            raise RuntimeError("MT5 position snapshot unavailable; cannot reconcile live trades")
        return [p for p in positions if p.magic == magic]

    def _record_live_close(self, ss: StrategyState):
        """Record a broker-side exit (SL/TP/manual) after a live position disappears."""
        pending = ss.live_exit_pending or {}
        ticket = pending.get("ticket") or ss.live_ticket
        trade_id = pending.get("trade_id") or ss.active_trade_id
        if ticket and not trade_id:
            existing = self._find_open_trade_by_ticket(ticket)
            trade_id = existing.get("trade_id") if existing else None
        if not ticket or not trade_id:
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
            side = pending.get("side") or ss.paper_entry_side or ("BUY" if ss.paper_position == 1 else "SELL")
            entry_price = pending.get("entry_price", ss.paper_entry_price)
            sl = pending.get("sl", ss.paper_sl)
            target = pending.get("target", ss.target)
            volume = pending.get("volume", ss.volume) or self._strategy_lot(ss.strategy)
            if ss.strategy in STRATEGIES and entry_price is not None:
                ss.session_realized_points += (
                    (price - entry_price) if side == "BUY" else (entry_price - price)
                ) - ss.session_open_points_baseline
                ss.session_realized_pnl += pnl - ss.session_open_pnl_baseline
            comment = str(getattr(exit_deal, "comment", "") or "").strip()
            reason = comment or "BROKER EXIT"
            self._record(
                ss.strategy, side,
                "CLOSE", price, "LIVE", pnl=pnl,
                message=f"LIVE — {reason}", sl=sl, target=target,
                volume=volume, trade_id=trade_id, ticket=ticket
            )
            ss.realized_pnl += pnl
            ss.active_trade_id = None
            ss.live_ticket = None
            ss.live_exit_pending = None
            ss.session_open_pnl_baseline = 0.0
            ss.session_open_points_baseline = 0.0
            return True
        except Exception:
            return False

    def _sync_live_snapshot(self, ss: StrategyState, positions=None):
        positions = self._live_positions(ss.execution.magic_number) if positions is None else positions
        # A new ticket may already exist when we poll again. Never attach it
        # to the previous OPEN or discard that position's unrecorded exit.
        current_ticket = str(positions[0].ticket) if positions else None
        if ss.live_ticket and current_ticket != ss.live_ticket:
            if ss.live_exit_pending is None:
                ss.live_exit_pending = {
                    "ticket": ss.live_ticket, "trade_id": ss.active_trade_id,
                    "side": ss.paper_entry_side, "entry_price": ss.paper_entry_price,
                    "sl": ss.paper_sl, "target": ss.target, "volume": ss.volume,
                }
            if not self._record_live_close(ss):
                # History can arrive after the position disappears. Retain
                # its identity and block another entry until it is accounted.
                return
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
                        volume=ss.volume or self._strategy_lot(ss.strategy), trade_id=ss.active_trade_id, ticket=ss.live_ticket
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

    def _reconcile_live_snapshot(self, ss: StrategyState):
        """Refresh one strategy from MT5 and retain broker-side exits in the log.

        A stop loss is executed by MT5, not by our order code.  Therefore the
        position can disappear between candles; detect that on every polling
        cycle and record the close *before* the snapshot clears its entry and
        stop-loss details.
        """
        positions = self._live_positions(ss.execution.magic_number)
        self._sync_live_snapshot(ss, positions=positions)

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
            ss.open_trade_time = self._mt5_chart_log_time()
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
        ss.current_pnl = ((current - ss.paper_entry_price) if ss.paper_position == 1 else (ss.paper_entry_price - current)) * (ss.volume or self._strategy_lot(ss.strategy)) * self.state.contract_size

    def _refresh_paper_pnl(self):
        """Mark open paper trades on every poll, independently of candle closes."""
        if self.live_enabled:
            return
        for name, ss in self.state.strategy_states.items():
            if name == "pob":
                continue  # POB is valued by its own execution journal.
            if not ss.paper_position:
                ss.current_pnl = 0.0
                continue
            if ss.paper_entry_price is None:
                continue
            tick = core.mt5.symbol_info_tick(self._strategy_symbol(name))
            if tick is None:
                continue
            price = float(tick.bid if ss.paper_position == 1 else tick.ask)
            if not math.isfinite(price) or price <= 0:
                continue
            ss.current_pnl = (price - ss.paper_entry_price) * ss.paper_position * (
                ss.volume or self._strategy_lot(name)) * self._strategy_contract(name)

    def _strategy_open_points(self, ss: StrategyState):
        """Return floating points for one bot strategy (manual is excluded)."""
        if ss.paper_position == 0 or core.mt5 is None:
            return 0.0
        tick = core.mt5.symbol_info_tick(self._strategy_symbol(ss.strategy))
        if tick is None:
            return 0.0
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        if ss.paper_position == 1 and ss.paper_entry_price is not None and bid > 0:
            return bid - ss.paper_entry_price
        if ss.paper_position == -1 and ss.paper_entry_price is not None and ask > 0:
            return ss.paper_entry_price - ask
        return 0.0

    def _enforce_strategy_targets(self):
        """Close only the strategy whose optional session target is met."""
        reached = False
        for name, ss in self.state.strategy_states.items():
            target = ss.session_target_points
            net_points = ss.session_realized_points + self._strategy_open_points(ss) - ss.session_open_points_baseline
            if target is None or (not ss.session_target_reached and net_points < target):
                continue
            ss.session_target_reached = True
            ss.execution.entries_enabled = False
            try:
                # Keep trying after rejection or temporary broker failure, even
                # if price drops back below the target. Never reopen entries.
                self.close_strategy(name)
                if self.live_enabled:
                    remaining = bool(self._live_positions(ss.execution.magic_number))
                    pending = bool(ss.live_exit_pending)
                else:
                    remaining, pending = bool(ss.paper_position), False
                ss.target_close_message = (
                    "Target reached — closing position; awaiting confirmation" if remaining else
                    "Target reached — awaiting exit record" if pending else
                    "Target reached — position closed; entries stopped")
            except Exception as exc:
                ss.target_close_message = f"Target reached — close pending: {exc}"
            reached = True
        return reached

    def _timing_local_dt(self, value):
        """Use the timestamp shown by the MT5 chart for Timing Candle rules.

        MT5 rates arrive as naive datetimes whose clock already matches the
        chart. Treating them as UTC and adding +05:30 selects the wrong candle.
        """
        if isinstance(value, pd.Timestamp):
            value = value.to_pydatetime()
        if value.tzinfo is None:
            return value
        return value.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)

    def _timing_option(self, ss, key):
        if ss.strategy == "timing2":
            defaults = dict(timing_hour=4, timing_minute=0, timing_end_time="08:00", timing_entry_mode="retest")
            return self.state.strategy_settings.get("timing2", {}).get(key, defaults[key])
        return getattr(self.state, key)

    def _timing_reset_if_needed(self, ss: StrategyState, local_dt):
        day = local_dt.date().isoformat()
        if ss.timing_date != day:
            ss.timing_date = day
            ss.timing_high = None
            ss.timing_low = None
            ss.timing_armed_buy = False
            ss.timing_armed_sell = False
            ss.timing_arm_time = None
            ss.timing_last_quote = None

    def _restore_timing_levels(self, ss: StrategyState, local_dt):
        """Restore today's completed timing candle when the bot starts late.

        The normal loop only sees the newest completed bar.  Consequently, a
        bot started after the configured time must explicitly read that day's
        historical timing bar before it can look for BO/BD.
        """
        if ss.timing_high is not None or ss.timing_low is not None:
            return

        timing_dt = local_dt.replace(
            hour=self._timing_option(ss, "timing_hour"),
            minute=self._timing_option(ss, "timing_minute"),
            second=0,
            microsecond=0,
        )
        if local_dt < timing_dt:
            return

        try:
            bar_minutes = core.timeframe_minutes(self._strategy_timeframe(ss.strategy))
            # Ask for a broad window because MT5 history requests use UTC
            # while the matching below deliberately uses the chart clock.
            rates = core.get_rates_range(
                self._strategy_symbol(ss.strategy),
                self._strategy_timeframe(ss.strategy),
                (timing_dt - timedelta(days=1)).replace(tzinfo=timezone.utc),
                (timing_dt + timedelta(days=1, minutes=bar_minutes)).replace(tzinfo=timezone.utc),
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

    def _enforce_timing_session(self, ss: StrategyState):
        """Use the current MT5 M1 bar's chart clock, not the last closed bar.

        A missing chart clock blocks entries. A cutoff close remains pending
        until MT5 confirms flat, including across a day change.
        """
        end_time = self._timing_option(ss, "timing_end_time")
        if not end_time:
            ss.timing_session_blocked = False
            ss.timing_session_message = None
            return
        try:
            rates = core.mt5.copy_rates_from_pos(
                self._strategy_symbol(ss.strategy), core.timeframe_to_mt5("M1"), 0, 1)
            if rates is None or not len(rates):
                raise RuntimeError("MT5 chart time unavailable")
            now = datetime.fromtimestamp(int(rates[-1]["time"]), timezone.utc).replace(tzinfo=None)
            self._timing_reset_if_needed(ss, now)
            end_hour, end_minute = map(int, end_time.split(":"))
            minutes = now.hour * 60 + now.minute
            ended = minutes >= end_hour * 60 + end_minute
            before_start = minutes < self._timing_option(ss, "timing_hour") * 60 + self._timing_option(ss, "timing_minute")
            ss.timing_session_blocked = ended or before_start or ss.timing_close_pending
            if ended:
                ss.timing_close_pending = True
            ss.timing_session_message = (
                f"End time {end_time} reached — entries closed" if ended else
                "Waiting for Timing Candle session" if before_start else None)
        except Exception as exc:
            ss.timing_session_blocked = True
            ss.timing_session_message = f"Timing entries blocked: {exc}"
        if ss.timing_session_blocked:
            ss.timing_armed_buy = ss.timing_armed_sell = False
            ss.timing_arm_time = None
            ss.timing_last_quote = None
        if not ss.timing_close_pending:
            return
        try:
            if self.live_enabled:
                # None is an unavailable snapshot, not proof that we are flat.
                positions = core.mt5.positions_get(symbol=self._strategy_symbol(ss.strategy))
                if positions is None:
                    raise RuntimeError("MT5 positions unavailable")
                owned = [p for p in positions if p.magic == ss.execution.magic_number]
                if owned:
                    info = core.ensure_symbol(self._strategy_symbol(ss.strategy))
                    for position in owned:
                        core.close_position(position, info, True, ss.execution.magic_number)
                    remaining = core.mt5.positions_get(symbol=self._strategy_symbol(ss.strategy))
                    if remaining is None or any(p.magic == ss.execution.magic_number for p in remaining):
                        raise RuntimeError("End-time close awaiting broker confirmation; will retry")
                    self._record_live_close(ss)
                    self._sync_live_snapshot(ss)
            elif ss.paper_position:
                price = self._paper_price("SELL" if ss.paper_position == 1 else "BUY", ss.strategy)
                if price is None or not math.isfinite(price) or price <= 0:
                    raise RuntimeError("End-time close awaiting a valid quote")
                self._paper_close(ss, price, "TIMING END TIME")
                ss.execution.virtual_position = 0
            ss.timing_close_pending = False
        except Exception as exc:
            ss.timing_session_message = f"Timing end-time exit pending: {exc}"

    def _process_timing_direct_tick(self, ss: StrategyState):
        """Enter once per fresh live crossing; an outside price alone is not a signal."""
        if getattr(ss, "timing_session_blocked", False):
            return None
        if self._timing_option(ss, "timing_entry_mode") != "direct" or ss.strategy not in ("timing", "timing2"):
            return None
        tick = core.mt5.symbol_info_tick(self._strategy_symbol(ss.strategy))
        if tick is None or ss.timing_high is None or ss.timing_low is None:
            ss.timing_last_quote = None
            return None
        bid, ask = float(tick.bid), float(tick.ask)
        if not all(math.isfinite(v) and v > 0 for v in (bid, ask)) or bid > ask:
            ss.timing_last_quote = None
            return None
        previous = ss.timing_last_quote
        ss.timing_last_quote = (bid, ask)
        if (previous is None or not self.state.entries_enabled
                or not ss.execution.entries_enabled):
            return None
        side = ("BUY" if ask > ss.timing_high and previous[1] <= ss.timing_high else
                "SELL" if bid < ss.timing_low and previous[0] >= ss.timing_low else None)
        if side is None:
            return None
        if self.live_enabled:
            # The broker can execute SL between the loop's position snapshot
            # and this quote. Reconcile at the crossing before deciding entry.
            self._reconcile_live_snapshot(ss)
        if ss.execution.virtual_position != 0 or getattr(ss, "live_exit_pending", None):
            ss.timing_entry_message = (
                f"{side} crossing skipped: previous exit awaiting broker history"
                if getattr(ss, "live_exit_pending", None) else
                f"{side} crossing skipped: a position is still open"
            )
            return None
        price = ask if side == "BUY" else bid
        sl = ss.timing_low if side == "BUY" else ss.timing_high
        if not self._place_timing_pending(ss, side, price, sl):
            return None
        ss.execution.virtual_position = 1 if side == "BUY" else -1
        ss.timing_armed_buy = ss.timing_armed_sell = False
        ss.timing_arm_time = None
        return f"{DISPLAY_NAME[ss.strategy]}: {side} (live crossing)"

    def _process_timing_retest_tick(self, ss: StrategyState, live_row=None):
        """Enter immediately when a future live price retests an armed level."""
        if (
            getattr(ss, "timing_session_blocked", False)
            or self._timing_option(ss, "timing_entry_mode") != "retest"
            or ss.strategy not in ("timing", "timing2")
            or ss.execution.virtual_position != 0
            or not self.state.entries_enabled
            or not ss.execution.entries_enabled
            or not (ss.timing_armed_buy or ss.timing_armed_sell)
        ):
            return None

        tick = core.mt5.symbol_info_tick(self._strategy_symbol(ss.strategy))
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
        return f"{DISPLAY_NAME[ss.strategy]}: {side}"

    def _process_timing_candle(self, ss: StrategyState, row):
        """Process the configurable India-time timing-candle strategy."""
        local_dt = self._timing_local_dt(row["time"])
        self._timing_reset_if_needed(ss, local_dt)
        close = float(row["close"]); high = float(row["high"]); low = float(row["low"])
        is_timing = local_dt.hour == self._timing_option(ss, "timing_hour") and local_dt.minute == self._timing_option(ss, "timing_minute")
        bar_key = str(row["time"])
        triggered = None

        # The timing candle itself only establishes the reference levels.
        if is_timing:
            ss.timing_high = high
            ss.timing_low = low
            ss.timing_armed_buy = False
            ss.timing_armed_sell = False
            ss.timing_arm_time = None
            ss.timing_last_quote = None
            return None

        self._restore_timing_levels(ss, local_dt)
        if ss.timing_high is None or ss.timing_low is None:
            return None

        if getattr(ss, "timing_session_blocked", False) or self._timing_option(ss, "timing_entry_mode") == "direct":
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

    def _process_strategy_bar(self, name, ss, row, signal):
        triggered = []
        ss.last_l100 = float(signal.l100) if signal.l100 is not None else None
        if not self.live_enabled:
            self._paper_check_sl(ss, row)
            ss.execution.virtual_position = ss.paper_position
        else:
            before_live = self._live_positions(ss.execution.magic_number)
            self._sync_live_snapshot(ss)
        before = ss.execution.virtual_position
        if name in ("timing", "timing2"):
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
                tick = core.mt5.symbol_info_tick(self._strategy_symbol(ss.strategy))
                if tick and ss.paper_entry_price is not None:
                    current = float(tick.bid if after == 1 else tick.ask)
                    ss.current_pnl = ((current - ss.paper_entry_price) if after == 1 else (ss.paper_entry_price - current)) * (ss.volume or self._strategy_lot(ss.strategy)) * self._strategy_contract(ss.strategy)
            return triggered
        if self.live_enabled and not before_live and ss.live_ticket:
            # The broker may have hit SL/TP between polling bars.
            # Record that exit before allowing a new signal to open.
            self._record_live_close(ss)
        core.process_candle(
            self._strategy_symbol(ss.strategy), row, signal, self._strategy_lot(name),
            self.live_enabled, ss.execution
        )
        after = ss.execution.virtual_position
        if not self.live_enabled:
            if before == 0 and after != 0:
                side = "BUY" if after == 1 else "SELL"
                self._paper_open(ss, side, self._paper_price(side, name) or float(row["close"]), signal.l100)
                triggered.append(f"{DISPLAY_NAME[name]}: {side}")
            elif before != 0 and after == 0:
                side = "BUY" if before == 1 else "SELL"
                self._paper_close(ss, self._paper_price("SELL" if before == 1 else "BUY", name) or float(row["close"]), "SIGNAL EXIT")
            elif before != after and before != 0 and after != 0:
                side = "BUY" if after == 1 else "SELL"
                self._paper_open(ss, side, self._paper_price(side, name) or float(row["close"]), signal.l100)
                triggered.append(f"{DISPLAY_NAME[name]}: {side}")
            elif after != 0:
                # Strategic Entry keeps the initial L100 +/- 5 SL fixed.
                # Magical retains its existing L100 synchronization.
                if name == "magical":
                    ss.paper_sl = self._paper_sl_from_l100("BUY" if after == 1 else "SELL", signal.l100, name)
                tick = core.mt5.symbol_info_tick(self._strategy_symbol(ss.strategy))
                if tick and ss.paper_entry_price is not None:
                    current = float(tick.bid if after == 1 else tick.ask)
                    ss.current_pnl = ((current - ss.paper_entry_price) if after == 1 else (ss.paper_entry_price - current)) * (ss.volume or self._strategy_lot(ss.strategy)) * self._strategy_contract(ss.strategy)
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
                    volume=float(getattr(position, "volume", self._strategy_lot(name)) or self._strategy_lot(name)),
                    trade_id=ss.active_trade_id, ticket=ss.live_ticket
                )
                triggered.append(f"{DISPLAY_NAME[name]}: {side}")
            elif before_live and not after_live:
                self._record_live_close(ss)
            self._sync_live_snapshot(ss)
        return triggered

    def _run(self):
        engine = ChandraTrendEngine(self.state.mt5_symbol, self.state.timeframe, 3000)
        try:
            engine.initialize()
            while not self._stop.is_set():
                with self._config_lock:
                    timing_states = [ss for name, ss in self.state.strategy_states.items() if name in ("timing", "timing2")]
                    for tss in timing_states:
                        self._enforce_timing_session(tss)
                    manual_ss = self._manual_state()
                    if self.live_enabled:
                        # Keep every UI position in step with broker-side SL/TP
                        # executions.  This cannot wait for the next candle.
                        self._reconcile_live_snapshot(manual_ss)
                        for ss in self.state.strategy_states.values():
                            if ss.strategy != "pob":
                                self._reconcile_live_snapshot(ss)
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
                    pss = self.state.strategy_states.get("pob")
                    if pss is not None:
                        self._pob.poll(None, self.state.entries_enabled and pss.execution.entries_enabled)
                        self._sync_pob()
                    # Fetch each timeframe once per cycle, but track completed bars
                    # separately so a newly deployed strategy can start independently.
                    frames = {}
                    triggered = []
                    for name in self.state.strategies:
                        ss = self.state.strategy_states[name]
                        tf = self._strategy_timeframe(name)
                        market = self._strategy_symbol(name)
                        key = (market, tf)
                        if key not in frames:
                            frames[key] = ChandraTrendEngine(market, tf, 3000).calculate_frame()
                        row = frames[key].iloc[-1]
                        bar_time = str(row["time"])
                        if name == "pob":
                            self._pob.poll(row, self.state.entries_enabled and ss.execution.entries_enabled)
                            self._sync_pob()
                            continue
                        if bar_time != ss.last_bar:
                            signal = core.signal_from_row(row)
                            summary = dict(self.state.last_signal or {})
                            if name == "strategic":
                                summary.update(buy_condition=signal.buy_condition, sell_condition=signal.sell_condition,
                                    strategic_buy=signal.strategic_buy, strategic_sell=signal.strategic_sell,
                                    l100=signal.l100, close=signal.close, time=bar_time)
                            elif name == "magical":
                                summary.update(magical_buy=signal.magical_buy, magical_sell=signal.magical_sell,
                                    magical_invalid=signal.magical_invalid)
                            self.state.last_signal = summary
                            triggered.extend(self._process_strategy_bar(name, ss, row, signal))
                            ss.last_bar = bar_time
                        if name in ("timing", "timing2"):
                            tick_trigger = self._process_timing_retest_tick(ss)
                            if tick_trigger:
                                triggered.append(tick_trigger)
                            self.state.last_signal = dict(self.state.last_signal or {}, **{
                                name + "_buy": ss.timing_armed_buy, name + "_sell": ss.timing_armed_sell,
                                name + "_high": ss.timing_high, name + "_low": ss.timing_low})
                    self.state.last_signal = dict(self.state.last_signal or {},
                        triggered=" • ".join(triggered) if triggered else None)
                    # After closed-bar processing, so an intrabar entry cannot be
                    # stopped using the preceding candle's historical range.
                    for tss in timing_states:
                        self._enforce_timing_session(tss)
                        direct_trigger = self._process_timing_direct_tick(tss)
                        if direct_trigger and self.state.last_signal is not None:
                            self.state.last_signal["triggered"] = direct_trigger
                    self._refresh_paper_pnl()
                    self._enforce_strategy_targets()
                    self._save_runtime()
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

    def _overall_pnl(self, strategy_status):
        # Use saved closures, not deployed StrategyState totals: removing a
        # strategy or restarting the bot must not remove its recorded results.
        realized = sum(
            float(t.get("pnl") or 0)
            for t in self.state.trades
            if t.get("action") in ("CLOSE", "PARTIAL_CLOSE")
            and str(t.get("mode", "")).lower() == self.state.mode
        )
        open_pnl = sum(float(v.get("pnl") or 0) for v in strategy_status.values()
                       if v.get("position"))
        return realized + open_pnl

    def status_payload(self):
        data = dict(self.state.__dict__)
        data["runtime_configuration_saved"] = HISTORY_FILE.with_name("strategy_runtime_state.json").exists()
        data["restart_recovery_pending"] = self._restart_pending
        data["restart_recovery_error"] = self._restart_error
        data["pob"] = self._pob.status() if self._pob else {"error": self._pob_load_error}
        strategy_status = {}
        for name, ss in self.state.strategy_states.items():
            strategy_status[name] = {
                "name": DISPLAY_NAME[name],
                "symbol": self._strategy_symbol(name),
                "timeframe": self._strategy_timeframe(name),
                "lot": self._strategy_lot(name),
                "position": ss.paper_position,
                "entry_price": ss.paper_entry_price,
                "entry_side": ss.paper_entry_side,
                "sl": ss.paper_sl,
                "l100": ss.last_l100,
                "pnl": ss.current_pnl,
                "realized_pnl": ss.realized_pnl,
                "total_pnl": ss.realized_pnl + ss.current_pnl,
                "session_realized_pnl": ss.session_realized_pnl,
                "session_total_pnl": ss.session_realized_pnl + ss.current_pnl - ss.session_open_pnl_baseline,
                "timing_session_message": ss.timing_session_message,
                "timing_entry_message": ss.timing_entry_message,
                "timing_last_quote": ss.timing_last_quote,
                "timing_session_blocked": ss.timing_session_blocked,
                "timing_close_pending": ss.timing_close_pending,
                "timing_high": ss.timing_high,
                "timing_low": ss.timing_low,
                "timing_armed_buy": ss.timing_armed_buy,
                "timing_armed_sell": ss.timing_armed_sell,
                "session_target_points": ss.session_target_points,
                "session_realized_points": ss.session_realized_points,
                "session_open_points": self._strategy_open_points(ss) - ss.session_open_points_baseline,
                "session_target_reached": ss.session_target_reached,
                "target_close_message": ss.target_close_message,
                "open_points": self._strategy_open_points(ss),
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
        reason = self._paper_switch_block_reason()
        data["paper_switch_blocked"] = bool(reason)
        data["paper_switch_reason"] = reason
        data["strategy_status"] = strategy_status
        data["combined_pnl"] = self._overall_pnl(strategy_status)
        data["combined_session_pnl"] = sum(v["session_total_pnl"] for v in strategy_status.values() if "session_total_pnl" in v)
        data.pop("strategy_states", None)
        data.pop("manual_state", None)
        return data

manager = BotManager()
