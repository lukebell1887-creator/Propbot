"""Quick inventory of data/historical."""
import pandas as pd
from pathlib import Path

DATA = Path("data/historical")

print(f"{'Symbol':<10} {'Start':<20} {'End':<20} {'Bars':>10} {'Days':>6}")
print("-" * 68)
for p in sorted(DATA.glob("*_M1.csv")):
    df = pd.read_csv(p, usecols=["time"])
    df["time"] = pd.to_datetime(df["time"])
    start = df["time"].iloc[0]
    end = df["time"].iloc[-1]
    days = (end - start).days
    print(f"{p.stem.replace('_M1',''):<10} {str(start):<20} {str(end):<20} {len(df):>10,} {days:>6}")
