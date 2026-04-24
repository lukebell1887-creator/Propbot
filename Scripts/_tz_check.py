"""
TZ-CHECK: prove which clock the CSV timestamps are on.

Real-world anchors we know for certain:
- NYSE cash session runs 09:30-16:00 ET = 13:30-20:00 UTC (in DST).
- DAX Xetra cash session runs 09:00-17:30 CEST = 07:00-15:30 UTC (in DST).
- London FX liquidity peaks 08:00-16:00 UTC (during overlap with NY 13:30-16:00 is highest).

If CSV time column is REAL UTC:
  NYSE peak volume should be at CSV hour 14-19.
If CSV time is broker GMT+3:
  NYSE peak volume should be at CSV hour 17-22.
"""
import pandas as pd
from pathlib import Path

print("=" * 70)
print("CSV timestamp audit — when does volume actually peak?")
print("=" * 70)
for sym in ("DE40", "US30", "US500", "XAUUSD"):
    df = pd.read_csv(f"data/historical/{sym}_M1.csv", parse_dates=["time"])
    by_h = df.groupby(df["time"].dt.hour)["tick_volume"].mean()
    top5 = by_h.nlargest(5).sort_index()
    print(f"\n{sym:8s}  top-5 hours (CSV clock) and mean ticks/bar:")
    for h, v in top5.items():
        print(f"          CSV hour {h:02d}:00 -> mean {v:6.0f} ticks/bar")

print("\n" + "=" * 70)
print("INTERPRETATION")
print("=" * 70)
print("""
Known facts (April 2026, during DST):
  - NYSE opens 13:30 UTC, first-hour is 13:30-14:30 UTC (highest vol for equities)
  - NYSE close 20:00 UTC
  - If CSV peak is 16:00-19:00 -> CSV clock is UTC+2-3 (BROKER TIME)
  - If CSV peak is 13:00-16:00 -> CSV clock is real UTC

Gold is cleanest — it peaks at London-NY overlap = 13:00-16:00 UTC real.
""")
