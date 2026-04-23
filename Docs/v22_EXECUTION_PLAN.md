# v22 Execution Plan — Full Institutional Build (Phase A + B + C)

> User directive (approved 2026-04-22): **"Full institutional build — Phase A + B + C (GARCH sizing, Bayesian priors, MC stress, pair-trade overlay), ~2-4 weeks"**
>
> This is the master tracker. Every subtask has an owner file, a test, and an exit gate.

---

## Phase A — "Lock in the free $4.5 k win" (target: deployable in ~1 day)

| # | Task | File(s) | Exit gate | Status |
|---|---|---|---|---|
| A1 | Create v22 backtest with **Lean+UK 5** symbol list | `Scripts/backtest_v22_lean_uk5.py` | Reproduces +$19,185 / 2.52 % DD from autopsy to within ±$300 | **IN PROGRESS** |
| A2 | Slippage-pad helper + sensitivity sweep | `src/execution/slippage.py`, `Scripts/audit_costs_v22.py` | PnL drop at 1-tick pad is < 10 %, still passes 4 % DD gate | TODO |
| A3 | Lot-size rounding to 5ers broker steps | `src/execution/lot_rounder.py` | All lots quantised to broker-valid step; PnL drop < 5 % | TODO |
| A4 | Weekend-flat rule in engine | patch `src/orb_engine_v20.py` → `v22` variant | No position held across Fri 16:45 NY | TODO |
| A5 | Hard daily-loss kill-switch @ 1.0 % | new `src/risk/kill_switch.py`, wired into live runner | Backtest: max daily loss ≤ 1.0 % | TODO |
| A6 | Max concurrent positions = 2 | patch engine `on_bar` → skip entries if `len(open)>=2` | Backtest: never > 2 positions open at once | TODO |
| A7 | Fresh JP225 + XAGUSD 2026 M1 download (optional) | `Scripts/download_5ers_3month.py` | New CSVs in `data/historical/` cover Jan 2026+ | OPTIONAL |
| A8 | Final Phase A report | `Docs/V22_PHASE_A_RESULTS.md` | Honest live-adjusted number table | TODO |

**Phase A success criterion:** Backtest run with all A1-A6 active on Lean+UK 5 shows **≥ $16,000 PnL @ ≤ 3 % DD @ PF ≥ 1.65 @ Sharpe ≥ 3.0** over the current 3-month window.

---

## Phase B — "PhD-grade edge upgrades" (target: 1 week after A)

| # | Task | File(s) | Exit gate | Status |
|---|---|---|---|---|
| B1 | HMM 2-state regime detector (trend / chop) | `src/regime/hmm2.py` + `tests/test_hmm2.py` | 10/10 unit tests; converges on synthetic 2-state series | TODO |
| B2 | Wire HMM gate into ORB engine (only trade if `P(trend)>0.6`) | `src/orb_engine_v22.py` | Reduces N trades by ~30 %, raises PF by ≥ 0.2 | TODO |
| B3 | Deflated Sharpe Ratio test (Bailey–López de Prado 2014) | `src/stats/deflated_sharpe.py` | DSR > 0.95 on Lean+UK 5 portfolio | TODO |
| B4 | Monte-Carlo bootstrap stress (N=1000 resamples) | `Scripts/mc_stress_v22.py` | 99 % VaR on max DD ≤ 4 % | TODO |
| B5 | Second-window walk-forward (Sep–Dec 2025 or earliest available) | `Scripts/walkforward_v22.py` | Lean+UK 5 still #1 on independent window; PBO < 30 % | TODO |
| B6 | Phase B results doc | `Docs/V22_PHASE_B_RESULTS.md` | Full comparison table, honest verdict | TODO |

**Phase B success criterion:** Lean+UK 5 + HMM filter delivers **≥ $21,000 PnL @ ≤ 2.5 % DD @ PF ≥ 1.9 @ Sharpe ≥ 3.5** and passes PBO < 30 % on the second window.

---

## Phase C — "Institutional-grade" (target: 2-4 weeks total from start)

| # | Task | File(s) | Exit gate | Status |
|---|---|---|---|---|
| C1 | GARCH(1,1) conditional-variance sizing module | `src/momentum/garch.py` (exists, needs integration), wire into sizer | Lot size uses σ_t+1 estimate; unit tests pass | TODO |
| C2 | Bayesian cross-symbol edge prior (Normal-inverse-gamma conjugate) | `src/sizing/bayes_prior.py` | New symbols deploy in ≤ 3 trades instead of 15 | TODO |
| C3 | Cross-asset co-integration pair-trade overlay (DE40 ↔ UK100) | `src/overlay/cointegration.py` | Pair-trade adds ≥ +10 % PnL on same window without raising DD | TODO |
| C4 | Full MC stress suite (20k paths, 99 % CI on DD, ruin probability) | `Scripts/mc_full_stress_v22.py` | Ruin (DD > 4 %) probability < 1 % | TODO |
| C5 | Live warmup + state-persistence for sizer (so reboot doesn't lose μ/σ²) | `src/live/sizer_persistence.py` | Restart test: state recovers within 1 trade | TODO |
| C6 | Phase C results + go-live guide | `Docs/V22_PHASE_C_RESULTS.md`, `Docs/V22_GO_LIVE.md` | All gates green; deployable | TODO |

**Phase C success criterion:** Full-stack v22 delivers **≥ $24,000 PnL @ ≤ 2.0 % DD @ PF ≥ 2.0 @ Sharpe ≥ 4.0 @ ruin prob < 1 %** over both in-sample and out-of-sample windows.

---

## Phase D — Optional moonshot (Reinforcement Learning session selector)

Skipped from this sprint unless all of Phase C passes its gates. Scoped at 1-2 additional months.

---

## Build principles (non-negotiable)

1. **No same-bar cheating, no wrong-side SL, no IS-only fits.** Every backtest must pass the v20 honesty test (broker-valid orders, NSB gate, OOS walk-forward).
2. **Every new module has a unit test file.** Target: ≥ 10 tests per module, 100 % pass before merge.
3. **Every phase ends with a written results doc.** No "I ran it and it worked" — show the numbers.
4. **No live deployment until all Phase A safety gates are live** (A3-A6): kill-switch, position cap, weekend-flat, lot-rounding.
5. **If any phase fails its exit gate, we stop and investigate — we do NOT move to the next phase.**
6. **Honest reporting always.** Red flags in bold. No hiding bad numbers.

---

## Session log

- **2026-04-22 18:30 BST** — Plan approved by user. Starting Phase A1.
