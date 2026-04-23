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

    # Sizer smoke — warmup regime
    eq = 100_000.0
    peak = 100_000.0
    rp = runner.sizer.compute_risk_pct("DE40", eq, peak, [])
    print(f"\n  Sizer smoke  :  warmup risk = {rp*100:.4f}%   "
          f"($ per trade @ $100k eq = ${eq*rp:.2f})")
    assert 0.0 < rp <= cfg.base_risk_pct * cfg.cap_mult + 1e-9, "sizer out of bounds"

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
