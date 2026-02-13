#!/usr/bin/env python3
"""
Gold/Silver (XAUUSD/XAGUSD) Backtest — Exact live bot engine parameters.
Tests HMM holds: 100, 20, 10, 5 with dwell bases: 60, 300, 1800
Commission: 0.0009% of notional per lot per side for both
Contract size: Gold=100oz, Silver=5000oz
"""
import sys, math, time, json, numpy as np, pandas as pd
from pathlib import Path
from collections import deque, defaultdict
from dataclasses import dataclass
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

# ── Exact live bot engine parameters ──
WELFORD_SPAN = 100
Z_BASE = 2.0
GAMMA = 6.0
HURST_WINDOW = 512
EXIT_Z_BASE = 0.5
EXIT_GAMMA = 2.0

# ── AKAD ──
DAKAD_LAMBDA = 40.0
DAKAD_P_RUIN = 1e-4
DAKAD_DAILY_DD_CEIL = 0.04
DAKAD_RESULT_WINDOW = 50
DAKAD_MIN_WR = 0.50
DAKAD_MAX_WR = 0.85
DAKAD_MIN_BASE = 0.003
DAKAD_MAX_BASE = 0.03
DAKAD_RISK_FLOOR = 0.0005

# ── Risk limits ──
GHOST_DAILY_DD = 0.04
GHOST_MAX_DD = 0.09
KALMAN_TOLERANCE = 0.15
CORR_WINDOW = 200
HMM_LOOKBACK = 100
MAX_CONSEC_LOSSES = 5
COOLDOWN_BARS = 60
MIN_WARMUP_BARS = 200
ROLLOVER_MIN = 30
BAL = 100_000.0

# ── Gold/Silver specific ──
GOLD_CONTRACT = 100       # 100 oz per lot
SILVER_CONTRACT = 5000    # 5000 oz per lot
COMMISSION_PCT = 0.000009 # 0.0009% = 0.000009

def spread_multiplier(hour):
    """Session-dependent spread widening (same as live bot)."""
    if 0 <= hour < 7:   return 1.8
    elif 7 <= hour < 9: return 1.2
    elif 9 <= hour < 17: return 1.0
    elif 17 <= hour < 21: return 1.1
    else: return 1.5

def calc_cost(lots, gold_price, silver_price, gold_spread_pts, silver_spread_pts, hour):
    """
    Total round-trip cost for Gold/Silver pair trade.
    - Bid-ask spread cost (both legs, entry + exit = x2)
    - Commission: 0.0009% of notional per side (2 sides per leg, 2 legs)
    """
    sm = spread_multiplier(hour)
    
    # Spread costs (bid-ask): spread in price points × contract size × lots × 2 (round trip) × multiplier
    # Gold spread: pts are in $0.01 units on MT5, so gold_spread_pts * 0.01 = $ spread
    # Silver spread: pts are in $0.001 units on MT5, so silver_spread_pts * 0.001 = $ spread
    gold_ba_cost = gold_spread_pts * 0.01 * GOLD_CONTRACT * lots * 2 * sm
    silver_ba_cost = silver_spread_pts * 0.001 * SILVER_CONTRACT * lots * 2 * sm
    
    # Commission: 0.0009% of notional per lot per side, 2 sides per leg, 2 legs
    gold_notional = gold_price * GOLD_CONTRACT * lots
    silver_notional = silver_price * SILVER_CONTRACT * lots
    commission = (gold_notional + silver_notional) * COMMISSION_PCT * 2 * 2
    
    return gold_ba_cost + silver_ba_cost + commission


class DynamicAKAD:
    def __init__(self):
        self._results = deque(maxlen=DAKAD_RESULT_WINDOW)
        for _ in range(10): self._results.append(1)
        for _ in range(5):  self._results.append(0)

    def record(self, win):
        self._results.append(1 if win else 0)

    def calc_risk(self, total_dd, daily_dd):
        dd_room = max(0.001, DAKAD_DAILY_DD_CEIL - daily_dd)
        wr = max(DAKAD_MIN_WR, min(DAKAD_MAX_WR, sum(self._results) / max(len(self._results), 1)))
        n_survive = math.log(DAKAD_P_RUIN) / math.log(1.0 - wr)
        base = max(DAKAD_MIN_BASE, min(DAKAD_MAX_BASE,
            (math.exp(DAKAD_LAMBDA * dd_room) - 1) / (DAKAD_LAMBDA * n_survive)))
        return max(DAKAD_RISK_FLOOR, base * math.exp(-DAKAD_LAMBDA * total_dd))


