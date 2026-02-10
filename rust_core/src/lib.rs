//! SHF v5.6 Cointegration Pairs Trading Engine — PyO3 Module
//! ===========================================================
//!
//! Exports all classes and functions for Python consumption.
//! Build: cargo build --release
//! Output: target/release/shf_core.dll -> rename to shf_core.pyd

use pyo3::prelude::*;

pub mod math_kernel;

use math_kernel::*;

// ============================================================================
// EXECUTION CORE (optional ZMQ-based execution stub)
// ============================================================================

#[pyclass]
pub struct ExecutionCore {
    #[pyo3(get)]
    pub connected: bool,
}

#[pymethods]
impl ExecutionCore {
    #[new]
    pub fn new() -> Self {
        ExecutionCore { connected: false }
    }

    pub fn connect(&mut self, _host: &str, _port: u16) -> bool {
        self.connected = true;
        true
    }

    pub fn disconnect(&mut self) {
        self.connected = false;
    }
}

// ============================================================================
// MATH KERNEL WRAPPER (v4.0 Dragnet legacy compatibility)
// ============================================================================

#[pyclass]
pub struct MathKernel {
    normalizer: OnlineNormalizer,
    #[pyo3(get)]
    pub span: usize,
}

#[pymethods]
impl MathKernel {
    #[new]
    #[pyo3(signature = (span=100))]
    pub fn new(span: usize) -> Self {
        MathKernel {
            normalizer: OnlineNormalizer::new(span),
            span,
        }
    }

    pub fn update(&mut self, x: f64) -> (f64, f64, f64) {
        self.normalizer.update(x)
    }

    pub fn z_score(&self, x: f64) -> f64 {
        self.normalizer.z_score(x)
    }

    pub fn reset(&mut self) {
        self.normalizer.reset();
    }
}

// ============================================================================
// PYTHON MODULE REGISTRATION
// ============================================================================

#[pymodule]
fn shf_core(_py: Python<'_>, m: &PyModule) -> PyResult<()> {
    // === Classes ===
    m.add_class::<ExecutionCore>()?;
    m.add_class::<MathKernel>()?;
    m.add_class::<OnlineNormalizer>()?;
    m.add_class::<CointegrationEngine>()?;
    m.add_class::<KalmanSentinel>()?;
    m.add_class::<AKADRiskCalculator>()?;
    m.add_class::<CorrelationRiskMonitor>()?;
    m.add_class::<SpreadSignal>()?;
    m.add_class::<OUFitResult>()?;
    m.add_class::<DynamicSignalResult>()?;

    // === Standalone Functions ===
    m.add_function(wrap_pyfunction!(fit_robust_ou_process, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_rolling_hurst, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_prop_kelly, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_hard_stop_price, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_equilibrium_std, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_z_score, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_z_score_quantiles, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_hurst_quantiles, m)?)?;
    m.add_function(wrap_pyfunction!(generate_dynamic_signal, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_rolling_z_scores, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_rolling_hurst_series_py, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_correlation, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_correlation_matrix, m)?)?;

    // Module metadata
    m.add("__version__", "5.6.0")?;
    m.add("__description__", "SHF v5.6 Cointegration Pairs Trading Engine")?;

    Ok(())
}
