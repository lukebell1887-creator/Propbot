#!/usr/bin/env python3
"""
smoke_v23_live.py — offline import + construction smoke test.

Runs with NO MT5 bridge. Confirms:
  1. All imports resolve (no circular imports, no missing modules).
  2. V23LiveConfig dataclass is well-formed.
  3. Per-symbol state builds for all 4 symbols.
  4. News CSV loads (count printed).
  5. TradingCalendar instantiates.
  6. Sizer produces a sane risk% in warm-up.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from src.live.v23_live import V23Live, V23LiveConfig, V23_SPECS


class _StubBridge:
    """A stub MT5Bridge that returns zero / empty for everything.
    We won't call .run() — just construct the runner and poke a couple of
    helpers, so bridge methods are never actually invoked."""
    def get_account_info(self):
        class AI: equity = 100_000.0
        return AI()
    def get_positions(self, *a, **kw): return []
    def get_quote(self, *a, **kw):  return None
    def get_history(self, *a, **kw): return []
    def connect(self): return True


def main() -> int:
    print("=" * 80)
    print("  v23 LIVE SMOKE TEST  (offline)")
    print("=" * 80)

    cfg = V23LiveConfig()
    print(f"  Config OK:  symbols={cfg.symbols}")
    print(f"              risk={cfg.base_risk_pct*100:.3f}% cap={cfg.cap_mult:.1f}x")
    print(f"              news_csv={cfg.news_csv}")
    print(f"              account_kill={cfg.account_kill_dd*100:.1f}%  "
          f"daily_breaker={cfg.daily_breaker_dd*100:.1f}%")

    runner = V23Live(bridge=_StubBridge(), cfg=cfg, dry_run=True)

    print(f"\n  Symbols wired  : {list(runner.states.keys())}")
    print(f"  News events    : {len(runner.news_events)}")
    for sym, st in runner.states.items():
        o = st.orb_cfg
        spec = st.spec
        print(f"    {sym:<6}  broker={spec.broker:<12}  "
              f"OR window = {o.or_start_hour:02d}:{o.or_start_minute:02d} UTC "
              f"+ {o.or_minutes}m,  trade_window={o.trade_window_minutes}m,  "
              f"tp1={o.tp1_range_mult}x  tp2={o.tp2_range_mult}x")

    # Sizer smoke — Merton-GZ (v24d sweet-spot: base=0.110%, cap=5x → 0.55% max)
    eq = 100_000.0
    peak = 100_000.0
    # Warmup: sizer returns base while <15 trades seen, so seed it a bit.
    # At DD=0 with edge μ>0, risk should equal cap = 0.55%.
    # At DD≈4%, risk should → 0 (Grossman-Zhou barrier absorbs).
    rp_warm = runner.merton_sizer.compute_risk_pct("DE40", eq, peak, [])
    print(f"\n  Sizer smoke  (warmup, DD=0 %)      :  risk = {rp_warm*100:.4f}%   "
          f"($ per trade @ $100k = ${eq*rp_warm:.2f})")
    assert abs(rp_warm - cfg.base_risk_pct) < 1e-12, "warmup should return base = 0.110%"

    # Feed 20 synthetic trades with avg +0.5R, σ~1.5R → μ/σ²≈0.22 → merton_mult binds cap
    import random
    random.seed(42)
    for _ in range(20):
        runner.merton_sizer.on_trade_closed("DE40", random.gauss(0.5, 1.5))
    rp_edge = runner.merton_sizer.compute_risk_pct("DE40", eq, peak, [])
    print(f"  Sizer smoke  (20 trades, DD=0 %)   :  risk = {rp_edge*100:.4f}%   "
          f"(cap binds at {cfg.base_risk_pct*cfg.cap_mult*100:.3f}%)")
    assert rp_edge <= cfg.base_risk_pct * cfg.cap_mult + 1e-9, "must not exceed hard cap"

    # Simulate DD = 2% → GZ barrier halves the size
    rp_dd2 = runner.merton_sizer.compute_risk_pct("DE40", eq * 0.98, eq, [])
    print(f"  Sizer smoke  (20 trades, DD=2 %)   :  risk = {rp_dd2*100:.4f}%   "
          f"(GZ barrier = {1 - 0.02/cfg.dd_cap_pct:.3f})")

    # Simulate DD ≈ 4% → GZ absorbs → risk = 0 (sizer itself stops trading)
    rp_dd4 = runner.merton_sizer.compute_risk_pct("DE40", eq * 0.96, eq, [])
    print(f"  Sizer smoke  (20 trades, DD=4 %)   :  risk = {rp_dd4*100:.4f}%   "
          f"(GZ barrier absorbs → size → 0 at DD_cap=4%)")
    assert rp_dd4 < rp_edge, "GZ barrier must shrink size as DD approaches cap"

    # Reset so warmup test works on subsequent runs
    runner.merton_sizer.reset()

    # Warmup smoke — verify the method exists and is callable (can't run
    # end-to-end here as we have no bridge; full warmup runs on VPS).
    assert hasattr(runner, "_warmup_all") and callable(runner._warmup_all), \
        "runner missing warmup; OR tracker will be empty on day-1"
    assert hasattr(runner, "_warmup_symbol") and callable(runner._warmup_symbol), \
        "runner missing per-symbol warmup"
    print(f"  Warmup wired :  _warmup_all() present (pulls 2880 M1 bars/symbol on start)")


    # Rail smoke
    from datetime import datetime, timezone, timedelta
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

    print("\n  ✅ SMOKE OK — imports, state, sizer, rails all green.")
    print("     Next step:  run DRY-RUN against a live SHF_Bridge EA.")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
