//! SHF v5.6 Cointegration Pairs Trading Engine - Math Kernel
//! ============================================================
//!
//! All latency-critical math for the trading system:
//! - Welford O(1) online normalization (EMA variant)
//! - Hurst exponent via R/S analysis
//! - CointegrationEngine (spread -> Welford -> Hurst -> Dynamic Z entry/exit -> signal)
//! - KalmanSentinel (2x2 Kalman filter kill-switch, ~50ns)
//! - AKADRiskCalculator (Adaptive Kelly-ATR-Drawdown risk sizing, ~50ns)
//! - CorrelationRiskMonitor (cross-pair rolling Pearson correlation)
//! - Huber-robust OU process fitting (IRLS with MAD scale)

use pyo3::prelude::*;
use std::collections::VecDeque;

// ============================================================================
// CONSTANTS
// ============================================================================

const HUBER_K: f64 = 1.345;
const HUBER_MAX_ITER: usize = 50;
const HUBER_TOL: f64 = 1e-8;
const MAD_SCALE: f64 = 1.4826;
const MIN_VARIANCE: f64 = 1e-10;
const MIN_STD: f64 = 1e-8;

// ============================================================================
// SPREAD SIGNAL RESULT
// ============================================================================

#[pyclass]
#[derive(Clone, Debug)]
pub struct SpreadSignal {
    #[pyo3(get)]
    pub z_score: f64,
    #[pyo3(get)]
    pub signal: i32,
    #[pyo3(get)]
    pub spread: f64,
    #[pyo3(get)]
    pub cross_type: String,
}

#[pymethods]
impl SpreadSignal {
    fn __repr__(&self) -> String {
        format!(
            "SpreadSignal(z={:.4}, signal={}, spread={:.6}, cross={})",
            self.z_score, self.signal, self.spread, self.cross_type
        )
    }
}

// ============================================================================
// OU FIT RESULT
// ============================================================================

#[pyclass]
#[derive(Clone, Debug)]
pub struct OUFitResult {
    #[pyo3(get)]
    pub theta: f64,
    #[pyo3(get)]
    pub mu: f64,
    #[pyo3(get)]
    pub sigma: f64,
    #[pyo3(get)]
    pub half_life: f64,
    #[pyo3(get)]
    pub iterations: usize,
    #[pyo3(get)]
    pub outlier_pct: f64,
}

#[pymethods]
impl OUFitResult {
    fn __repr__(&self) -> String {
        format!(
            "OUFitResult(theta={:.4}, mu={:.6}, sigma={:.6}, hl={:.2}h, iters={}, outliers={:.1}%)",
            self.theta, self.mu, self.sigma, self.half_life, self.iterations, self.outlier_pct * 100.0
        )
    }
}

// ============================================================================
// DYNAMIC SIGNAL RESULT (Legacy Dragnet compatibility)
// ============================================================================

#[pyclass]
#[derive(Clone, Debug)]
pub struct DynamicSignalResult {
    #[pyo3(get)]
    pub z_score: f64,
    #[pyo3(get)]
    pub signal: i32,
    #[pyo3(get)]
    pub spread: f64,
    #[pyo3(get)]
    pub hurst: f64,
    #[pyo3(get)]
    pub z_crit: f64,
    #[pyo3(get)]
    pub exit_z: f64,
}

#[pymethods]
impl DynamicSignalResult {
    fn __repr__(&self) -> String {
        format!(
            "DynamicSignalResult(z={:.4}, sig={}, H={:.4}, z_crit={:.4}, exit_z={:.4})",
            self.z_score, self.signal, self.hurst, self.z_crit, self.exit_z
        )
    }
}

// ============================================================================
// ONLINE NORMALIZER (Welford EMA variant, O(1))
// ============================================================================

#[pyclass]
#[derive(Clone, Debug)]
pub struct OnlineNormalizer {
    span: usize,
    alpha: f64,
    mean: f64,
    m2: f64,
    count: usize,
}

#[pymethods]
impl OnlineNormalizer {
    #[new]
    #[pyo3(signature = (span=100))]
    pub fn new(span: usize) -> Self {
        let alpha = 2.0 / (span as f64 + 1.0);
        OnlineNormalizer { span, alpha, mean: 0.0, m2: 0.0, count: 0 }
    }

