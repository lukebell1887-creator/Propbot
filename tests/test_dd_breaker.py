"""Tests for src.dd_breaker (total-DD hard circuit breaker)."""
import sys
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dd_breaker import DDBreaker, apply_dd_breaker


# ---------------------------------------------------------------------------
#  DDBreaker streaming state machine
# ---------------------------------------------------------------------------
def _ts(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute).timestamp()


def test_not_halted_at_start():
    br = DDBreaker(halt_pct=0.04)
    halted, dd = br.check(_ts(2026, 1, 2), 100_000)
    assert not halted
    assert dd == 0.0


def test_updates_peak_on_gains():
    br = DDBreaker(halt_pct=0.04)
    br.check(_ts(2026, 1, 2), 100_000)
    br.check(_ts(2026, 1, 2, 13), 103_000)
    br.check(_ts(2026, 1, 2, 14), 101_500)
    assert br.peak_equity == 103_000
    # current dd = (103000 - 101500) / 103000 ≈ 1.46 %
    assert abs(br.max_dd_pct_seen - 1500 / 103_000) < 1e-9


def test_trips_exactly_at_threshold():
    br = DDBreaker(halt_pct=0.04)
    br.check(_ts(2026, 1, 2, 12), 100_000)
    br.check(_ts(2026, 1, 2, 13), 110_000)   # peak = 110k
    # 4 % of 110k = 4400 → trip at 105,600
    halted, dd = br.check(_ts(2026, 1, 2, 14), 105_600)
    assert halted
    assert abs(dd - 0.04) < 1e-6
    assert br.total_halts == 1


def test_does_not_trip_just_above_threshold():
    """105,700 is -3.91 % off peak — just inside."""
    br = DDBreaker(halt_pct=0.04)
    br.check(_ts(2026, 1, 2, 12), 100_000)
    br.check(_ts(2026, 1, 2, 13), 110_000)
    halted, _ = br.check(_ts(2026, 1, 2, 14), 105_700)
    assert not halted


def test_halt_persists_for_rest_of_day():
    br = DDBreaker(halt_pct=0.04)
    br.check(_ts(2026, 1, 2, 12), 110_000)
    br.check(_ts(2026, 1, 2, 13), 105_600)    # trips
    # even if equity recovers to 108k later THAT DAY, still halted
    halted, _ = br.check(_ts(2026, 1, 2, 15), 108_000)
    assert halted
    halted, _ = br.check(_ts(2026, 1, 2, 23), 109_500)
    assert halted


def test_halt_resets_next_day():
    br = DDBreaker(halt_pct=0.04)
    br.check(_ts(2026, 1, 2, 12), 110_000)
    br.check(_ts(2026, 1, 2, 14), 105_600)    # trips
    # next day, small DD → not halted
    halted, _ = br.check(_ts(2026, 1, 3, 9), 108_000)
    # Note: peak is STILL 110k, current dd = 2/110 ≈ 1.8 %
    assert not halted
    # but if on day 3 we breach 4 % from the 110k peak, halt again
    halted, _ = br.check(_ts(2026, 1, 3, 14), 105_000)
    assert halted
    assert br.total_halts == 2


def test_peak_preserved_across_days():
    br = DDBreaker(halt_pct=0.04)
    br.check(_ts(2026, 1, 2, 12), 120_000)    # peak set
    br.check(_ts(2026, 1, 3, 12), 100_000)    # large drawdown day 2
    assert br.peak_equity == 120_000


def test_reset_clears_state():
    br = DDBreaker(halt_pct=0.04)
    br.check(_ts(2026, 1, 2, 12), 110_000)
    br.check(_ts(2026, 1, 2, 14), 105_600)
    assert br.total_halts == 1
    br.reset()
    assert br.peak_equity == 0.0
    assert not br.halted
    assert br.total_halts == 0


# ---------------------------------------------------------------------------
#  apply_dd_breaker backtest filter
# ---------------------------------------------------------------------------
@dataclass
class _T:
    open_time_unix: float
    close_time_unix: float
    net_pnl: float


def test_apply_breaker_no_dd_keeps_all():
    # Five small-winner trades, no DD → all kept
    trades = [_T(_ts(2026, 1, 2, 9 + i),
                 _ts(2026, 1, 2, 10 + i), +200) for i in range(5)]
    kept, br = apply_dd_breaker(trades, starting_balance=100_000, halt_pct=0.04)
    assert len(kept) == 5
    assert br.total_halts == 0


