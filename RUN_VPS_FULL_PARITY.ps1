# RUN_VPS_FULL_PARITY.ps1
# =======================
# Runs the COMPLETE live-vs-backtest audit on the VPS, in order:
#
#   1. Snapshot the live sizer + breaker + journal state (so we can see warmup status)
#   2. Pull the last 3 months of M1 data from 5%ers MT5 (covers the LIVE TRADING WINDOW)
#   3. Re-run the v30 backtest engine on that fresh data (with the live RISK %)
#   4. Run the 5-way parity audit so we get a side-by-side comparison
#   5. Compare live journal entries vs backtest engine on the overlap
#   6. Print the verdict + dump all of it to Results/VPS_FULL_PARITY_<ts>.log
#
# Run from the PropBot folder on the VPS:
#
#     cd C:\PropBot
#     git pull
#     .\RUN_VPS_FULL_PARITY.ps1
#
# Expected runtime: 3-5 minutes (MT5 pull dominates).
# All output is also captured to a timestamped log file in Results\.

$ErrorActionPreference = "Continue"
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$logDir = Join-Path $PSScriptRoot "Results"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "VPS_FULL_PARITY_$ts.log"

function Tee-LogLine {
    param($Line)
    $Line | Tee-Object -FilePath $logFile -Append | Out-Host
}

Tee-LogLine ""
Tee-LogLine ("=" * 100)
Tee-LogLine "  VPS FULL PARITY AUDIT — $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Tee-LogLine "  PropBot root: $PSScriptRoot"
Tee-LogLine "  Log file:     $logFile"
Tee-LogLine ("=" * 100)

# ----------------------------------------------------------------------------
# Step 1: snapshot the live state (sizer + breaker + recent journal)
# ----------------------------------------------------------------------------
Tee-LogLine ""
Tee-LogLine ("─" * 100)
Tee-LogLine "  STEP 1/6  —  Snapshot current LIVE state"
Tee-LogLine ("─" * 100)

$state1 = @"
import json, os, sys
from pathlib import Path
root = Path(r'$PSScriptRoot')
print()
print('  ─── SIZER STATE  (Results/v30_state/sizer_mertongz.json) ───')
sp = root / 'Results' / 'v30_state' / 'sizer_mertongz.json'
if not sp.exists():
    print(f'  ✗  MISSING — bot has never written sizer state (or it was just deleted)')
else:
    d = json.loads(sp.read_text(encoding='utf-8'))
    n_seen = d.get('n_seen', {})
    mu     = d.get('mu', {})
    sigma2 = d.get('sigma2', {})
    print(f'  schema = {d.get(\"schema\", \"?\")}')
    print(f'  symbols seen: {list(n_seen.keys())}')
    print(f'  {\"key\":<10s} {\"n\":>4s}  {\"mu\":>9s}  {\"sigma2\":>9s}')
    for k in sorted(n_seen):
        n = n_seen.get(k, 0)
        m = mu.get(k, 0.0)
        s = sigma2.get(k, 1.0)
        warm = 'WARM' if n >= 15 else 'COLD'
        print(f'  {k:<10s} {n:>4d}  {m:>+9.4f}  {s:>9.4f}  {warm}')
    total_n = sum(n_seen.values())
    if total_n == 0:
        print('  ⚠️  WARMUP_STATUS: COLD-START (n_seen total = 0). Bot is sizing at base risk.')
    elif total_n < 15:
        print(f'  ⚠️  WARMUP_STATUS: WARMING (n_seen total = {total_n} / 15). Still at base risk.')
    else:
        print(f'  ✓  WARMUP_STATUS: WARM (n_seen total = {total_n} ≥ 15). Sizer using Merton formula.')

print()
print('  ─── BREAKER STATE  (Results/v30_state/dd_breaker.json) ───')
bp = root / 'Results' / 'v30_state' / 'dd_breaker.json'
if not bp.exists():
    print(f'  ✗  MISSING')