class SimpleHMM:
    def __init__(self, lookback=100, min_hold=20):
        self._lb = lookback
        self._cr = 0
        self._buf = []
        self._hold_count = 0
        self._mh = min_hold

    def update(self, spread_return):
        self._buf.append(spread_return)
        if len(self._buf) > self._lb * 3:
            self._buf = self._buf[-self._lb * 2:]
        if len(self._buf) < 50:
            return 0
        r = np.array(self._buf[-self._lb:])
        n = len(r)
        ws = min(20, n // 3)
        if ws < 5:
            return 0
        vols = [np.std(r[i:i+ws]) for i in range(0, n - ws + 1, ws)]
        if len(vols) < 3:
            return 0
        v40 = np.percentile(vols, 40)
        v80 = np.percentile(vols, 80)
        nr = 0 if vols[-1] <= v40 else (1 if vols[-1] <= v80 else 2)
        self._hold_count += 1
        if nr != self._cr and self._hold_count >= self._mh:
            self._cr = nr
            self._hold_count = 0
        return self._cr

    @property
    def blocked(self):
        return self._cr >= 2


def load_data():
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    gold = pd.read_csv(d / "XAUUSD_M1.csv", parse_dates=['time']).rename(columns={'close': 'gold_close', 'spread': 'gold_spread'})
    silver = pd.read_csv(d / "XAGUSD_M1.csv", parse_dates=['time']).rename(columns={'close': 'silver_close', 'spread': 'silver_spread'})
    
    merged = pd.merge(
        gold[['time', 'gold_close', 'gold_spread']],
        silver[['time', 'silver_close', 'silver_spread']],
        on='time', how='inner'
    ).sort_values('time').reset_index(drop=True)
    
    merged = merged[(merged['gold_close'] > 0) & (merged['silver_close'] > 0)].reset_index(drop=True)
    return merged


def dwell_time(hurst, dwell_base, anchor_h=0.3):
    """Dynamic dwell: dwell_base * (H / anchor_H), clamped."""
    dmin = dwell_base * 0.5
    dmax = dwell_base * 5.0
    return max(dmin, min(dmax, dwell_base * (hurst / anchor_h)))


def run_backtest(df, hmm_hold, dwell_base, notional_mult=100000.0):
    """Run full backtest with exact live bot parameters."""
    bal = BAL
    peak = BAL
    day_start = BAL
    last_date = None
    ghost_halt = False
    
    dakad = DynamicAKAD()
    corr_mon = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec_losses = 0
    cooldown_until = 0
    
    n = len(df)
    
    # Rust Cointegration Engine
    eng = shf_core.CointegrationEngine(
        span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE, exit_z=EXIT_Z_BASE,
        z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW, dynamic_z=True,
        exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True
    )
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = SimpleHMM(lookback=HMM_LOOKBACK, min_hold=hmm_hold)
    
    # State
    position = 0
    entry_z = 0
    entry_spread = 0
    entry_bar = 0
    entry_lots = 0
    last_spread = 0
    prev_spread = 0
    sentinel_abort = False
    last_close_bar = -9999
    last_close_hurst = 0.5
    entry_hour = 0
    entry_time = None
    
    trades = []
    equity_curve = [BAL]
    hmm_blocks = 0
    sentinel_blocks = 0
    
    for bar in range(n):
        if ghost_halt:
            break
            
        row = df.iloc[bar]
        bt = row['time']
        pa = float(row['gold_close'])
        pb = float(row['silver_close'])
        ga_spread = float(row['gold_spread'])
        si_spread = float(row['silver_spread'])
        
        # Daily reset
        cd = bt.date() if hasattr(bt, 'date') else None
        if cd and cd != last_date:
            last_date = cd
            day_start = bal
        
        # Drawdown checks
        total_dd = max(0, (peak - bal) / peak) if peak > 0 else 0
        daily_dd = max(0, (day_start - bal) / day_start) if day_start > 0 else 0
        if daily_dd >= GHOST_DAILY_DD:
            ghost_halt = True
            break
        if total_dd >= GHOST_MAX_DD:
            ghost_halt = True
            break
        
        # Cooldown check
        if bar < cooldown_until:
            continue
        
        # Update engine
        prev_spread = last_spread
        sig = eng.update(pa, pb)
        z = sig.z_score
        signal = sig.signal
        spread = sig.spread
        last_spread = spread
        
        hurst = eng.last_hurst
        exit_z = eng.last_exit_z
        
        # Correlation monitor
        if prev_spread != 0:
            corr_mon.push_return(2, spread - prev_spread)  # pair index 2 (3rd pair)
        
        # Kalman sentinel
        la = math.log(pa) if pa > 0 else 0
        lb = math.log(pb) if pb > 0 else 0
        beta, abort = sen.update(la, lb)
        
        if abort and not sentinel_abort:
            sentinel_abort = True
            sentinel_blocks += 1
            if position != 0:
                # Force close
                gross = (spread - entry_spread) * position * entry_lots * notional_mult
                cost = calc_cost(entry_lots, pa, pb, ga_spread, si_spread, entry_hour)
                pnl = gross - cost
                bal += pnl
                peak = max(peak, bal)
                win = pnl > 0
                dakad.record(win)
                if not win:
                    consec_losses += 1
                else:
                    consec_losses = 0
                if consec_losses >= MAX_CONSEC_LOSSES:
                    cooldown_until = bar + COOLDOWN_BARS
                    consec_losses = 0
                trades.append({
                    'pnl': pnl, 'gross': gross, 'cost': cost, 'hold': bar - entry_bar,
                    'entry_hour': entry_hour, 'exit_hour': bt.hour,
                    'entry_time': entry_time, 'exit_time': bt, 'reason': 'SENTINEL'
                })
                position = 0
                last_close_bar = bar
                last_close_hurst = hurst
            continue
        
        if sentinel_abort and not abort:
            sentinel_abort = False
        if sentinel_abort:
            continue
        
        # HMM update
        hmm_blocked = False
        if prev_spread != 0:
            hmm.update(spread - prev_spread)
            hmm_blocked = hmm.blocked
            if hmm_blocked:
                hmm_blocks += 1
        
        if bar < MIN_WARMUP_BARS:
            continue
        
        # Rollover lockout
        mins_since_midnight = bt.hour * 60 + bt.minute
        mins_before_midnight = 1440 - mins_since_midnight
        in_rollover = mins_since_midnight < ROLLOVER_MIN or mins_before_midnight < ROLLOVER_MIN
        
        # ── ENTRY ──
        if position == 0 and signal != 0:
            if hmm_blocked:
                continue
            if in_rollover:
                continue
            
            # Dwell check
            if last_close_bar >= 0:
                dwell_bars = dwell_time(last_close_hurst, dwell_base) / 60.0
                if (bar - last_close_bar) < dwell_bars:
                    continue
            
            # AKAD sizing
            risk = dakad.calc_risk(total_dd, daily_dd)
            corr_mon.compute_risk()
            cm = corr_mon.last_risk_multiplier
            lots = max(0.01, round(bal * risk * cm / 1000.0, 2))
            
            position = signal
            entry_z = z
            entry_spread = spread
            entry_bar = bar
            entry_lots = lots
            entry_hour = bt.hour
            entry_time = bt
        
        # ── EXIT ──
        elif position != 0:
            should_exit = False
            reason = ""
            
            # Emergency exit
            if abs(z) > abs(entry_z) * 2.5:
                should_exit = True
                reason = "EMERGENCY"
            
            if not should_exit:
                # Dwell check for exit
                held_bars = bar - entry_bar
                min_hold = dwell_time(hurst, dwell_base) / 60.0
                if held_bars < min_hold:
                    continue
                
                # Dynamic Z exit
                if position == 1 and z > -exit_z:
                    should_exit = True
                    reason = "DYN_EXIT"
                elif position == -1 and z < exit_z:
                    should_exit = True
                    reason = "DYN_EXIT"
            
            if should_exit:
                gross = (spread - entry_spread) * position * entry_lots * notional_mult
                cost = calc_cost(entry_lots, pa, pb, ga_spread, si_spread, entry_hour)
                pnl = gross - cost
                bal += pnl
                peak = max(peak, bal)
                win = pnl > 0
                dakad.record(win)
                if not win:
                    consec_losses += 1
                else:
                    consec_losses = 0
                if consec_losses >= MAX_CONSEC_LOSSES:
                    cooldown_until = bar + COOLDOWN_BARS
                    consec_losses = 0
                trades.append({
                    'pnl': pnl, 'gross': gross, 'cost': cost, 'hold': bar - entry_bar,
                    'entry_hour': entry_hour, 'exit_hour': bt.hour,
                    'entry_time': entry_time, 'exit_time': bt, 'reason': reason
                })
                position = 0
                last_close_bar = bar
                last_close_hurst = hurst
        
        equity_curve.append(bal)
    
    return trades, bal, equity_curve, hmm_blocks, sentinel_blocks


def analyze_trades(trades, label):
    """Print comprehensive trade analysis."""
    if not trades:
        print(f"  {label}: NO TRADES")
        return {}
    
    net = sum(t['pnl'] for t in trades)
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    wr = len(wins) / len(trades) * 100
    avg_win = np.mean([t['pnl'] for t in wins]) if wins else 0
    avg_loss = np.mean([abs(t['pnl']) for t in losses]) if losses else 0
    pf = sum(t['pnl'] for t in wins) / abs(sum(t['pnl'] for t in losses)) if losses else float('inf')
    avg_hold = np.mean([t['hold'] for t in trades])
    total_cost = sum(t['cost'] for t in trades)
    total_gross = sum(t['gross'] for t in trades)
    
    # Max drawdown
    equity = BAL
    peak = BAL
    max_dd = 0
    for t in trades:
        equity += t['pnl']
        peak = max(peak, equity)
        dd = (peak - equity) / peak
        max_dd = max(max_dd, dd)
    
    # Sharpe (annualized from daily)
    daily_pnl = defaultdict(float)
    for t in trades:
        day = t['exit_time'].date() if hasattr(t['exit_time'], 'date') else 'unknown'
        daily_pnl[day] += t['pnl']
    daily_returns = list(daily_pnl.values())
    sharpe = (np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252)) if len(daily_returns) > 5 and np.std(daily_returns) > 0 else 0
    
    print(f"\n  {label}")
    print(f"    Trades: {len(trades)} | WR: {wr:.1f}% | Net: ${net:,.2f} | PF: {pf:.2f}")
    print(f"    Avg Win: ${avg_win:,.2f} | Avg Loss: ${avg_loss:,.2f} | Avg Hold: {avg_hold:.0f} bars")
    print(f"    Gross: ${total_gross:,.2f} | Costs: ${total_cost:,.2f} ({total_cost/total_gross*100:.1f}% of gross)" if total_gross > 0 else f"    Gross: ${total_gross:,.2f}")
    print(f"    Max DD: {max_dd*100:.2f}% | Sharpe: {sharpe:.2f}")
    print(f"    Final Balance: ${BAL + net:,.2f} ({net/BAL*100:.2f}% return)")
    
    # Hour distribution
    hp = defaultdict(lambda: {'pnl': 0, 'n': 0, 'w': 0})
    for t in trades:
        h = t['entry_hour']
        hp[h]['pnl'] += t['pnl']
        hp[h]['n'] += 1
        if t['pnl'] > 0:
            hp[h]['w'] += 1
    
    print(f"    {'Hour':>6} {'Trades':>7} {'WR':>6} {'Net P&L':>12} {'Avg':>10}")
    for h in sorted(hp.keys()):
        d = hp[h]
        avg = d['pnl'] / d['n'] if d['n'] else 0
        w = d['w'] / d['n'] * 100 if d['n'] else 0
        print(f"    {h:>6} {d['n']:>7} {w:>5.1f}% ${d['pnl']:>11,.2f} ${avg:>9,.2f}")
    
    return {
        'trades': len(trades), 'wr': wr, 'net': net, 'pf': pf,
        'sharpe': sharpe, 'max_dd': max_dd, 'avg_hold': avg_hold,
        'total_cost': total_cost, 'total_gross': total_gross,
    }


