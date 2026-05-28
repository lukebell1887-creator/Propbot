"""
run_v30_backtest_live_period.py
================================

Runs the EXACT v30 backtest config the live bot is supposed to be running,
on whatever bars are currently in ``data/historical/``.

Live v30 ship config (from src/live/v30_live.py + V30_RELEASE_NOTES.md):
   symbols           = ["DE40", "US30", "US500", "XAUUSD"]
   base_risk_pct     = 0.00170          (0.170 %)
   cap_mult          = 5.0
   gamma             = 3.0
   ewma_alpha        = 0.20
   warmup_trades     = 15
   dd_cap_pct        = 0.04
   pool_symbols      = True
   no_edge_multiplier= 1.0
   slippage_ticks    = 1.0
   news block        = 15 min
   news flatten      = 2  min
   cross-sym no-chase= 300 s

Saves trades to ``Results/v30_live_period_trades.json`` with the same
schema as ``Results/v30_fresh_trades.json`` so
``parity_live_vs_backtest_window.py`` works directly against it.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from Scripts.preflight_checks import (
    BALANCE,
    run_portfolio,
    apply_full_safety_rails,
    MertonGZSizerConfig,
)
from Scripts.backtest_v22_lean_uk5 import stats
from Scripts.backtest_v23_locked import (
    load_news_events,
    apply_news_entry_block,
    apply_news_flatten,
    build_price_lookup,
)
from Scripts.backtest_v23_nochase import apply_no_chase

NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"
OUT_JSON = ROOT / "Results" / "v30_live_period_trades.json"

V30_SYMBOLS = ["DE40", "US30", "US500", "XAUUSD"]
V30_RISK    = 0.00170


def _ts_to_iso(t) -> str:
    """Convert a Trade.entry_time/exit_time field to an ISO string."""
    if hasattr(t, "isoformat"):
        return t.isoformat()
    try:
        return datetime.fromtimestamp(float(t), tz=timezone.utc).isoformat()
    except Exception:
        return str(t)


def main() -> int:
    print("=" * 90)
    print("  V30 BACKTEST -- running on whatever is in data/historical/")
    print("  symbols : ", V30_SYMBOLS)
    print(f"  risk    :  {V30_RISK*100:.3f} %  (matches live ship config)")
    print("=" * 90)

    cfg = MertonGZSizerConfig(
        base_risk_pct=V30_RISK,
        cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )

    # ---- engine + pipeline (same as backtest_v30_3sym_pairings.run_one) ----
    raw, tmin, tmax, _dropped, streams = run_portfolio(V30_SYMBOLS, cfg)
    print(f"  data window: {tmin}  ->  {tmax}")
    print(f"  raw engine trades: {len(raw)}")

    pl = build_price_lookup(streams)
    events = load_news_events(NEWS_CSV)
    raw, _ = apply_news_entry_block(raw, events, buffer_min=15)
    raw, _ = apply_news_flatten(raw, events, pl, minutes_before=2)
    print(f"  after news block + flatten: {len(raw)}")

    base = apply_full_safety_rails(raw, slippage_ticks=1.0)
    print(f"  after safety rails: {len(base)}")

    final, _drop = apply_no_chase(list(base), cooldown_s=300.0)
    print(f"  after no-chase 300 s: {len(final)}")

    s = stats(final)
    print("\n" + "-" * 90)
    print(f"  net PnL  : ${s['net']:>+12,.2f}")
    print(f"  ret %    :  {s['ret_pct']:>+7.2f} %")
    print(f"  DD %     :  {s['dd_pct']:>7.2f} %")
    print(f"  PF       :  {s['pf']:>7.2f}")
    print(f"  WR       :  {s['wr']:>7.2f} %")
    print(f"  n_part   :  {s['n']}")
    print("-" * 90)

    # ---- serialise to JSON in the SAME schema as v30_fresh_trades.json ----
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for t in final:
        out.append({
            "symbol":      t.symbol,
            "side":        int(t.side) if hasattr(t, "side") else 0,
            "entry_time":  _ts_to_iso(t.entry_time),
            "exit_time":   _ts_to_iso(t.exit_time),
            "entry_price": float(getattr(t, "entry_price", 0.0)),
            "exit_price":  float(getattr(t, "exit_price", 0.0)),
            "net_pnl":     float(getattr(t, "net_pnl", 0.0)),
            "realised_R":  float(getattr(t, "realised_R", 0.0)),
        })
    OUT_JSON.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"  wrote {len(out)} trades to {OUT_JSON.relative_to(ROOT)}")
    print("=" * 90)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
