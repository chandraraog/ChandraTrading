def test_timing_backtest_source_contains_daily_diagnostics():
    from pathlib import Path
    p = Path(__file__).parents[1] / "backend" / "app" / "bot_manager.py"
    s = p.read_text(encoding="utf-8")
    assert '"timing_found"' in s
    assert '"breakout_time"' in s
    assert '"retest_time"' in s
    assert '"diagnostic":day_diag.get(d,{})' in s
