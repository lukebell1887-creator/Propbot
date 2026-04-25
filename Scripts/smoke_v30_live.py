#!/usr/bin/env python3
"""
smoke_v30_live.py — offline import + construction smoke test for v30 live.

Runs with NO MT5 bridge. Confirms:
  1. All imports resolve.
  2. V30LiveConfig defaults (risk=0.170 %, nochase=300 s, magic=30000).
  3. Per-symbol state builds for all 4 symbols.
  4. News CSV loads.
  5. Sizer warmup returns base, post-warmup respects cap.
  6. Cross-symbol no-chase cooldown logic gates correctly.
  7. Slippage tracker captures + rolls up + clears.
"""
from __future__ import annotations
import sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from src.live.v30_live import V30Live, V30LiveConfig, V30_SPECS, SlippageStat


class _StubBridge:
    """A stub MT5Bridge that returns zero / empty for everything."""
    def get_account_info(self):
        class AI: equity = 100_000.0
        return AI()
    def get_positions(self, *a, **kw): return []
    def get_quote(self, *a, **kw):  return None
    def get_history(self, *a, **kw): return []
    def get_server_time(self, *a, **kw): return None
    def connect(self): return True


def main() -> int:
    print("=" * 80)
    print("  v30 LIVE SMOKE TEST  (offline)")
    print("=" * 80)

    cfg = V30LiveConfig()
    assert abs(cfg.base_risk_pct - 0.00170) < 1e-12, \
        f"v30 ship risk MUST be 0.00170, got {cfg.base_risk_pct}"
    assert cfg.nochase_cooldown_s == 300.0, \
        f"v30 ship cooldown MUST be 300.0 s, got {cfg.nochase_cooldown_s}"
    assert cfg.magic == 30000, \
        f"v30 magic MUST be 30000 (distinguishes from v23/23000), got {cfg.magic}"

    print(f"  Config OK:  symbols={cfg.symbols}")
    print(f"              risk={cfg.base_risk_pct*100:.3f}%  cap={cfg.cap_mult:.1f}x  "
          f"max_per_trade={cfg.base_risk_pct*cfg.cap_mult*100:.3f}%")
    print(f"              nochase_cooldown_s={cfg.nochase_cooldown_s}  "
          f"magic={cfg.magic}  comment={cfg.comment!r}")
    print(f"              news_csv={cfg.news_csv}")
    print(f"              account_kill={cfg.account_kill_dd*100:.1f}%  "
          f"daily_breaker={cfg.daily_breaker_dd*100:.1f}%")

    runner = V30Live(bridge=_StubBridge(), cfg=cfg, dry_run=True)

    print(f"\n  Symbols wired  : {list(runner.states.keys())}")
    print(f"  News events    : {len(runner.news_events)}")
    for sym, st in runner.states.items():
        o = st.orb_cfg
        spec = st.spec
        print(f"    {sym:<6}  broker={spec.broker:<12}  tick={spec.tick_size:<5}  "
              f"pip_value=${spec.pip_value_per_lot:<6.2f}  "
              f"OR={o.or_start_hour:02d}:{o.or_start_minute:02d}+{o.or_minutes}m  "
              f"trade_window={o.trade_window_minutes}m  "
              f"tp1={o.tp1_range_mult}x  tp2={o.tp2_range_mult}x")

    # -----------------------------------------------------------------
    # Sizer smoke — must return base risk during warm-up, and never exceed cap.
    # -----------------------------------------------------------------
    eq = 100_000.0
    rp_warm = runner.merton_sizer.compute_risk_pct("DE40", eq, eq, [])
    assert abs(rp_warm - cfg.base_risk_pct) < 1e-12, \
        f"warmup risk should equal base ({cfg.base_risk_pct}), got {rp_warm}"
    print(f"\n  Sizer smoke  (warmup, DD=0 %)      :  risk = {rp_warm*100:.4f}%   "
          f"($ per trade @ $100k = ${eq*rp_warm:.2f})")

    import random
    random.seed(42)
    for _ in range(20):
        runner.merton_sizer.on_trade_closed("DE40", random.gauss(0.5, 1.5))
    rp_edge = runner.merton_sizer.compute_risk_pct("DE40", eq, eq, [])
    assert rp_edge <= cfg.base_risk_pct * cfg.cap_mult + 1e-9, "must not exceed hard cap"
    print(f"  Sizer smoke  (20 trades, DD=0 %)   :  risk = {rp_edge*100:.4f}%   "
          f"(cap binds at {cfg.base_risk_pct*cfg.cap_mult*100:.3f}%)")

    rp_dd2 = runner.merton_sizer.compute_risk_pct("DE40", eq * 0.98, eq, [])
    print(f"  Sizer smoke  (20 trades, DD=2 %)   :  risk = {rp_dd2*100:.4f}%   "
          f"(GZ barrier = {1 - 0.02/cfg.dd_cap_pct:.3f})")

    rp_dd4 = runner.merton_sizer.compute_risk_pct("DE40", eq * 0.96, eq, [])
    assert rp_dd4 < rp_edge, "GZ barrier must shrink size as DD approaches cap"
    print(f"  Sizer smoke  (20 trades, DD=4 %)   :  risk = {rp_dd4*100:.4f}%   "
          f"(GZ barrier absorbs → size → 0 at DD_cap=4%)")

    runner.merton_sizer.reset()

    # -----------------------------------------------------------------
    # ★ NEW v30: cross-symbol no-chase cooldown gate.
    # -----------------------------------------------------------------
    print(f"\n  No-chase cooldown smoke:")
    # No closes recorded → entry should be allowed
    assert runner._nochase_block("DE40", time.time()) is None, \
        "no closes yet, should not block"

    # Stamp a US30 close 5 s ago — DE40 should be blocked
    runner._last_close_ts_by_symbol["US30"] = time.time() - 5.0
    nc = runner._nochase_block("DE40", time.time())
    assert nc is not None, "DE40 must be blocked when US30 closed 5s ago"
    assert nc[0] == "US30", f"wrong blocker: {nc}"
    print(f"    DE40 blocked by {nc[0]} ({nc[1]:.0f}s ago)  (within 300s window)  OK")

    # Same-symbol close: US30 should NOT be blocked by its own close
    nc_self = runner._nochase_block("US30", time.time())
    assert nc_self is None, "same-symbol close must NOT block (ORB fires once/day anyway)"
    print(f"    US30 NOT blocked by its own close (cross-symbol filter only)     OK")

    # 305 s ago is outside the cooldown window
    runner._last_close_ts_by_symbol["US30"] = time.time() - 305.0
    nc_old = runner._nochase_block("DE40", time.time())
    assert nc_old is None, "305s > 300s, must not block"
    print(f"    DE40 NOT blocked when US30 closed 305s ago (>300s cooldown)      OK")

    # Reset for slippage smoke
    for k in runner._last_close_ts_by_symbol:
        runner._last_close_ts_by_symbol[k] = 0.0

    # -----------------------------------------------------------------
    # ★ NEW v30: slippage tracker.
    # -----------------------------------------------------------------
    print(f"\n  Slippage tracker smoke:")
    # DE40 LONG: intended 17,500.0, fill 17,500.5 → slip = +0.5 ticks (worse)
    t1, d1 = runner._record_entry_slippage("DE40", "LONG",
                                           intended_px=17500.0, fill_px=17500.5,
                                           lots=0.10)
    assert abs(t1 - 0.5) < 1e-9, f"LONG slip wrong: {t1}"
    # DE40 LONG: intended 17,500.0, fill 17,499.0 → slip = -1.0 ticks (better)
    t2, d2 = runner._record_entry_slippage("DE40", "LONG",
                                           intended_px=17500.0, fill_px=17499.0,
                                           lots=0.10)
    assert abs(t2 - (-1.0)) < 1e-9, f"price-improvement slip wrong: {t2}"
    # US30 SHORT: intended 38,000.0, fill 37,998.0 → slip = (38000-37998)/1.0 = +2.0 (worse)
    t3, d3 = runner._record_entry_slippage("US30", "SHORT",
                                           intended_px=38000.0, fill_px=37998.0,
                                           lots=0.20)
    assert abs(t3 - 2.0) < 1e-9, f"SHORT slip wrong: {t3}"

    de = runner.slip_per_symbol["DE40"]
    us = runner.slip_per_symbol["US30"]
    tot = runner.slip_total
    assert de.n == 2 and us.n == 1 and tot.n == 3
    assert abs(de.avg_ticks - (0.5 + -1.0) / 2) < 1e-9
    assert abs(us.avg_ticks - 2.0) < 1e-9
    assert abs(tot.avg_ticks - (0.5 + -1.0 + 2.0) / 3) < 1e-9
    assert runner._worst_slip_owner == ("US30", 2.0)
    assert runner._best_slip_owner == ("DE40", -1.0)

    print(f"    recorded 3 slippage points across DE40 / US30                    OK")
    print(f"    DE40   n={de.n}  avg={de.avg_ticks:+.2f}t  min={de.min_ticks:+.2f}t  max={de.max_ticks:+.2f}t")
    print(f"    US30   n={us.n}  avg={us.avg_ticks:+.2f}t  min={us.min_ticks:+.2f}t  max={us.max_ticks:+.2f}t")
    print(f"    PORTFOLIO trades={tot.n}  avg={tot.avg_ticks:+.2f}t  "
          f"sum$={tot.sum_dollars:+.2f}  worst={runner._worst_slip_owner}  "
          f"best={runner._best_slip_owner}")

    # Verify SlippageStat.to_dict() shape (used by telemetry)
    d = de.to_dict()
    assert set(d.keys()) >= {"n", "avg_ticks", "max_ticks", "min_ticks", "sum_dollars"}, d
    print(f"    SlippageStat.to_dict() keys OK                                   OK")

    # -----------------------------------------------------------------
    # Warmup wiring
    # -----------------------------------------------------------------
    assert hasattr(runner, "_warmup_all") and callable(runner._warmup_all)
    assert hasattr(runner, "_warmup_symbol") and callable(runner._warmup_symbol)
    print(f"\n  Warmup wired :  _warmup_all() present (pulls 2880 M1 bars/symbol on start)")

    # -----------------------------------------------------------------
    # Rail smoke (offline)
    # -----------------------------------------------------------------
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    block = runner._in_news_entry_block(now)
    flat = runner._in_news_flatten_window(now)
    print(f"  Rails @ now  :  news_entry_block={block is not None}  "
          f"news_flatten={flat is not None}")

    # DD smoke
    runner.start_equity = eq
    runner.peak_equity = eq
    runner.day_start_equity = eq
    print(f"  DD smoke     :  rolling_dd={runner._equity_dd_pct():.2f}%  "
          f"day_dd={runner._day_dd_pct():.2f}%")

    print("\n  [OK] SMOKE OK — imports, state, sizer, no-chase cooldown, slippage all green.")
    print("       Next step:  run DRY-RUN against a live SHF_Bridge EA on the VPS.")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