    pub fn update(&mut self, x: f64) -> (f64, f64, f64) {
        self.count += 1;
        if self.count == 1 {
            self.mean = x;
            self.m2 = 0.0;
            return (0.0, self.mean, MIN_VARIANCE);
        }
        let delta = x - self.mean;
        self.mean += self.alpha * delta;
        let delta2 = x - self.mean;
        self.m2 = (1.0 - self.alpha) * self.m2 + self.alpha * delta * delta2;
        let var = self.m2.max(MIN_VARIANCE);
        let z = (x - self.mean) / var.sqrt().max(MIN_STD);
        (z, self.mean, var)
    }

    pub fn z_score(&self, x: f64) -> f64 {
        let var = self.m2.max(MIN_VARIANCE);
        (x - self.mean) / var.sqrt().max(MIN_STD)
    }

    #[getter]
    pub fn get_mean(&self) -> f64 { self.mean }
    #[getter]
    pub fn get_variance(&self) -> f64 { self.m2.max(MIN_VARIANCE) }
    #[getter]
    pub fn get_std(&self) -> f64 { self.m2.max(MIN_VARIANCE).sqrt() }
    #[getter]
    pub fn get_count(&self) -> usize { self.count }

    pub fn reset(&mut self) {
        self.mean = 0.0;
        self.m2 = 0.0;
        self.count = 0;
    }
}

// ============================================================================
// HURST EXPONENT (R/S Analysis)
// ============================================================================

fn compute_hurst_rs(data: &[f64], window: usize) -> f64 {
    if data.len() < window {
        return 0.5;
    }
    let prices = &data[data.len() - window..];
    let returns: Vec<f64> = prices.windows(2).map(|w| w[1] - w[0]).collect();
    if returns.len() < 16 {
        return 0.5;
    }

    let mut window_sizes = Vec::new();
    let mut size = 8usize;
    while size <= returns.len() / 2 {
        window_sizes.push(size);
        size *= 2;
    }
    if window_sizes.len() < 2 {
        return 0.5;
    }

    let mut log_n_vec = Vec::new();
    let mut log_rs_vec = Vec::new();

    for &n in &window_sizes {
        let n_segments = returns.len() / n;
        if n_segments == 0 { continue; }
        let mut rs_values = Vec::new();
        for seg in 0..n_segments {
            let start = seg * n;
            let end = start + n;
            let segment = &returns[start..end];
            let mean: f64 = segment.iter().sum::<f64>() / n as f64;
            let var: f64 = segment.iter().map(|&x| (x - mean).powi(2)).sum::<f64>() / (n as f64 - 1.0);
            let std = var.sqrt();
            if std < 1e-10 { continue; }
            let mut cumsum = Vec::with_capacity(n);
            let mut running = 0.0;
            for &x in segment {
                running += x - mean;
                cumsum.push(running);
            }
            let max_cs = cumsum.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let min_cs = cumsum.iter().cloned().fold(f64::INFINITY, f64::min);
            let rs = (max_cs - min_cs) / std;
            if rs.is_finite() && rs > 0.0 {
                rs_values.push(rs);
            }
        }
        if !rs_values.is_empty() {
            let avg_rs: f64 = rs_values.iter().sum::<f64>() / rs_values.len() as f64;
            if avg_rs > 0.0 {
                log_n_vec.push((n as f64).ln());
                log_rs_vec.push(avg_rs.ln());
            }
        }
    }

    if log_n_vec.len() < 2 {
        return 0.5;
    }

    let n_pts = log_n_vec.len() as f64;
    let n_mean: f64 = log_n_vec.iter().sum::<f64>() / n_pts;
    let rs_mean: f64 = log_rs_vec.iter().sum::<f64>() / n_pts;
    let mut cov = 0.0;
    let mut var_n = 0.0;
    for i in 0..log_n_vec.len() {
        let dn = log_n_vec[i] - n_mean;
        let dr = log_rs_vec[i] - rs_mean;
        cov += dn * dr;
        var_n += dn * dn;
    }
    let h = if var_n > 0.0 { cov / var_n } else { 0.5 };
    h.max(0.0).min(1.0)
}

pub fn compute_rolling_hurst_series(data: &[f64], window: usize, step: usize) -> Vec<(usize, f64)> {
    let mut results = Vec::new();
    let mut i = window;
    while i <= data.len() {
        let h = compute_hurst_rs(&data[..i], window);
        results.push((i, h));
        i += step;
    }
    results
}

// ============================================================================
// COINTEGRATION ENGINE
// ============================================================================