def main():
    t0 = time.time()
    print("=" * 100)
    print("GOLD/SILVER (XAUUSD/XAGUSD) PAIR EVALUATION")
    print("Exact v5.6.3 engine parameters | Commission: 0.0009% per lot per side")
    print("Gold: 100oz/lot | Silver: 5000oz/lot")
    print("=" * 100)
    
    df = load_data()
    print(f"\nData: {len(df)} aligned M1 bars")
    print(f"Range: {df['time'].iloc[0]} to {df['time'].iloc[-1]}")
    print(f"Gold: ${df['gold_close'].min():.2f} - ${df['gold_close'].max():.2f}")
    print(f"Silver: ${df['silver_close'].min():.4f} - ${df['silver_close'].max():.4f}")
    
    # Check spread data
    avg_gold_spread = df['gold_spread'].mean()
    avg_silver_spread = df['silver_spread'].mean()
    print(f"Avg Gold Spread: {avg_gold_spread:.1f} pts | Avg Silver Spread: {avg_silver_spread:.1f} pts")
    
    # Quick Hurst check
    print("\n--- QUICK HURST & COINTEGRATION CHECK ---")
    eng_check = shf_core.CointegrationEngine(
        span=100, beta=1.0, entry_z=2.0, exit_z=0.5,
        z_base=2.0, gamma=6.0, hurst_window=512, dynamic_z=True,
        exit_z_base=0.5, exit_gamma=2.0, dynamic_exit=True
    )
    for i in range(min(2000, len(df))):
        eng_check.update(float(df.iloc[i]['gold_close']), float(df.iloc[i]['silver_close']))
    h_sample = eng_check.last_hurst
    z_sample = eng_check.last_z_crit
    print(f"  Hurst after 2000 bars: {h_sample:.3f}")
    print(f"  Z_crit at that Hurst: {z_sample:.2f}")
    print(f"  Regime: {'Mean-reverting (good!)' if h_sample < 0.5 else 'Trending (needs high Z)' if h_sample < 0.6 else 'Strongly trending (very selective)'}")
    
    # ── Main test matrix ──
    HMM_HOLDS = [5, 10, 20, 100]
    DWELL_BASES = [60, 300, 1800]
    
    print("\n" + "=" * 100)
    print("BACKTEST MATRIX: HMM Hold × Dwell Base")
    print("=" * 100)
    
    results = {}
    best_key = None
    best_net = -float('inf')
    
    for dwell_base in DWELL_BASES:
        for hmm_hold in HMM_HOLDS:
            key = f"HMM={hmm_hold}_Dwell={dwell_base}s"
            print(f"\n  Running: {key}...", end="", flush=True)
            
            trades, final_bal, eq, hmm_bl, sen_bl = run_backtest(
                df, hmm_hold=hmm_hold, dwell_base=dwell_base, notional_mult=100000.0
            )
            
            net = sum(t['pnl'] for t in trades) if trades else 0
            wr = len([t for t in trades if t['pnl'] > 0]) / len(trades) * 100 if trades else 0
            tc = sum(t['cost'] for t in trades) if trades else 0
            
            # Max DD
            equity = BAL; peak_eq = BAL; max_dd = 0
            for t in trades:
                equity += t['pnl']; peak_eq = max(peak_eq, equity)
                max_dd = max(max_dd, (peak_eq - equity) / peak_eq)
            
            pf = 0
            if trades:
                wins_sum = sum(t['pnl'] for t in trades if t['pnl'] > 0)
                loss_sum = abs(sum(t['pnl'] for t in trades if t['pnl'] <= 0))
                pf = wins_sum / loss_sum if loss_sum > 0 else float('inf')
            
            results[key] = {
                'trades': len(trades), 'wr': wr, 'net': net, 'pf': pf,
                'max_dd': max_dd, 'hmm_blocks': hmm_bl, 'sentinel_blocks': sen_bl,
                'costs': tc, 'dwell': dwell_base, 'hmm': hmm_hold,
                'all_trades': trades
            }
            
            if net > best_net:
                best_net = net
                best_key = key
            
            print(f" {len(trades)} trades, WR={wr:.1f}%, Net=${net:,.2f}, PF={pf:.2f}, DD={max_dd*100:.1f}%")
    
    # Summary table
    print("\n" + "=" * 100)
    print("SUMMARY TABLE")
    print("=" * 100)
    print(f"  {'Config':<25} {'Trades':>7} {'WR':>6} {'Net P&L':>12} {'PF':>6} {'MaxDD':>7} {'Costs':>10} {'HMM Blk':>8} {'Sen Blk':>8}")
    print(f"  {'-'*23:<25} {'-'*5:>7} {'-'*4:>6} {'-'*10:>12} {'-'*4:>6} {'-'*5:>7} {'-'*8:>10} {'-'*6:>8} {'-'*6:>8}")
    
    for key in sorted(results.keys()):
        r = results[key]
        star = " <<<" if key == best_key else ""
        print(f"  {key:<25} {r['trades']:>7} {r['wr']:>5.1f}% ${r['net']:>11,.2f} {r['pf']:>5.2f} {r['max_dd']*100:>6.1f}% ${r['costs']:>9,.2f} {r['hmm_blocks']:>8} {r['sentinel_blocks']:>8}{star}")
    
    # Best config detailed analysis
    print("\n" + "=" * 100)
    print(f"BEST CONFIG: {best_key}")
    print("=" * 100)
    best_trades = results[best_key]['all_trades']
    analyze_trades(best_trades, best_key)
    
    # Compare with live pairs (from previous rollover test)
    print("\n" + "=" * 100)
    print("COMPARISON WITH LIVE PAIRS (from previous backtests)")
    print("=" * 100)
    print(f"  {'Pair':<20} {'Trades':>7} {'WR':>6} {'Net P&L':>12} {'MaxDD':>7} {'Per Month':>12}")
    print(f"  {'-'*18:<20} {'-'*5:>7} {'-'*4:>6} {'-'*10:>12} {'-'*5:>7} {'-'*10:>12}")
    
    # Calculate per-month for gold/silver
    if best_trades:
        date_range = (best_trades[-1]['exit_time'] - best_trades[0]['entry_time']).days
        months = max(1, date_range / 30.0)
        gs_per_month = best_net / months
    else:
        months = 1
        gs_per_month = 0
    
    print(f"  {'Index (NAS/DAX)':<20} {'98':>7} {'70.4':>5}% ${'4,377':>11} {'~2.0':>6}% ${'1,250':>11}")
    print(f"  {'Oil (WTI/Brent)':<20} {'659':>7} {'79.7':>5}% ${'28,389':>11} {'~1.5':>6}% ${'8,111':>11}")
    
    br = results[best_key]
    print(f"  {'Gold/Silver':<20} {br['trades']:>7} {br['wr']:>5.1f}% ${br['net']:>11,.2f} {br['max_dd']*100:>6.1f}% ${gs_per_month:>11,.2f}")
    
    # Verdict
    print("\n" + "=" * 100)
    print("VERDICT")
    print("=" * 100)
    if best_net > 2000 and br['pf'] > 1.3 and br['max_dd'] < 0.05:
        print(f"  RECOMMENDED: Gold/Silver looks viable as 3rd pair!")
        print(f"  Suggested config: {best_key}")
    elif best_net > 500 and br['pf'] > 1.1:
        print(f"  MARGINAL: Gold/Silver shows some edge but modest returns.")
        print(f"  Best config: {best_key}")
    elif best_net > 0:
        print(f"  WEAK: Barely profitable. Not recommended for live trading.")
    else:
        print(f"  NOT VIABLE: Gold/Silver loses money with current bot settings.")
    
    # Save results
    save_data = {k: {kk: vv for kk, vv in v.items() if kk != 'all_trades'} for k, v in results.items()}
    with open(Path(__file__).resolve().parent.parent / "Results" / "gold_silver_backtest.json", 'w') as f:
        json.dump(save_data, f, indent=2, default=str)
    
    print(f"\n  Completed in {time.time() - t0:.1f}s")
    print(f"  Results saved to Results/gold_silver_backtest.json")


if __name__ == "__main__":
    main()
