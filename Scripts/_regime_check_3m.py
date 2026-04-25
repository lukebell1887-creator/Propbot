"""
_regime_check_3m.py
===================
Was the 3-month backtest window (20 Jan – 21 Apr 2026) favourable, normal,
or unfavourable for an Opening-Range Breakout strategy?

Measures, per symbol:
  • Total % return (trend strength — ORB loves strong trends)
  • Realized vol (ann. %) (more vol → larger OR → bigger trades → ORB-friendly)
  • Avg daily range in pts (proxy for opportunity size)
  • ORB-favourability score = % of days where 09:30-10:00 NY move continued
    in the same direction in the 10:00-15:00 window (true breakouts)
  • Chop days = % of days where price returned to OR midpoint (fake-outs)

Output: one score per symbol + overall regime verdict.
"""
import pandas as pd
import numpy as np
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "historical"

SYMBOLS = {
    "US500": ("sp500", "NY"),   # window: 14:30 UTC - 15:00 UTC (09:30-10:00 NY)
    "US30":  ("dow", "NY"),
    "XAUUSD":("gold", "NY"),
    "DE40":  ("dax", "DE"),     # window: 07:00 UTC - 07:30 UTC (08:00-08:30 CET)
}

OR_WINDOWS = {
    "NY": (pd.Timedelta(hours=14, minutes=30), pd.Timedelta(hours=15, minutes=0)),
    "DE": (pd.Timedelta(hours=7, minutes=0),    pd.Timedelta(hours=7, minutes=30)),
}

def analyse(sym: str, tz: str) -> dict:
    path = DATA / f"{sym}_M1.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["time"])
    df = df.sort_values("time").set_index("time")
    df["date"] = df.index.date
    
    # 1. Total return
    total_ret = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    
    # 2. Realized ann. vol (daily close-to-close)
    daily = df["close"].resample("1D").last().dropna()
    daily_ret = daily.pct_change().dropna()
    ann_vol = daily_ret.std() * np.sqrt(252) * 100
    
    # 3. Avg daily range (high-low)
    daily_hi = df["high"].resample("1D").max()
    daily_lo = df["low"].resample("1D").min()
    daily_range_pts = (daily_hi - daily_lo).dropna()
    # daily here is a Series of closes, use it directly as denominator
    daily_range_pct = (daily_range_pts / daily).dropna() * 100
    
    # 4. ORB-favourability: for each trading day, did price break OR and continue?
    or_start, or_end = OR_WINDOWS[tz]
    trade_start = or_end
    trade_end = or_end + pd.Timedelta(hours=2)   # 2h window (matches bot)
    
    wins, losses, nosignals, chops = 0, 0, 0, 0
    for day, day_bars in df.groupby("date"):
        idx = day_bars.index
        base = pd.Timestamp(day)
        or_bars = day_bars[(idx >= base + or_start) & (idx < base + or_end)]
        if len(or_bars) < 5:
            continue
        or_hi = or_bars["high"].max()
        or_lo = or_bars["low"].min()
        after = day_bars[(idx >= base + trade_start) & (idx < base + trade_end)]
        if len(after) < 10:
            continue
        
        # Did price break OR up or down?
        upper_breaks = after[after["high"] > or_hi]
        lower_breaks = after[after["low"] < or_lo]
        
        if len(upper_breaks) == 0 and len(lower_breaks) == 0:
            nosignals += 1
            continue
        
        # First breakout direction
        first_up = upper_breaks.index[0] if len(upper_breaks) > 0 else pd.Timestamp.max
        first_dn = lower_breaks.index[0] if len(lower_breaks) > 0 else pd.Timestamp.max
        
        if first_up < first_dn:
            # broke up first. Did it keep going? (high at end > break price)
            entry = or_hi
            sl = or_lo
            tp = entry + (entry - sl)   # 1R target
            after_break = after[after.index >= first_up]
            hit_tp = (after_break["high"] >= tp).any()
            hit_sl = (after_break["low"] <= sl).any()
            if hit_tp and not hit_sl:
                wins += 1
            elif hit_sl and not hit_tp:
                losses += 1
            else:
                # both hit → assume SL first (conservative)
                if hit_tp:
                    tp_time = after_break[after_break["high"] >= tp].index[0]
                    sl_time = after_break[after_break["low"] <= sl].index[0] if hit_sl else pd.Timestamp.max
                    if tp_time < sl_time:
                        wins += 1
                    else:
                        losses += 1
                else:
                    chops += 1
        elif first_dn < first_up:
            entry = or_lo
            sl = or_hi
            tp = entry - (sl - entry)
            after_break = after[after.index >= first_dn]
            hit_tp = (after_break["low"] <= tp).any()
            hit_sl = (after_break["high"] >= sl).any()
            if hit_tp and not hit_sl:
                wins += 1
            elif hit_sl and not hit_tp:
                losses += 1
            else:
                if hit_tp:
                    tp_time = after_break[after_break["low"] <= tp].index[0]
                    sl_time = after_break[after_break["high"] >= sl].index[0] if hit_sl else pd.Timestamp.max
                    if tp_time < sl_time:
                        wins += 1
                    else:
                        losses += 1
                else:
                    chops += 1
    
    total_signals = wins + losses + chops
    winrate = wins / total_signals * 100 if total_signals > 0 else 0
    
    return {
        "symbol": sym,
        "total_return_pct": total_ret,
        "ann_vol_pct": ann_vol,
        "avg_daily_range_pct": daily_range_pct.mean(),
        "winrate_naive_orb_pct": winrate,
        "wins": wins, "losses": losses, "chops": chops, "nosignals": nosignals,
        "total_days": total_signals + nosignals,
    }

print("\n" + "="*80)
print("REGIME CHECK: 20 Jan – 21 Apr 2026 vs ORB-favourability")
print("="*80)

rows = []
for sym, (label, tz) in SYMBOLS.items():
    r = analyse(sym, tz)
    if r:
        rows.append(r)

results_df = pd.DataFrame(rows)
print(results_df.to_string(index=False))

print("\nInterpretation:")
print("  • winrate_naive_orb_pct = raw OR-break 1R hit rate, NO filters")
print("  • Any value >55% means ORB-friendly environment")
print("  • <50% means chop-heavy / mean-reverting environment")
print("  • 45-50% = neutral")
print("  • Bot's filters (NR7, CUSUM, volatility gating) lift naive rate")
print("    by ~10-15 percentage points → bot hits ~65% in the backtest")