#[pyclass]
pub struct CointegrationEngine {
    span: usize,
    alpha: f64,
    w_mean: f64,
    w_m2: f64,
    w_count: usize,
    beta: f64,
    entry_z_val: f64,
    exit_z_val: f64,
    z_base: f64,
    gamma: f64,
    hurst_window: usize,
    dynamic_z: bool,
    exit_z_base: f64,
    exit_gamma: f64,
    dynamic_exit: bool,
    spread_buffer: VecDeque<f64>,
    cached_hurst: f64,
    cached_z_crit: f64,
    cached_exit_z: f64,
    cached_z_score: f64,
    cached_spread: f64,
    cached_signal: i32,
}

#[pymethods]
impl CointegrationEngine {
    #[new]
    #[pyo3(signature = (
        span = 100,
        beta = 1.0,
        entry_z = 2.0,
        exit_z = 0.5,
        z_base = 2.0,
        gamma = 6.0,
        hurst_window = 512,
        dynamic_z = false,
        exit_z_base = 0.5,
        exit_gamma = 2.0,
        dynamic_exit = false
    ))]
    pub fn new(
        span: usize, beta: f64, entry_z: f64, exit_z: f64,
        z_base: f64, gamma: f64, hurst_window: usize, dynamic_z: bool,
        exit_z_base: f64, exit_gamma: f64, dynamic_exit: bool,
    ) -> Self {
        let alpha = 2.0 / (span as f64 + 1.0);
        CointegrationEngine {
            span, alpha, w_mean: 0.0, w_m2: 0.0, w_count: 0,
            beta, entry_z_val: entry_z, exit_z_val: exit_z,
            z_base, gamma, hurst_window, dynamic_z,
            exit_z_base, exit_gamma, dynamic_exit,
            spread_buffer: VecDeque::with_capacity(hurst_window + 256),
            cached_hurst: 0.5, cached_z_crit: entry_z, cached_exit_z: exit_z,
            cached_z_score: 0.0, cached_spread: 0.0, cached_signal: 0,
        }
    }

    pub fn update(&mut self, price_a: f64, price_b: f64) -> SpreadSignal {
        let log_a = if price_a > 0.0 { price_a.ln() } else { 0.0 };
        let log_b = if price_b > 0.0 { price_b.ln() } else { 0.0 };
        let spread = log_a - self.beta * log_b;
        self.cached_spread = spread;

        self.spread_buffer.push_back(spread);
        if self.spread_buffer.len() > self.hurst_window + 256 {
            self.spread_buffer.pop_front();
        }

        self.w_count += 1;
        let z: f64;
        if self.w_count == 1 {
            self.w_mean = spread;
            self.w_m2 = 0.0;
            z = 0.0;
        } else {
            let delta = spread - self.w_mean;
            self.w_mean += self.alpha * delta;
            let delta2 = spread - self.w_mean;
            self.w_m2 = (1.0 - self.alpha) * self.w_m2 + self.alpha * delta * delta2;
            let var = self.w_m2.max(MIN_VARIANCE);
            z = (spread - self.w_mean) / var.sqrt().max(MIN_STD);
        }
        self.cached_z_score = z;

        if (self.dynamic_z || self.dynamic_exit) && self.spread_buffer.len() >= self.hurst_window {
            let buf: Vec<f64> = self.spread_buffer.iter().cloned().collect();
            self.cached_hurst = compute_hurst_rs(&buf, self.hurst_window);
        }

        if self.dynamic_z {
            self.cached_z_crit = self.z_base * (1.0 + self.gamma * (self.cached_hurst - 0.5).max(0.0));
        } else {
            self.cached_z_crit = self.entry_z_val;
        }

        if self.dynamic_exit {
            let raw = self.exit_z_base * (1.0 + self.exit_gamma * (self.cached_hurst - 0.5));
            self.cached_exit_z = raw.max(0.1).min(1.0);
        } else {
            self.cached_exit_z = self.exit_z_val;
        }

        let mut signal = 0i32;
        let mut cross_type = String::from("none");
        if self.w_count >= 200 {
            if z > self.cached_z_crit {
                signal = -1;
                cross_type = String::from("short_entry");
            } else if z < -self.cached_z_crit {
                signal = 1;
                cross_type = String::from("long_entry");
            }
        }
        self.cached_signal = signal;

        SpreadSignal { z_score: z, signal, spread, cross_type }
    }

    #[getter]
    pub fn last_hurst(&self) -> f64 { self.cached_hurst }
    #[getter]
    pub fn last_z_crit(&self) -> f64 { self.cached_z_crit }
    #[getter]
    pub fn last_exit_z(&self) -> f64 { self.cached_exit_z }
    #[getter]
    pub fn entry_z(&self) -> f64 { self.entry_z_val }
    #[getter]
    pub fn exit_z(&self) -> f64 { self.exit_z_val }
    #[getter]
    pub fn dynamic_z_enabled(&self) -> bool { self.dynamic_z }
    #[getter]
    pub fn dynamic_exit_enabled(&self) -> bool { self.dynamic_exit }
    #[getter]
    pub fn last_z_score(&self) -> f64 { self.cached_z_score }
    #[getter]
    pub fn last_spread(&self) -> f64 { self.cached_spread }
    #[getter]
    pub fn last_std(&self) -> f64 { self.w_m2.max(MIN_VARIANCE).sqrt() }
    #[getter]
    pub fn last_mean(&self) -> f64 { self.w_mean }
    #[getter]
    pub fn buffer_len(&self) -> usize { self.spread_buffer.len() }

    pub fn reset(&mut self) {
        self.w_mean = 0.0; self.w_m2 = 0.0; self.w_count = 0;
        self.spread_buffer.clear();
        self.cached_hurst = 0.5;
        self.cached_z_crit = self.entry_z_val;
        self.cached_exit_z = self.exit_z_val;
        self.cached_z_score = 0.0; self.cached_spread = 0.0; self.cached_signal = 0;
    }
}

