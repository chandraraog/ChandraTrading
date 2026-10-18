"""Purity of Breakout levels translated from Chandra's Pine indicator.

The Pine labels deliberately use different long/short T2 formulas. Keep
these calculations separate from order management and chart label offsets.
"""

import math


def calculate_levels(high: float, low: float) -> dict:
    """Return underlying-price levels; high/low may come from selected bars."""
    if any(isinstance(x, bool) or not isinstance(x, (int, float))
           or not math.isfinite(x) for x in (high, low)):
        raise ValueError("POB High and Low must be finite numbers")
    if not high > low > 0:
        raise ValueError("POB High must exceed Low, and both must be positive")
    spread = high - low
    onm = spread * 0.146
    decider = spread * 0.236
    levels = {
        "high": float(high), "low": float(low),
        "buy": {"entry": high + decider, "stop": high - decider,
                "t1": high + 6 * onm, "t2": high + 10 * onm,
                "t3": high + 10 * decider},
        "sell": {"entry": low - decider, "stop": low + decider,
                 "t1": low - 6 * onm, "t2": low - 6 * decider,
                 "t3": low - 10 * decider},
    }
    if any(not math.isfinite(value) or value <= 0
           for side in ("buy", "sell") for value in levels[side].values()):
        raise ValueError("POB range produces non-positive or invalid price levels")
    return levels


def configured_levels(high, low, sl_mode="default"):
    """Preserve Pine targets; optionally use the opposite entry as initial SL."""
    if sl_mode not in ("default", "levels"):
        raise ValueError("POB SL must use Default or Buy/Sell levels")
    levels = calculate_levels(high, low)
    if sl_mode == "levels":
        levels["buy"]["stop"] = levels["sell"]["entry"]
        levels["sell"]["stop"] = levels["buy"]["entry"]
    return levels


class POBStrategy:
    """Completed breakout arms a side; a later live-price retest enters."""

    def __init__(self, high=None, low=None, sl_mode="default"):
        self.levels = configured_levels(high, low, sl_mode) if high is not None and low is not None else None
        self.previous = None
        self.armed = None
        self.arm_time = None
        self.last_time = None
        self.last_quote = None

    def crossing(self, timestamp, bid, ask):
        """Baseline the first quote; trigger only a subsequent live crossing."""
        key = str(timestamp)
        old = self.last_quote
        self.last_quote = {"day": key[:10], "bid": bid, "ask": ask}
        if not self.levels or not old or old["day"] != key[:10]:
            return None
        buy, sell = self.levels["buy"], self.levels["sell"]
        side = ("BUY" if old["ask"] < buy["entry"] <= ask else
                "SELL" if old["bid"] > sell["entry"] >= bid else None)
        if not side:
            return None
        levels = self.levels[side.lower()]
        price = ask if side == "BUY" else bid
        if (side == "BUY" and price >= levels["t2"]) or (side == "SELL" and price <= levels["t2"]):
            return None
        return {"action": "ENTER", "side": side, "pob_levels": dict(levels),
                "spot_stop": levels["stop"], "reason": "POB live crossing"}

    def decide(self, row, position=None):
        key = str(row["time"])
        if key == self.last_time:
            return None
        if self.last_time and key[:10] != self.last_time[:10]:
            self.armed = self.arm_time = None
        self.last_time = key
        self.previous = {k: float(row[k]) for k in ("high", "low", "close")}
        if not self.levels or position:
            self.armed = self.arm_time = None
            return None
        if self.armed and self.arm_time and key > self.arm_time:
            side = self.armed
            levels = self.levels[side.lower()]
            high, low = float(row["high"]), float(row["low"])
            touched = low <= levels["entry"] <= high
            invalid = (low <= levels["stop"] or high >= levels["t2"]) if side == "BUY" else (
                high >= levels["stop"] or low <= levels["t2"])
            if invalid or touched:
                self.armed = self.arm_time = None
                if invalid:
                    return None
                return {"action": "ENTER", "side": side, "spot_stop": levels["stop"],
                        "pob_levels": dict(levels), "reason": "POB candle-close retest fallback"}
        close = float(row["close"])
        buy, sell = self.levels["buy"], self.levels["sell"]
        side = "BUY" if close > buy["entry"] else "SELL" if close < sell["entry"] else None
        if side and (side != self.armed or not self.arm_time):
            self.armed, self.arm_time = side, key
        elif ((self.armed == "BUY" and close <= buy["stop"])
              or (self.armed == "SELL" and close >= sell["stop"])):
            self.armed = self.arm_time = None
        # Only a later candle may confirm the missed retest.
        return None

    def retest(self, timestamp, price):
        key = str(timestamp)
        if (not self.armed or not self.arm_time or not self.levels
                or key <= self.arm_time or key[:10] != self.arm_time[:10]
                or not math.isfinite(price)):
            return None
        side = self.armed
        levels = self.levels[side.lower()]
        if (side == "BUY" and price <= levels["stop"]) or (side == "SELL" and price >= levels["stop"]):
            self.armed = self.arm_time = None
            return None
        if (side == "BUY" and price >= levels["t2"]) or (side == "SELL" and price <= levels["t2"]):
            self.armed = self.arm_time = None
            return None
        touched = price <= levels["entry"] if side == "BUY" else price >= levels["entry"]
        if not touched:
            return None
        self.armed = self.arm_time = None
        return {"action": "ENTER", "side": side, "spot_stop": levels["stop"],
                "pob_levels": dict(levels), "retest_level": levels["entry"],
                "reason": "POB live-price retest"}