else:
    d = json.loads(bp.read_text(encoding='utf-8'))
    print(json.dumps(d, indent=2)[:1500])
    if d.get('halted'):
        print('  ⚠️  HALTED — DD breaker is currently active. Bot sizes to zero until reset.')
    else:
        # Compute current DD from peak vs current equity if both present
        pe = d.get('peak_equity')
        ce = d.get('current_equity') or d.get('last_equity')
        if pe and ce:
            dd_pct = 100.0 * (pe - ce) / pe
            print(f'  current DD vs peak: {dd_pct:.2f} %')

print()
print('  ─── RECENT LIVE TRADES (last 20 entries) ───')
lp = root / 'Results' / 'v30_live_trades.jsonl'
if not lp.exists():
    print('  ✗  MISSING')
else:
    lines = lp.read_text(encoding='utf-8').strip().split(chr(10))
    entries = [json.loads(l) for l in lines if l.strip() and 'ENTRY' in l]
    print(f'  total entries on disk: {len(entries)}')
    for e in entries[-20:]:
        ts  = e.get('ts','')[:16]
        sym = e.get('symbol','')[:6]
        side = '+' if e.get('side',0) > 0 else '-'
        lots = e.get('lots',0)
        risk = e.get('risk_dollars',0) or e.get('risk_usd',0) or 0
        conf = e.get('edge_mult', e.get('confidence', '?'))
        print(f'  {ts}  {sym:<6s} {side} lots={lots:>7.4f}  risk=\${risk:>7.2f}  edge_mult={conf}')
"@

python -c $state1 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Host

# ----------------------------------------------------------------------------
# Step 2: pull fresh M1 data covering the live window
# ----------------------------------------------------------------------------
Tee-LogLine ""
Tee-LogLine ("─" * 100)
Tee-LogLine "  STEP 2/6  —  Pull fresh 3-month M1 data from 5%ers MT5"
Tee-LogLine ("─" * 100)

python Scripts/download_5ers_3month.py 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Host

# Verify the data now extends through today
Tee-LogLine ""
Tee-LogLine "  Verifying data coverage AFTER download:"
$verify = @"
import pandas as pd
from pathlib import Path
root = Path(r'$PSScriptRoot')
for s in ['DE40','US30','US500','XAUUSD']:
    p = root / 'data' / 'historical' / f'{s}_M1.csv'
    if p.exists():
        df = pd.read_csv(p, usecols=[0])
        print(f'  {s}: {df.iloc[0,0]}  ->  {df.iloc[-1,0]}   ({len(df):,} bars)')
    else:
        print(f'  {s}: MISSING')
"@
python -c $verify 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Host

# ----------------------------------------------------------------------------
# Step 3: re-run baseline backtest on the fresh data (so we see the OOS period)
# ----------------------------------------------------------------------------
Tee-LogLine ""
Tee-LogLine ("─" * 100)
Tee-LogLine "  STEP 3/6  —  Re-run v30 BASELINE backtest on FRESH data (incl. live window)"
Tee-LogLine ("─" * 100)

python Scripts/backtest_v30_fresh.py 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Host

# ----------------------------------------------------------------------------
# Step 4: 5-run parity audit (now on fresh data)
# ----------------------------------------------------------------------------
Tee-LogLine ""
Tee-LogLine ("─" * 100)
Tee-LogLine "  STEP 4/6  —  5-run PARITY AUDIT on FRESH data"
Tee-LogLine ("─" * 100)

$parityJson = Join-Path $logDir "parity_v31_VPS_$ts.json"
python Scripts/parity_v31_full_audit.py --json $parityJson 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Host

# ----------------------------------------------------------------------------
# Step 5: extract the OOS-only window (Apr 21 -> today) from the fresh backtest
# ----------------------------------------------------------------------------
Tee-LogLine ""
Tee-LogLine ("─" * 100)
Tee-LogLine "  STEP 5/6  —  OOS-only slice (Apr 21 -> today) from the fresh engine run"
Tee-LogLine ("─" * 100)

