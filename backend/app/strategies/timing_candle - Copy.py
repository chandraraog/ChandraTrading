"""Timing Candle strategy rules.

Workflow:
1. A completed Timing Candle locks HIGH and LOW.
2. A later completed candle closing above HIGH confirms BO and arms BUY.
3. A later completed candle closing below LOW confirms BD and arms SELL.
4. After BO/BD, a later price touch of the corresponding level triggers entry
   immediately (no requirement for the retest candle to close).
5. BUY uses Timing LOW as the initial SL; SELL uses Timing HIGH as the initial SL.

This module contains only pure decision rules. It does not know about MT5,
paper execution, BotManager, UI, or persistence. Prices retain raw precision.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TimingDecision:
    side: str
    reason: str


def breakout_arm(
    *,
    close: float,
    timing_high: Optional[float],
    timing_low: Optional[float],
    bar_key: str,
    already_armed_buy: bool = False,
    already_armed_sell: bool = False,
    position_flat: bool = True,
    entries_enabled: bool = True,
):
    """Return BUY/SELL when a completed candle closes outside the reference."""
    if not position_flat or not entries_enabled:
        return None
    if timing_high is None or timing_low is None:
        return None

    close = float(close)

    if close > float(timing_high) and not already_armed_buy:
        return TimingDecision("BUY", f"BO CLOSE > TIMING HIGH | bar={bar_key}")

    if close < float(timing_low) and not already_armed_sell:
        return TimingDecision("SELL", f"BD CLOSE < TIMING LOW | bar={bar_key}")

    return None


def retest_signal(
    *,
    armed_buy: bool,
    armed_sell: bool,
    timing_high: Optional[float],
    timing_low: Optional[float],
    bar_key: Optional[str],
    arm_time: Optional[str],
    live_bar_key: Optional[str] = None,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    candle_high: Optional[float] = None,
    candle_low: Optional[float] = None,
):
    """Return a retest decision for a FUTURE price touch or completed candle.

    BUY  = after a closed-candle BO, a later live price touching/crossing
           Timing High triggers immediately; a later completed candle whose
           low reaches Timing High also proves the retest.

    SELL = after a closed-candle BD, a later live price touching/crossing
           Timing Low triggers immediately; a later completed candle whose
           high reaches Timing Low also proves the retest.

    The breakout candle itself can never be treated as its own retest.
    """
    if timing_high is None or timing_low is None:
        return None

    high = float(timing_high)
    low = float(timing_low)

    breakout_bar = (
        bar_key is not None
        and arm_time is not None
        and str(bar_key) == str(arm_time)
    )

    live_breakout_bar = (
        live_bar_key is not None
        and arm_time is not None
        and str(live_bar_key) == str(arm_time)
    )

    if armed_buy:
        buy_live_touch = ask is not None and float(ask) <= high
        if buy_live_touch and not live_breakout_bar:
            return TimingDecision(
                "BUY", "BUY RETEST | live price touched Timing High"
            )

        buy_candle_touch = (
            not breakout_bar
            and candle_low is not None
            and float(candle_low) <= high
        )
        if buy_candle_touch:
            return TimingDecision(
                "BUY",
                "BUY RETEST | future completed candle wick touched Timing High",
            )

    if armed_sell:
        sell_live_touch = bid is not None and float(bid) >= low
        if sell_live_touch and not live_breakout_bar:
            return TimingDecision(
                "SELL", "SELL RETEST | live price touched Timing Low"
            )

        sell_candle_touch = (
            not breakout_bar
            and candle_high is not None
            and float(candle_high) >= low
        )
        if sell_candle_touch:
            return TimingDecision(
                "SELL",
                "SELL RETEST | future completed candle wick touched Timing Low",
            )

    return None


class TimingCandleStrategy:
    """Compatibility facade exposing the pure Timing Candle rules."""

    breakout_arm = staticmethod(breakout_arm)
    retest_signal = staticmethod(retest_signal)