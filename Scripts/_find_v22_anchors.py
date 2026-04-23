import sys, csv, os, json
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')

data = Path('data/historical')
# provenance first
prov_path = data / '_provenance.json'
if prov_path.exists():
    p = json.load(open(prov_path))
    print('PROVENANCE:')
    for k, v in p.items():
        if isinstance(v, dict):
            print(f'  {k}: server={v.get("server","?")} rows={v.get("rows","?")} first={v.get("first","?")} last={v.get("last","?")}')
        else:
            print(f'  {k}: {v}')

print('\n--- DATE RANGES (file scan) ---')
for s in ['DE40','US30','US500','XAUUSD','UK100','US100','JP225','XAGUSD']:
    p = data / f'{s}_M1.csv'
    if not p.exists():
        print(f'{s:8s} MISSING')
        continue
    with open(p) as f:
        rdr = csv.reader(f); hdr = next(rdr)
        first = next(rdr, None)
        rows = 1
        last = first
        for r in rdr:
            rows += 1
            last = r
    print(f'{s:8s} rows={rows:>7d}  first={first[0]}  last={last[0]}')
