"""Unit tests for the 5%ers risk guard — the bit that must not fail."""
from datetime import datetime, timezone, timedelta
from src.fivers_risk_guard import FiversRiskGuard


def test_green_at_zero_dd():
    g = FiversRiskGuard(start_equity=100_000)
    s = g.multiplier(100_000, now_utc=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc))
    assert s.multiplier == 1.0
    assert s.phase == "green"
    assert not s.halted_today


def test_soft_brake_at_50pct_daily():
    # Lost $2,000 today (= 50 % of $4k daily cap) → multiplier hits the taper edge
    g = FiversRiskGuard(start_equity=100_000)
    t = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    s = g.multiplier(98_000, now_utc=t)
    assert 0.99 <= s.multiplier <= 1.0
    # $2,500 lost = 62.5 % of cap = exactly halfway through the 50–75 % taper → 0.5
    s2 = g.multiplier(97_500, now_utc=t)
    assert 0.45 <= s2.multiplier <= 0.55
    # $2,200 lost = 55 % of cap = 20 % through the taper → 0.8
    s3 = g.multiplier(97_800, now_utc=t)
    assert 0.75 <= s3.multiplier <= 0.85



def test_hard_stop_at_75pct_daily():
    g = FiversRiskGuard(start_equity=100_000)
    t = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    s = g.multiplier(97_000, now_utc=t)      # lost $3,000 today = 75 % of $4k
    assert s.multiplier == 0.0
    assert s.halted_today
    assert s.phase == "STOP"


def test_day_rollover_resets_halt():
    g = FiversRiskGuard(start_equity=100_000)
    t1 = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    s1 = g.multiplier(97_000, now_utc=t1)    # halted today
    assert s1.halted_today
    # Next calendar day
    t2 = datetime(2026, 1, 2, 8, 0, tzinfo=timezone.utc)
    s2 = g.multiplier(97_000, now_utc=t2)
    assert not s2.halted_today
    assert s2.multiplier >= 0.9     # green again (no fresh losses today)


def test_permanent_halt_at_70pct_total():
    g = FiversRiskGuard(start_equity=100_000)
    t = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    s = g.multiplier(93_000, now_utc=t)     # $7,000 down = 70 % of total cap
    assert s.halted_permanently
    assert s.multiplier == 0.0
    # Even next day, still halted
    t2 = datetime(2026, 1, 2, 8, 0, tzinfo=timezone.utc)
    s2 = g.multiplier(100_000, now_utc=t2)  # even if we 'recover' (shouldn't in real world)
    assert s2.halted_permanently


def test_total_soft_brake_at_50pct():
    """Slowly bleed equity over many days so daily brake never fires, but
    total DD accumulates and trips the total-DD taper."""
    g = FiversRiskGuard(start_equity=100_000)
    # Day 1: lose $1,500 (under daily soft brake @ $2k)
    g.multiplier(98_500, now_utc=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
    # Day 2: lose another $1,500 -> total $3,000 down, today only $1,500 (green today)
    g.multiplier(97_000, now_utc=datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc))
    # Day 3: lose another $1,500 -> total $4,500 down, today only $1,500 (still green today)
    s3 = g.multiplier(95_500, now_utc=datetime(2026, 1, 3, 10, 0, tzinfo=timezone.utc))
    assert s3.multiplier >= 0.9         # total 45 % < 50 % soft brake, daily 37.5 % green
    # Day 4: lose another $1,500 -> total $6,000 down = 60 % of $10k cap
    s4 = g.multiplier(94_000, now_utc=datetime(2026, 1, 4, 10, 0, tzinfo=timezone.utc))
    # total DD = 60 %, halfway through the 50–70 % total-DD taper → 0.5 multiplier
    assert 0.4 <= s4.multiplier <= 0.6
    assert s4.phase in ("yellow", "red")


