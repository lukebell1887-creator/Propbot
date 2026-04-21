"""Check what data source the 5 TIER 1 CSV files actually came from, and over what dates."""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

syms = ['US30','US100','US500','DE40','XAUUSD']
for s in syms:
    p = ROOT / f"data/historical/{s}_M1.csv"
    df = pd.read_csv(p)
    print(f"{s:<8} n={len(df):>8,}  cols={list(df.columns)}")
    print(f"    first row: {df.iloc[0].tolist()}")
    print(f"    last  row: {df.iloc[-1].tolist()}")
    print()
