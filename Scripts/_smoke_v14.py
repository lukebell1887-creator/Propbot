#!/usr/bin/env python3
"""Quick smoke test of v14 engine on 1 month of US100."""
import csv, sys
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.smartbb_engine_v14 import (
    SmartBBV14Engine, SmartBBV14Config, SymbolParams, SMARTBB_UNIVERSE)

rows = []
with open(ROOT / 'data/historical/US100_M1.csv') as f:
    rdr = csv.DictReader(f)
    for row in rdr:
        try: t = datetime.fromisoformat(row['time'])
        except: t = datetime.strptime(row['time'], '%Y-%m-%d %H:%M:%S')
        rows.append((t, float(row['open']), float(row['high']), float(row['low']), float(row['close'])))

end = rows[-1][0]
start = end - timedelta(days=30)
rows = [r for r in rows if r[0] >= start]
print(f'Loaded {len(rows)} M1 bars of US100 from {start} to {end}', flush=True)

cfg = SmartBBV14Config()
eng = SmartBBV14Engine(
    symbols=[SMARTBB_UNIVERSE['US100']],
    params={},
    cfg=cfg,
    initial_equity=100_000.0,
)
for (t, o, h, l, c) in rows:
    eng.on_bar('US100', t.timestamp(), t.strftime('%Y-%m-%d'),
                t.hour, t.minute, o, h, l, c)

s = eng.summary()
print(f'v14 default params on US100 (30d):', flush=True)
print(f'  trades     = {s["trades"]}', flush=True)
print(f'  net_pnl    = ${s["net_pnl"]:.2f}', flush=True)
print(f'  win_rate   = {s["win_rate"]*100:.1f}%', flush=True)
print(f'  PF         = {s["pf"]:.2f}', flush=True)
print(f'  max_dd     = {s["max_dd_pct"]:.2f}%', flush=True)
print(f'  exit_reasons: {s["by_exit_reason"]}', flush=True)
