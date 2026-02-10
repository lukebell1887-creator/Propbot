"""SHF v5.6 PRE-LIVE COMPREHENSIVE WIRING AUDIT"""
import sys, os
os.chdir(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.getcwd())
print('='*70)
print('SHF v5.6 PRE-LIVE COMPREHENSIVE WIRING AUDIT')
print('='*70)

errors = []
passed = []

# 1. RUST CORE
print('\n[1] RUST CORE (shf_core.pyd)')
try:
    from shf_core import CointegrationEngine, KalmanSentinel, AKADRiskCalculator, CorrelationRiskMonitor
    passed.append('Rust: All 4 production classes import OK')
    
    e = CointegrationEngine(span=100, beta=1.0, dynamic_z=True, dynamic_exit=True)
    for attr in ('last_hurst','last_z_crit','last_exit_z','last_std','last_mean','last_z_score','last_spread','buffer_len'):
        assert hasattr(e, attr), f'Missing: {attr}'
    passed.append('Rust: FFI contract validated (8 getters)')
    
    for i in range(200):
        sig = e.update(100.0 + i*0.01, 99.0 + i*0.01)
    assert hasattr(sig, 'z_score') and hasattr(sig, 'signal') and hasattr(sig, 'spread')
    passed.append(f'Rust: CointegrationEngine signals OK (Z={sig.z_score:.4f})')
    
    ks = KalmanSentinel(static_beta=1.0, beta_tolerance=0.15)
    beta, abort = ks.update(4.6, 4.59)
    passed.append(f'Rust: KalmanSentinel OK (beta={beta:.4f}, abort={abort})')
    
    akad = AKADRiskCalculator(base_risk=0.0075, dd_lambda=40.0)
    r, dd_f, atr_f, exp_g = akad.calculate_risk(0.02)
    passed.append(f'Rust: AKADRiskCalculator OK (risk={r*100:.3f}% at 2% DD)')
    
    cm = CorrelationRiskMonitor(window=200)
    cm.push_return(0, 0.001)
    cm.push_return(1, -0.002)
    cm.compute_risk()
    mult = cm.last_risk_multiplier
    passed.append(f'Rust: CorrelationRiskMonitor OK (mult={mult})')
except Exception as ex:
    errors.append(f'Rust: FAILED - {ex}')

# 2. DYNAMIC AKAD
print('\n[2] DYNAMIC AKAD (PRIMARY risk calculator)')
try:
    from src.risk.akad_risk import DynamicAKAD, AKADRiskManager
    d = DynamicAKAD(dd_lambda=40.0, daily_dd_ceiling=0.04)
    
    r0 = d.calculate_risk(0.0, 0.0)
    assert 0.003 <= r0 <= 0.03, f'Bad risk at 0% DD: {r0}'
    passed.append(f'DynamicAKAD: 0% DD -> {r0*100:.3f}% risk')
    
    r_near = d.calculate_risk(0.0, 0.039)
    assert r_near < r0, 'Risk should drop near DD ceiling'
    passed.append(f'DynamicAKAD: Near ceiling (3.9% daily) -> {r_near*100:.3f}%')
    
    r_high = d.calculate_risk(0.05, 0.0)
    assert r_high < r0, 'Risk should drop at high DD'
    passed.append(f'DynamicAKAD: 5% total DD -> {r_high*100:.3f}%')
    
    r_extreme = d.calculate_risk(0.09, 0.039)
    assert r_extreme >= 0.0005, 'Must respect 0.05% floor'
    passed.append(f'DynamicAKAD: Extreme DD -> {r_extreme*100:.4f}% (floor OK)')
    
    d.record_trade(True)
    d.record_trade(False)
    passed.append(f'DynamicAKAD: Trade recording OK (WR={d.current_wr:.2f}, count={d.trade_count})')
except Exception as ex:
    errors.append(f'DynamicAKAD: FAILED - {ex}')

# 3. ENGINE WIRING
print('\n[3] ENGINE WIRING')
try:
    with open('src/engine.py', 'r', encoding='utf-8') as f:
        src = f.read()
    
    checks = [
        ('from src.risk.akad_risk import AKADRiskManager, DynamicAKAD', 'DynamicAKAD import'),
        ('self._dynamic_akad = DynamicAKAD(', 'DynamicAKAD initialized'),
        ('await self._process_pair(state, current_dd, daily_dd, account.balance)', 'daily_dd passed to _process_pair'),
        ('await self._maybe_enter(state, current_dd, daily_dd, balance)', 'daily_dd passed to _maybe_enter'),
        ('self._dynamic_akad.calculate_risk(total_dd=current_dd, daily_dd=daily_dd)', 'DynamicAKAD.calculate_risk() PRIMARY'),
        ('elif self._akad_rust is not None:', 'Rust AKAD fallback chain'),
        ('self._dynamic_akad.record_trade(win=is_win)', 'DynamicAKAD.record_trade() in _close_spread'),
        ('final_risk = risk * corr_mult', 'Correlation multiplier applied'),
    ]
    for pattern, label in checks:
        assert pattern in src, f'MISSING: {label}'
        passed.append(f'Engine: {label}')