// ============================================================================
// KALMAN SENTINEL (2x2 Kalman filter kill-switch, ~50ns)
// ============================================================================

#[pyclass]
pub struct KalmanSentinel {
    theta: [f64; 2],
    p: [f64; 4],
    q_diag: f64,
    obs_noise: f64,
    static_beta: f64,
    beta_tolerance: f64,
}

#[pymethods]
impl KalmanSentinel {
    #[new]
    #[pyo3(signature = (static_beta=1.0, beta_tolerance=0.15, process_noise=0.0001, obs_noise=0.001))]
    pub fn new(static_beta: f64, beta_tolerance: f64, process_noise: f64, obs_noise: f64) -> Self {
        KalmanSentinel {
            theta: [0.0, 1.0],
            p: [1.0, 0.0, 0.0, 1.0],
            q_diag: process_noise,
            obs_noise,
            static_beta,
            beta_tolerance,
        }
    }

    pub fn update(&mut self, log_a: f64, log_b: f64) -> (f64, bool) {
        let f0 = 1.0;
        let f1 = log_b;

        let pp00 = self.p[0] + self.q_diag;
        let pp01 = self.p[1];
        let pp10 = self.p[2];
        let pp11 = self.p[3] + self.q_diag;

        let y_hat = f0 * self.theta[0] + f1 * self.theta[1];
        let innovation = log_a - y_hat;

        let s = f0 * (pp00 * f0 + pp01 * f1) + f1 * (pp10 * f0 + pp11 * f1) + self.obs_noise;

        let k0 = (pp00 * f0 + pp01 * f1) / s;
        let k1 = (pp10 * f0 + pp11 * f1) / s;

        self.theta[0] += k0 * innovation;
        self.theta[1] += k1 * innovation;

        self.p[0] = pp00 - k0 * k0 * s;
        self.p[1] = pp01 - k0 * k1 * s;
        self.p[2] = pp10 - k1 * k0 * s;
        self.p[3] = pp11 - k1 * k1 * s;

        let beta = self.theta[1];
        let deviation = (beta - self.static_beta).abs();
        let should_abort = deviation > self.beta_tolerance;

        (beta, should_abort)
    }

    #[getter]
    pub fn beta(&self) -> f64 { self.theta[1] }
    #[getter]
    pub fn alpha(&self) -> f64 { self.theta[0] }
    #[getter]
    pub fn get_static_beta(&self) -> f64 { self.static_beta }
    #[getter]
    pub fn get_beta_tolerance(&self) -> f64 { self.beta_tolerance }

    pub fn reset(&mut self) {
        self.theta = [0.0, 1.0];
        self.p = [1.0, 0.0, 0.0, 1.0];
    }
}

// ============================================================================
// AKAD RISK CALCULATOR (~50ns)
// ============================================================================