$oos = @"
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
root = Path(r'$PSScriptRoot')
p = root / 'Results' / 'v30_fresh_trades.json'
if not p.exists():
    print('  ✗  v30_fresh_trades.json not found — step 3 must have failed')
else:
    trades = json.loads(p.read_text(encoding='utf-8'))
    # OOS slice = trades whose exit_time >= 2026-04-21
    cutoff = datetime(2026, 4, 21, tzinfo=timezone.utc)
    oos_trades = []
    for t in trades:
        et = t.get('exit_time','')
        try:
            dt = datetime.fromisoformat(et.replace('Z','+00:00'))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                oos_trades.append(t)
        except Exception:
            pass
    n = len(oos_trades)
    net = sum(t['net_pnl'] for t in oos_trades)
    wins = sum(1 for t in oos_trades if t['net_pnl'] > 0)
    wr = 100.0*wins/max(1,n)
    print(f'  OOS window: {cutoff.date()} -> today')
    print(f'  trades:     {n}')
    print(f'  net PnL:    \${net:+,.2f}')
    print(f'  win rate:   {wr:.1f}%')
    bysym = defaultdict(lambda: [0,0.0,0])
    for t in oos_trades:
        d = bysym[t['symbol']]
        d[0] += 1
        d[1] += t['net_pnl']
        if t['net_pnl'] > 0: d[2] += 1
    print()
    print(f'  per-symbol OOS:')
    print(f'  {\"sym\":<8s} {\"n\":>4s}  {\"net\":>12s}  {\"WR%\":>6s}')
    for sym in sorted(bysym):
        d = bysym[sym]
        wr2 = 100.0*d[2]/max(1,d[0])
        print(f'  {sym:<8s} {d[0]:>4d}  \${d[1]:>+10,.2f}  {wr2:>5.1f}%')
"@
python -c $oos 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Host

# ----------------------------------------------------------------------------
# Step 6: per-trade diff between live journal and backtest engine
# (THIS is the step that actually answers "is the bot wired up correctly?")
# ----------------------------------------------------------------------------
Tee-LogLine ""
Tee-LogLine ("─" * 100)
Tee-LogLine "  STEP 6/7  —  Per-trade LIVE vs BACKTEST diagnostic (sizing / wiring)"
Tee-LogLine ("─" * 100)

python Scripts/diag_v31_live_vs_backtest.py 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Host

# ----------------------------------------------------------------------------
# Step 7: the verdict
# ----------------------------------------------------------------------------
Tee-LogLine ""
Tee-LogLine ("=" * 100)
Tee-LogLine "  STEP 7/7  —  VERDICT"
Tee-LogLine ("=" * 100)
Tee-LogLine ""
Tee-LogLine "  Compare the OOS slice above to your actual live trades."
Tee-LogLine ""
Tee-LogLine "    ─ If OOS net PnL is strongly POSITIVE  →  bot WIRING is broken (warmup / breaker / config drift)"
Tee-LogLine "    ─ If OOS net PnL is ~0 or NEGATIVE     →  edge has eroded; strategy needs retuning"
Tee-LogLine "    ─ If OOS shows different signal counts →  data isn't matching what the bot sees live"
Tee-LogLine ""
Tee-LogLine "  Also check STEP 1 sizer state:"
Tee-LogLine "    ─ n_seen total < 15  →  sizer never warmed up after restart (seeding bug)"
Tee-LogLine "    ─ breaker halted=true →  every new trade sized to ~\$0; deadlock"
Tee-LogLine ""
Tee-LogLine "  Full log saved to:  $logFile"
Tee-LogLine "  Parity JSON saved:  $parityJson"
Tee-LogLine ("=" * 100)
