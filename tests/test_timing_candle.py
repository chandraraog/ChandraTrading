from backend.app.strategies.timing_candle import breakout_arm, retest_signal


def test_buy_breakout_arms():
    d = breakout_arm(
        close=100.10,
        timing_high=100.00,
        timing_low=99.00,
        bar_key="2",
    )
    assert d.side == "BUY"


def test_sell_breakout_arms():
    d = breakout_arm(
        close=98.90,
        timing_high=100.00,
        timing_low=99.00,
        bar_key="2",
    )
    assert d.side == "SELL"


def test_breakout_candle_cannot_retest_itself():
    d = retest_signal(
        armed_sell=True,
        armed_buy=False,
        timing_high=100.0,
        timing_low=99.0,
        bar_key="2",
        arm_time="2",
        live_bar_key="2",
        bid=99.0,
        ask=99.1,
        candle_high=99.5,
        candle_low=98.5,
    )
    assert d is None


def test_future_sell_wick_touch_triggers():
    d = retest_signal(
        armed_sell=True,
        armed_buy=False,
        timing_high=100.0,
        timing_low=99.0,
        bar_key="3",
        arm_time="2",
        live_bar_key="3",
        bid=98.8,
        ask=98.9,
        candle_high=99.05,
        candle_low=98.7,
    )
    assert d.side == "SELL"


def test_future_buy_wick_touch_triggers():
    d = retest_signal(
        armed_sell=False,
        armed_buy=True,
        timing_high=100.0,
        timing_low=99.0,
        bar_key="3",
        arm_time="2",
        live_bar_key="3",
        bid=100.1,
        ask=100.2,
        candle_high=100.4,
        candle_low=99.95,
    )
    assert d.side == "BUY"


def test_raw_price_precision_is_not_rounded():
    high = 4493.06037
    low = 4490.87019
    d = retest_signal(
        armed_sell=True,
        armed_buy=False,
        timing_high=high,
        timing_low=low,
        bar_key="3",
        arm_time="2",
        live_bar_key="3",
        bid=4490.87018,
        ask=4490.87028,
        candle_high=4490.87018,
        candle_low=4490.70,
    )
    assert d is None


def test_live_buy_retest_triggers_while_breakout_is_still_last_completed_candle():
    """Critical regression: next candle is forming, so the last completed
    candle is still the breakout candle. A live touch must trigger immediately.
    """
    d = retest_signal(
        armed_buy=True,
        armed_sell=False,
        timing_high=100.00,
        timing_low=99.00,
        bar_key="breakout",
        arm_time="breakout",
        live_bar_key="future",
        bid=99.95,
        ask=99.99,
        candle_high=101.00,
        candle_low=100.10,
    )
    assert d is not None
    assert d.side == "BUY"


def test_live_sell_retest_triggers_while_breakout_is_still_last_completed_candle():
    """Critical regression for SELL: live touch must not wait for candle close."""
    d = retest_signal(
        armed_buy=False,
        armed_sell=True,
        timing_high=101.00,
        timing_low=100.00,
        bar_key="breakout",
        arm_time="breakout",
        live_bar_key="future",
        bid=100.01,
        ask=100.05,
        candle_high=100.90,
        candle_low=99.80,
    )
    assert d is not None
    assert d.side == "SELL"