#[pyclass]
pub struct AKADRiskCalculator {
    base_risk: f64,
    dd_lambda: f64,
    fast_window: usize,
    slow_window: usize,
    baseline_expectancy: f64,
    trade_results: VecDeque<f64>,
    atr_history: VecDeque<f64>,
    current_atr: f64,
    historical_atr: f64,
    atr_count: usize,
}

#[pymethods]
impl AKADRiskCalculator {
    #[new]
    #[pyo3(signature = (base_risk=0.0075, dd_lambda=40.0, fast_window=15, slow_window=50, baseline_expectancy=0.1119))]
    pub fn new(base_risk: f64, dd_lambda: f64, fast_window: usize, slow_window: usize, baseline_expectancy: f64) -> Self {
        AKADRiskCalculator {
            base_risk, dd_lambda, fast_window, slow_window, baseline_expectancy,
            trade_results: VecDeque::with_capacity(slow_window + 16),
            atr_history: VecDeque::with_capacity(256),
            current_atr: 0.0, historical_atr: 0.0, atr_count: 0,
        }
    }

    pub fn record_trade(&mut self, r_multiple: f64) {
        self.trade_results.push_back(r_multiple);
        if self.trade_results.len() > self.slow_window + 16 {
            self.trade_results.pop_front();
        }
    }

    pub fn update_atr(&mut self, true_range: f64) {
        self.atr_count += 1;
        self.current_atr = true_range;
        self.atr_history.push_back(true_range);
        if self.atr_history.len() > 200 { self.atr_history.pop_front(); }
        if !self.atr_history.is_empty() {
            self.historical_atr = self.atr_history.iter().sum::<f64>() / self.atr_history.len() as f64;
        }
    }

    pub fn calculate_risk(&self, current_dd: f64) -> (f64, f64, f64, f64) {
        let dd_factor = (-self.dd_lambda * current_dd).exp();

        let atr_factor = if self.atr_count < 10 || self.historical_atr < MIN_STD {
            1.0
        } else {
            let vol_ratio = self.current_atr / self.historical_atr;
            if vol_ratio > 2.0 { 0.0 }
            else if vol_ratio > 1.5 { 0.5 }
            else if vol_ratio < 0.5 { 0.75 }
            else { (self.historical_atr / self.current_atr).min(1.0) }
        };

        let exp_gate = self.compute_expectancy_gate();
        let raw_risk = self.base_risk * dd_factor * atr_factor * exp_gate;
        let final_risk = raw_risk.max(0.0005);

        (final_risk, dd_factor, atr_factor, exp_gate)
    }

    #[getter]
    pub fn get_base_risk(&self) -> f64 { self.base_risk }
    #[getter]
    pub fn get_dd_lambda(&self) -> f64 { self.dd_lambda }
    #[getter]
    pub fn trade_count(&self) -> usize { self.trade_results.len() }
}

impl AKADRiskCalculator {
    fn compute_expectancy_gate(&self) -> f64 {
        let n = self.trade_results.len();
        if n < 5 { return 1.0; }

        let fast_start = if n > self.fast_window { n - self.fast_window } else { 0 };
        let fast_trades: Vec<f64> = self.trade_results.iter().skip(fast_start).cloned().collect();
        let fast_exp: f64 = fast_trades.iter().sum::<f64>() / fast_trades.len() as f64;

        let slow_start = if n > self.slow_window { n - self.slow_window } else { 0 };
        let slow_trades: Vec<f64> = self.trade_results.iter().skip(slow_start).cloned().collect();
        let slow_exp: f64 = slow_trades.iter().sum::<f64>() / slow_trades.len() as f64;

        if fast_exp < 0.0 && slow_exp < 0.0 { 0.0 }
        else if fast_exp < 0.0 { 0.75 }
        else if fast_exp < self.baseline_expectancy * 0.5 { 0.85 }
        else { 1.0 }
    }
}

// ============================================================================
// CORRELATION RISK MONITOR (v5.6)
// ============================================================================

#[pyclass]
pub struct CorrelationRiskMonitor {
    window: usize,
    n_pairs: usize,
    return_buffers: Vec<VecDeque<f64>>,
    cached_max_corr: f64,
    cached_risk_mult: f64,
}

