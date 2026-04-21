"""
v15 live smoke test — NO bridge required
=========================================

Verifies:
  1. `load_v15_params` reads the tuning JSON and builds SymbolParams for all
     5 Tier-1 symbols.
  2. Engine constructs cleanly with those params.
  3. We can feed it a couple of fake bars without crashing.

Run this BEFORE touching the VPS — proves the Python side is green.
"""
from __future__ import annotations

import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.live.v15_live import load_v15_params
from src.smartbb_engine import SMARTBB_UNIVERSE
from src.smartbb_engine_v14 import SmartBBV14Engine, SmartBBV14Config

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("smoke.v15")


def main():
    tuning = Path("Results/v15_ultimate_tuning.json")
    if not tuning.exists():
        log.error(f"Missing {tuning} — optimizer hasn't been run yet.")
        return 2

    params = load_v15_params(tuning, tier="TIER1",
                             symbols=["US30", "US100", "US500", "DE40", "XAUUSD"])
    if not params:
        log.error("No TIER1 params found")
        return 3

    log.info("Loaded per-symbol params:")
    for sym, p in params.items():
        log.info(
            f"  {sym:8s}  z_q={p.z_quantile:.2f} "
            f"[{p.z_min_abs:.1f},{p.z_max_abs:.1f}]  "
            f"stop={p.stop_atr_mult:.2f}  tp={p.tp_frac:.2f}  "
            f"hurst_q={p.hurst_quantile:.2f}<{p.hurst_max_abs:.2f}  "
            f"ou_hl<{p.ou_max_halflife:.0f}  risk_x={p.risk_multiplier:.2f}"
        )

    specs = [SMARTBB_UNIVERSE[s] for s in params.keys()]
    cfg = SmartBBV14Config()
    eng = SmartBBV14Engine(symbols=specs, params=params, cfg=cfg,
                            initial_equity=100_000.0)
    log.info(f"Engine built OK with {len(eng.states)} symbols  "
             f"equity=${eng.equity:,.2f}")

    # Feed 30 synthetic bars per symbol just to verify on_bar doesn't raise
    import random
    random.seed(42)
    for sym, spec in zip(params.keys(), specs):
        px = 100.0
        for i in range(120):
            ret = random.uniform(-0.001, 0.001)
            px = max(px * (1 + ret), 1.0)
            t = 1_700_000_000 + i * 60
            eng.on_bar(
                symbol=sym, time=t,
                day_key="2026-04-21",
                hour=9, minute=i % 60,
                open_=px, high=px * 1.0005, low=px * 0.9995, close=px,
            )
    log.info("✅  Fed 120 synthetic bars per symbol, engine stable.")
    s = eng.summary()
    log.info(f"Summary: trades={s['trades']} equity=${s['equity']:,.2f}")
    log.info("🟢  SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
