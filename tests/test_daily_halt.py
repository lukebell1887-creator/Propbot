"""Unit tests for src/daily_halt.py — ensure hard kill-switch behaves correctly."""
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.daily_halt import DailyHalt


def ts(y, m, d, h=12, minute=0):
    """UTC unix timestamp."""
    return datetime(y, m, d, h, minute, tzinfo=timezone.utc).timestamp()


def test_fresh_day_allows_trade():
    h = DailyHalt(halt_pct=0.04)
    assert h.can_trade(ts(2026, 4, 1), 100_000) is True
    assert h.days_seen == 1


def test_under_limit_allows():
    h = DailyHalt(halt_pct=0.04)
    h.can_trade(ts(2026, 4, 1, 9), 100_000)
    # drop 3 % — still allowed
    assert h.can_trade(ts(2026, 4, 1, 10), 97_000) is True


def test_at_limit_halts_exactly_on_4pct():
    h = DailyHalt(halt_pct=0.04)
    h.can_trade(ts(2026, 4, 1, 9), 100_000)
    # drop to exactly -4 %
    assert h.can_trade(ts(2026, 4, 1, 10), 96_000) is False
    assert h.halted_today is True
    assert h.total_halts == 1


def test_halt_blocks_rest_of_day_even_if_recovers():
    h = DailyHalt(halt_pct=0.04)
    h.can_trade(ts(2026, 4, 1, 9), 100_000)
    # breach
    assert h.can_trade(ts(2026, 4, 1, 10), 95_000) is False
    # later, equity miraculously recovers — STILL halted
    assert h.can_trade(ts(2026, 4, 1, 15), 99_000) is False
    assert h.can_trade(ts(2026, 4, 1, 20), 101_000) is False


def test_new_day_resets_halt():
    h = DailyHalt(halt_pct=0.04)
    h.can_trade(ts(2026, 4, 1, 9), 100_000)
    h.can_trade(ts(2026, 4, 1, 10), 95_000)  # halt
    # next day — should reopen
    assert h.can_trade(ts(2026, 4, 2, 9), 95_000) is True
    assert h.halted_today is False
    # day_start_equity anchors to yesterday's end
    assert h.day_start_equity == 95_000
    assert h.days_seen == 2


def test_day_rollover_detection_across_midnight():
    h = DailyHalt(halt_pct=0.04)
    h.can_trade(ts(2026, 4, 1, 23, 59), 100_000)
    # +1 min → next day
    h.can_trade(ts(2026, 4, 2, 0, 1), 100_000)
    assert h.days_seen == 2


def test_server_tz_offset_shifts_day_boundary():
    # broker-server +3 h (EEST). 22:00 UTC is 01:00 next-day server-time.
    h = DailyHalt(halt_pct=0.04, server_tz_offset_h=3.0)
    h.can_trade(ts(2026, 4, 1, 22), 100_000)
    assert h.current_day.day == 2   # already next day server-side


def test_multiple_halts_accumulate():
    h = DailyHalt(halt_pct=0.04)
    # day 1 — halt
    h.can_trade(ts(2026, 4, 1, 9), 100_000)
    h.can_trade(ts(2026, 4, 1, 10), 95_000)
    # day 2 — halt again
    h.can_trade(ts(2026, 4, 2, 9), 95_000)
    h.can_trade(ts(2026, 4, 2, 10), 91_000)
    # day 3 — no halt
    h.can_trade(ts(2026, 4, 3, 9), 91_000)
    h.can_trade(ts(2026, 4, 3, 10), 90_000)
    assert h.total_halts == 2
    assert h.days_seen == 3
    assert len(h.halted_dates) == 2


def test_reset_clears_state():
    h = DailyHalt(halt_pct=0.04)
    h.can_trade(ts(2026, 4, 1, 9), 100_000)
    h.can_trade(ts(2026, 4, 1, 10), 95_000)
    h.reset()
    assert h.current_day is None
    assert h.halted_today is False
    assert h.total_halts == 0
    assert h.days_seen == 0


if __name__ == "__main__":
    # run all tests
    import inspect, sys
    tests = [o for n, o in inspect.getmembers(sys.modules[__name__])
             if n.startswith("test_") and callable(o)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n  {len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