#[pymethods]
impl CorrelationRiskMonitor {
    #[new]
    #[pyo3(signature = (n_pairs=3, window=200))]
    pub fn new(n_pairs: usize, window: usize) -> Self {
        let mut buffers = Vec::with_capacity(n_pairs);
        for _ in 0..n_pairs { buffers.push(VecDeque::with_capacity(window + 16)); }
        CorrelationRiskMonitor {
            window, n_pairs, return_buffers: buffers,
            cached_max_corr: 0.0, cached_risk_mult: 1.0,
        }
    }

    pub fn push_return(&mut self, pair_index: usize, spread_return: f64) {
        while self.return_buffers.len() <= pair_index {
            self.return_buffers.push(VecDeque::with_capacity(self.window + 16));
        }
        let buf = &mut self.return_buffers[pair_index];
        buf.push_back(spread_return);
        if buf.len() > self.window + 16 { buf.pop_front(); }
    }

    pub fn compute_risk(&mut self) -> (f64, f64) {
        let mut max_abs_corr: f64 = 0.0;
        for i in 0..self.return_buffers.len() {
            for j in (i + 1)..self.return_buffers.len() {
                let corr = self.pearson_corr(i, j);
                let abs_corr = corr.abs();
                if abs_corr > max_abs_corr { max_abs_corr = abs_corr; }
            }
        }
        let risk_mult = if max_abs_corr < 0.3 { 1.0 }
            else if max_abs_corr < 0.5 { 0.8 }
            else if max_abs_corr < 0.7 { 0.6 }
            else { 0.4 };
        self.cached_max_corr = max_abs_corr;
        self.cached_risk_mult = risk_mult;
        (max_abs_corr, risk_mult)
    }

    #[getter]
    pub fn last_max_corr(&self) -> f64 { self.cached_max_corr }
    #[getter]
    pub fn last_risk_multiplier(&self) -> f64 { self.cached_risk_mult }
}

impl CorrelationRiskMonitor {
    fn pearson_corr(&self, i: usize, j: usize) -> f64 {
        let buf_a = &self.return_buffers[i];
        let buf_b = &self.return_buffers[j];
        let n = buf_a.len().min(buf_b.len()).min(self.window);
        if n < 50 { return 0.0; }
        let a: Vec<f64> = buf_a.iter().rev().take(n).cloned().collect();
        let b: Vec<f64> = buf_b.iter().rev().take(n).cloned().collect();
        let a_mean: f64 = a.iter().sum::<f64>() / n as f64;
        let b_mean: f64 = b.iter().sum::<f64>() / n as f64;
        let mut cov = 0.0;
        let mut var_a = 0.0;
        let mut var_b = 0.0;
        for k in 0..n {
            let da = a[k] - a_mean;
            let db = b[k] - b_mean;
            cov += da * db;
            var_a += da * da;
            var_b += db * db;
        }
        let std_a = var_a.sqrt();
        let std_b = var_b.sqrt();
        if std_a < 1e-10 || std_b < 1e-10 { return 0.0; }
        (cov / (std_a * std_b)).max(-1.0).min(1.0)
    }
}

// ============================================================================
// HUBER-ROBUST OU FITTING
// ============================================================================

