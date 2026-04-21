"""Pull the ACTUAL backtest numbers — not extrapolations — for each of the 5 symbols on 5%ers 3-month data.

We want:
  - Net PnL $
  - PF
  - Win rate %
  - Max drawdown %
  - Avg win $ / Avg loss $
  - Best / Worst single trade
  - Number of trades
  - Per-split breakdown
"""
import io, sys, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load v15 tuning result JSON
data = json.load(open(ROOT / "Results/v15_ultimate_tuning.json", encoding="utf-8"))

# Inspect structure once
print("Top-level keys:", list(data.keys()))
for k, v in data.items():
    if isinstance(v, dict):
        print(f"  [{k}] dict keys: {list(v.keys())[:10]}")
    elif isinstance(v, list):
        print(f"  [{k}] list len {len(v)}")

print("\n")

# Walk per-symbol
res = data.get("results", data)  # some JSONs wrap results; try both
if isinstance(res, dict):
    for sym, r in res.items():
        if not isinstance(r, dict):
            continue
        print(f"--- {sym} ---")
        print("  keys:", list(r.keys()))
        for k, v in r.items():
            if isinstance(v, (int, float, str, bool)):
                print(f"    {k}: {v}")
            elif isinstance(v, list):
                print(f"    {k}: list len {len(v)}")
                if v and isinstance(v[0], dict):
                    print(f"      sample[0] keys: {list(v[0].keys())}")
                    print(f"      sample[0]: {v[0]}")
            elif isinstance(v, dict):
                print(f"    {k}: dict keys {list(v.keys())[:8]}")
        print()
