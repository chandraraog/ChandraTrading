from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
from backend.app.engine import chandra_core as core

class StrategicEntryStrategy:
    """Strategy-specific rules. Common runtime services are delegated to BotManager."""
    def __init__(self, manager):
        self.manager = manager

    def __getattr__(self, name):
        return getattr(self.manager, name)

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


    def process_candle(self, row, signal, state, lot, live):
        return self.manager.core.process_candle(
            self.manager.state.mt5_symbol, row, signal, lot, live, state
        )