pub fn fit_robust_ou_irls(data: &[f64], dt: f64) -> OUFitResult {
    let n = data.len();
    if n < 10 {
        return OUFitResult { theta: 0.0, mu: 0.0, sigma: 0.0, half_life: f64::INFINITY, iterations: 0, outlier_pct: 0.0 };
    }
    let x_t: Vec<f64> = data[..n - 1].to_vec();
    let x_next: Vec<f64> = data[1..].to_vec();
    let m = x_t.len();
    let mut weights = vec![1.0f64; m];
    let mut alpha_hat = 0.0;
    let mut beta_hat = 1.0;
    let mut iterations = 0;
    let mut outlier_count = 0;

    for iter in 0..HUBER_MAX_ITER {
        iterations = iter + 1;
        let (a, b) = weighted_ls(&x_t, &x_next, &weights);
        if (a - alpha_hat).abs() < HUBER_TOL && (b - beta_hat).abs() < HUBER_TOL && iter > 0 {
            alpha_hat = a; beta_hat = b; break;
        }
        alpha_hat = a; beta_hat = b;
        let residuals: Vec<f64> = (0..m).map(|i| x_next[i] - (alpha_hat + beta_hat * x_t[i])).collect();
        let mut abs_resid: Vec<f64> = residuals.iter().map(|r| r.abs()).collect();
        abs_resid.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let median_abs = if abs_resid.len() % 2 == 0 {
            (abs_resid[abs_resid.len() / 2 - 1] + abs_resid[abs_resid.len() / 2]) / 2.0
        } else { abs_resid[abs_resid.len() / 2] };
        let scale = median_abs * MAD_SCALE;
        if scale < 1e-15 { break; }
        outlier_count = 0;
        for i in 0..m {
            let standardized = residuals[i].abs() / scale;
            if standardized <= HUBER_K { weights[i] = 1.0; }
            else { weights[i] = HUBER_K / standardized; outlier_count += 1; }
        }
    }

    let theta = ((1.0 - beta_hat) / dt).max(1e-10);
    let mu = if theta * dt > 1e-10 { alpha_hat / (theta * dt) } else { 0.0 };
    let residuals: Vec<f64> = (0..m).map(|i| x_next[i] - (alpha_hat + beta_hat * x_t[i])).collect();
    let mut w_sum = 0.0;
    let mut w_var = 0.0;
    for i in 0..m { w_sum += weights[i]; w_var += weights[i] * residuals[i] * residuals[i]; }
    let sigma = if w_sum > 0.0 { (w_var / (w_sum * dt)).sqrt() } else { 0.0 };
    let half_life = if theta > 1e-10 { (2.0f64).ln() / theta } else { f64::INFINITY };
    let outlier_pct = outlier_count as f64 / m as f64;
    OUFitResult { theta, mu, sigma, half_life, iterations, outlier_pct }
}

fn weighted_ls(x: &[f64], y: &[f64], w: &[f64]) -> (f64, f64) {
    let n = x.len();
    let (mut sw, mut swx, mut swy, mut swxx, mut swxy) = (0.0, 0.0, 0.0, 0.0, 0.0);
    for i in 0..n {
        sw += w[i]; swx += w[i]*x[i]; swy += w[i]*y[i];
        swxx += w[i]*x[i]*x[i]; swxy += w[i]*x[i]*y[i];
    }
    let denom = sw * swxx - swx * swx;
    if denom.abs() < 1e-30 { return (0.0, 1.0); }
    let alpha = (swy * swxx - swx * swxy) / denom;
    let beta = (sw * swxy - swx * swy) / denom;
    (alpha, beta)
}

// ============================================================================
// STANDALONE PYFUNCTION EXPORTS
// ============================================================================

#[pyfunction]
#[pyo3(signature = (data, dt=None))]
pub fn fit_robust_ou_process(data: Vec<f64>, dt: Option<f64>) -> OUFitResult {
    fit_robust_ou_irls(&data, dt.unwrap_or(1.0 / 60.0))
}

#[pyfunction]
#[pyo3(signature = (data, window=512, step=100))]
pub fn calculate_rolling_hurst(data: Vec<f64>, window: usize, step: usize) -> Vec<f64> {
    let mut result = vec![0.5; data.len()];
    let mut i = window;
    while i <= data.len() {
        result[i - 1] = compute_hurst_rs(&data[..i], window);
        i += step;
    }
    for i in 1..result.len() {
        if result[i] == 0.5 && i >= window { result[i] = result[i - 1]; }
    }
    result
}

#[pyfunction]
pub fn calculate_prop_kelly(win_rate: f64, avg_win: f64, avg_loss: f64) -> f64 {
    if avg_loss.abs() < 1e-10 { return 0.0; }
    let b = avg_win / avg_loss.abs();
    let q = 1.0 - win_rate;
    (win_rate - q / b).max(0.0) * 0.5
}

#[pyfunction]
#[pyo3(signature = (mu, eq_std, multiplier=4.815, is_long=true))]
pub fn calculate_hard_stop_price(mu: f64, eq_std: f64, multiplier: f64, is_long: bool) -> f64 {
    if is_long { (mu - multiplier * eq_std).exp() } else { (mu + multiplier * eq_std).exp() }
}

#[pyfunction]
pub fn calculate_equilibrium_std(sigma: f64, theta: f64) -> f64 {
    if theta <= 0.0 { f64::INFINITY } else { sigma / (2.0 * theta).sqrt() }
}

#[pyfunction]
#[pyo3(signature = (data, span=100))]
pub fn calculate_z_score(data: Vec<f64>, span: usize) -> Vec<f64> {
    let mut norm = OnlineNormalizer::new(span);
    data.iter().map(|&x| { let (z, _, _) = norm.update(x); z }).collect()
}

