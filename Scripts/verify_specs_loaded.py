#!/usr/bin/env python3
"""
verify_specs_loaded.py
======================
Imports v30_live the way the running bot does and dumps the actual
`pip_value_per_lot` and `tick_size` for each symbol.  This tells us if
the hotfix-2 table is being honoured at runtime or if Python is loading
stale bytecode.

Expected post-hotfix-2:
    DE40    tick=1.0    pip_value_per_lot=1.0
    US30    tick=1.0    pip_value_per_lot=1.0
    US500   tick=0.25   pip_value_per_lot=0.25   <-- key check
    XAUUSD  tick=0.01   pip_value_per_lot=1.0

If US500's pip_value_per_lot prints as 1.0 instead of 0.25, the bot is
running stale code.  Fix:
    Stop-Process -Name python   (or however you stop the bot)
    Remove-Item -Recurse -Force src\live\__pycache__
    Remove-Item -Recurse -Force src\__pycache__
    .\GO_LIVE_V30.ps1
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

print("=" * 72)
print(" v30_live.py SPECS  --  what the running bot actually uses")
print("=" * 72)

# Force-reimport with no cache
import importlib
import src.live.v30_live as v30
importlib.reload(v30)

print(f"\n  V30_DOLLARS_PER_TICK_PER_LOT  (raw table):")
for sym, val in v30.V30_DOLLARS_PER_TICK_PER_LOT.items():
    print(f"    {sym:8s} = ${val:.4f}/tick/lot")

print(f"\n  V30_BROKER_TICK_SIZE  (raw table):")
for sym, val in v30.V30_BROKER_TICK_SIZE.items():
    print(f"    {sym:8s} = {val:.4f} price units / tick")

# Build the specs dict the same way the bot does
print(f"\n  SymbolSpec.pip_value_per_lot  (what the sizer reads):")
try:
    specs = v30._build_specs()  # adjust if function name differs
    for sym, spec in specs.items():
        ts = spec.tick_size
        pv = spec.pip_value_per_lot
        per_pt = pv / ts if ts > 0 else 0.0
        flag = "  <-- KEY" if sym == "US500" else ""
        print(f"    {sym:8s} tick_size={ts:.4f}  pip_value_per_lot=${pv:.4f}  "
              f"=> ${per_pt:.4f}/pt{flag}")
except AttributeError:
    print("    (couldn't auto-find _build_specs(); inspecting raw table only)")

print()
print("Expected for hotfix-2:  US500 pip_value_per_lot = $0.25 (= $1/pt)")
print("If it prints $1.00, the bot is running stale code — clear __pycache__")
print("and restart.  See header comment for exact commands.")