def test_apply_breaker_drops_trades_after_trigger():
    """Big first loser triggers breaker; subsequent same-day trades dropped."""
    # trade 1: loses $4500 on 100k → -4.5 % DD ≥ 4 % threshold
    t1 = _T(_ts(2026, 1, 2, 9), _ts(2026, 1, 2, 10), -4_500)
    t2 = _T(_ts(2026, 1, 2, 11), _ts(2026, 1, 2, 12), +1_000)   # should drop
    t3 = _T(_ts(2026, 1, 2, 13), _ts(2026, 1, 2, 14), -500)     # should drop
    kept, br = apply_dd_breaker([t1, t2, t3], 100_000, halt_pct=0.04)
    assert kept == [t1]
    assert br.total_halts == 1


def test_apply_breaker_resumes_next_day_only_if_dd_recovered():
    """Next day: if DD is STILL ≥ 4 %, breaker stays tripped (strict 4 % enforce).
    Breaker only allows new trades once DD drops back below 4 %.  A scenario
    where the next day's trade would bring DD below 4 % IS allowed, but the
    first sample of the day still shows DD above — conservatively reject.

    This is STRICTER than 5ers (which allows trading up to 8 % total DD), and
    that is exactly the user's stated goal: 'never fail the challenge'."""
    t1 = _T(_ts(2026, 1, 2, 9), _ts(2026, 1, 2, 10), -4_500)   # trip, dd=4.5%
    t2 = _T(_ts(2026, 1, 2, 11), _ts(2026, 1, 2, 12), +500)    # same day → drop
    t3 = _T(_ts(2026, 1, 3, 9), _ts(2026, 1, 3, 10), +200)     # still in DD → drop
    kept, br = apply_dd_breaker([t1, t2, t3], 100_000, halt_pct=0.04)
    assert t1 in kept
    assert t2 not in kept
    assert t3 not in kept     # strict: can't trade until dd < 4 %


def test_apply_breaker_resumes_when_dd_recovers():
    """If between-trade equity rebuilds so DD < 4 %, trading is re-enabled."""
    # t1 loses 4.5% → trip
    # t2 gains 1000 BUT opens same day → still halted
    # t3 gains 200 on day 3 — by then equity = 96,500 + 0 = 96,500 (t2 dropped),
    #       peak still 100,000, dd = 3.5% < 4 % → allowed
    t1 = _T(_ts(2026, 1, 2, 9), _ts(2026, 1, 2, 10), -4_500)
    # FAKE trade that "recovers" some loss but is dropped inside halt day;
    # we need an in-flight trade that OPENED before trigger to generate PnL
    t_recover = _T(_ts(2026, 1, 2, 8), _ts(2026, 1, 2, 13), +1_000)
    t3 = _T(_ts(2026, 1, 3, 9), _ts(2026, 1, 3, 10), +200)
    kept, br = apply_dd_breaker([t1, t_recover, t3], 100_000, halt_pct=0.04)
    # after t1 close (eq=95.5k) and t_recover close (eq=96.5k), dd = 3.5% on day 3
    assert t1 in kept
    assert t_recover in kept           # opened BEFORE trigger, so kept
    assert t3 in kept                  # day 3, dd now 3.5% < 4 % → allowed


def test_apply_breaker_accepts_overlapping_running_trades():
    """Trades opened BEFORE trigger still close normally (conservative sim)."""
    # t1 opens 09:00 closes 14:00 PnL -6000
    # t2 opens 10:00 closes 11:00 PnL -2000 → TRIPS breaker (total -8000 = 8%)
    # wait — breaker fires when total equity hits -4%. Let's recalc.
    # open order: t1 at 9, t2 at 10
    # close order: t2 at 11 (-2000 → equity 98000, dd 2%), then t1 at 14
    # so after t2 closes we're at 2% dd — NO trip yet
    # t1 at 14 is still in flight (opened 9am)
    # then t1 closes with -6000 → equity 92000 → dd 8% — now tripped but
    # it was already running, so it stays in the kept list
    t1 = _T(_ts(2026, 1, 2, 9), _ts(2026, 1, 2, 14), -6_000)
    t2 = _T(_ts(2026, 1, 2, 10), _ts(2026, 1, 2, 11), -2_000)
    kept, br = apply_dd_breaker([t1, t2], 100_000, halt_pct=0.04)
    # t1 opened before trigger so stays; t2 opened before trigger so stays
    assert t1 in kept
    assert t2 in kept
    # breaker did trip (on t1's close event)
    assert br.total_halts == 1


def test_apply_breaker_empty_input():
    kept, br = apply_dd_breaker([], 100_000)
    assert kept == []
    assert br.total_halts == 0


if __name__ == "__main__":
    # minimal inline runner
    import traceback
    tests = [v for k, v in list(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0; failed = 0
    for fn in tests:
        try:
            fn(); passed += 1
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}:  {e}")
            traceback.print_exc()
    print(f"\n{passed} / {passed+failed} tests passed")
    sys.exit(0 if failed == 0 else 1)