#[pyfunction]
pub fn calculate_z_score_quantiles(z_scores: Vec<f64>, quantiles: Vec<f64>) -> Vec<f64> {
    if z_scores.is_empty() { return vec![0.0; quantiles.len()]; }
    let mut sorted = z_scores;
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    quantiles.iter().map(|&q| {
        let idx = ((q * (sorted.len() as f64 - 1.0)).round() as usize).min(sorted.len() - 1);
        sorted[idx]
    }).collect()
}

#[pyfunction]
pub fn calculate_hurst_quantiles(hurst_values: Vec<f64>, quantiles: Vec<f64>) -> Vec<f64> {
    calculate_z_score_quantiles(hurst_values, quantiles)
}

#[pyfunction]
#[pyo3(signature = (price_a, price_b, spread_history, span=100, beta=1.0, z_base=2.0, gamma=6.0, hurst_window=512, exit_z_base=0.5, exit_gamma=2.0))]
pub fn generate_dynamic_signal(
    price_a: f64, price_b: f64, spread_history: Vec<f64>,
    span: usize, beta: f64, z_base: f64, gamma: f64, hurst_window: usize,
    exit_z_base: f64, exit_gamma: f64,
) -> DynamicSignalResult {
    let log_a = if price_a > 0.0 { price_a.ln() } else { 0.0 };
    let log_b = if price_b > 0.0 { price_b.ln() } else { 0.0 };
    let spread = log_a - beta * log_b;
    let mut all = spread_history; all.push(spread);
    let alpha = 2.0 / (span as f64 + 1.0);
    let mut mean = 0.0; let mut m2 = 0.0;
    for (i, &x) in all.iter().enumerate() {
        if i == 0 { mean = x; m2 = 0.0; }
        else { let d = x - mean; mean += alpha * d; let d2 = x - mean; m2 = (1.0 - alpha) * m2 + alpha * d * d2; }
    }
    let var = m2.max(MIN_VARIANCE);
    let z = (spread - mean) / var.sqrt().max(MIN_STD);
    let h = compute_hurst_rs(&all, hurst_window);
    let z_crit = z_base * (1.0 + gamma * (h - 0.5).max(0.0));
    let exit_z = (exit_z_base * (1.0 + exit_gamma * (h - 0.5))).max(0.1).min(1.0);
    let signal = if all.len() < 200 { 0 } else if z > z_crit { -1 } else if z < -z_crit { 1 } else { 0 };
    DynamicSignalResult { z_score: z, signal, spread, hurst: h, z_crit, exit_z }
}

#[pyfunction]
#[pyo3(signature = (data, span=100))]
pub fn calculate_rolling_z_scores(data: Vec<f64>, span: usize) -> Vec<f64> {
    calculate_z_score(data, span)
}

#[pyfunction]
#[pyo3(signature = (data, window=512, step=50))]
pub fn calculate_rolling_hurst_series_py(data: Vec<f64>, window: usize, step: usize) -> Vec<(usize, f64)> {
    compute_rolling_hurst_series(&data, window, step)
}

#[pyfunction]
pub fn calculate_correlation(a: Vec<f64>, b: Vec<f64>) -> f64 {
    let n = a.len().min(b.len());
    if n < 2 { return 0.0; }
    let a_mean: f64 = a[..n].iter().sum::<f64>() / n as f64;
    let b_mean: f64 = b[..n].iter().sum::<f64>() / n as f64;
    let mut cov = 0.0; let mut va = 0.0; let mut vb = 0.0;
    for i in 0..n {
        let da = a[i] - a_mean; let db = b[i] - b_mean;
        cov += da * db; va += da * da; vb += db * db;
    }
    if va.sqrt() < 1e-10 || vb.sqrt() < 1e-10 { return 0.0; }
    (cov / (va.sqrt() * vb.sqrt())).max(-1.0).min(1.0)
}

#[pyfunction]
pub fn calculate_correlation_matrix(series: Vec<Vec<f64>>) -> Vec<Vec<f64>> {
    let n = series.len();
    let mut matrix = vec![vec![0.0; n]; n];
    for i in 0..n {
        matrix[i][i] = 1.0;
        for j in (i + 1)..n {
            let corr = calculate_correlation(series[i].clone(), series[j].clone());
            matrix[i][j] = corr; matrix[j][i] = corr;
        }
    }
    matrix
}