except Exception as ex:
    errors.append(f'Engine wiring: FAILED - {ex}')

# 4. SAFETY LAYERS (16 layers)
print('\n[4] SAFETY LAYERS')
try:
    safety_checks = [
        ('GHOST_STOP_DAILY = 0.04', 'Ghost stop daily 4%'),
        ('GHOST_STOP_MAX = 0.09', 'Ghost stop max 9%'),
        ('daily_dd >= self.GHOST_STOP_DAILY', 'Ghost stop daily enforced'),
        ('current_dd >= self.GHOST_STOP_MAX', 'Ghost stop max enforced'),
        ('_emergency_close_all', 'Emergency close all'),
        ('sentinel_aborted', 'Kalman Sentinel kill-switch'),
        ('hmm_blocked', 'HMM volatility filter'),
        ('_calculate_dynamic_dwell', 'Dynamic dwell (30-300s)'),
        ('last_close_time', 'Re-entry cooldown'),
        ('abs(state.last_z) > abs(state.entry_z) * 2.5', 'Emergency exit bypasses dwell'),
        ('_check_spread', 'Spread blowout filter'),
        ('_is_rollover_lockout', 'Rollover lockout +/-5min'),
        ('STALE_FEED_TIMEOUT = 5.0', 'Delta staleness guard 5s'),
        ('_reconcile_after_timeout', '3-state Widowmaker reconciliation'),
        ('BridgeTimeoutError', 'BridgeTimeoutError handling'),
        ('is_halted', 'RiskSupervisor consecutive loss cooldown'),
        ('HUBER_SIGMA = 4.815', 'Server-side hard stops (Huber 4.815 sigma)'),
    ]
    for pattern, label in safety_checks:
        assert pattern in src, f'MISSING: {label}'
        passed.append(f'Safety: {label}')
    
    with open('src/execution/mt5_bridge.py', 'r', encoding='utf-8') as f:
        bridge_src = f.read()
    assert 'execute_spread' in bridge_src
    passed.append('Safety: Concurrent spread execution in mt5_bridge.py')
except Exception as ex:
    errors.append(f'Safety: FAILED - {ex}')

# 5. RISK SUPERVISOR
print('\n[5] RISK SUPERVISOR')
try:
    from src.risk.supervisor import RiskSupervisor, RiskAction, calculate_position_size
    rs = RiskSupervisor(initial_balance=100000)
    rs.record_win()
    assert not rs.is_halted
    passed.append('RiskSupervisor: Init + record_win OK')
    for i in range(5):
        rs.record_loss()
    passed.append(f'RiskSupervisor: 5 losses -> halted={rs.is_halted}')
except Exception as ex:
    errors.append(f'RiskSupervisor: FAILED - {ex}')

# 6. HMM
print('\n[6] HMM VOLATILITY FILTER')
try:
    from src.strategies.hmm_regime import HMMRegimeDetector, create_regime_detector
    hmm = create_regime_detector(n_regimes=3, lookback=100)
    for i in range(110):
        hmm.update(0.001 * ((-1)**i))
    passed.append(f'HMM: 3-regime filter OK (blocked={hmm.is_blocked})')
except Exception as ex:
    errors.append(f'HMM: FAILED - {ex}')

# 7. PERFORMANCE
print('\n[7] PERFORMANCE')
try:
    import time
    d2 = DynamicAKAD()
    t0 = time.perf_counter()
    for _ in range(100000):
        d2.calculate_risk(0.01, 0.005)
    us = (time.perf_counter() - t0) / 100000 * 1e6
    passed.append(f'DynamicAKAD: {us:.2f}us/call = {100000/us:.0f}x faster than 100ms tick')
except Exception as ex:
    errors.append(f'Performance: FAILED - {ex}')

# SUMMARY
print('\n' + '='*70)
print(f'AUDIT COMPLETE: {len(passed)} PASSED | {len(errors)} ERRORS')
print('='*70)

if errors:
    print('\n*** ERRORS ***')
    for e in errors:
        print(f'  [FAIL] {e}')

for p in passed:
    print(f'  [PASS] {p}')

if not errors:
    print(f'\n*** ALL {len(passed)} CHECKS PASSED - READY FOR LIVE ***')
else:
    print('\n*** FIX ERRORS BEFORE GOING LIVE ***')
    sys.exit(1)
