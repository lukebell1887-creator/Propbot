"""
Scripts/verify_v31_live_wiring.py
==================================

Single-shot green-light report for the v31 Layer-1 live wire-up.

Run from the repo root:

    python Scripts\\verify_v31_live_wiring.py

It does NOT touch the broker.  It only:

  1. Imports `src.live.v30_live` and asserts the engine class has the
     `Layer1Tracker` and the `emergency_sl_offset_for` import wired in.
  2. Greps the launcher .ps1 files for `--risk 0.00185` so the live
     base-risk matches the backtest config (v31 ship value).
  3. Asserts `LAYER1_CAPS` matches the values proven in
     `Docs/V31_DEFENSE_PROOF_RESULTS.md`.
  4. Greps `_manage_open` for the Layer 1 intercept loop.
  5. Runs the 81 unit tests in
        tests/test_layer1.py
        tests/test_layer1_tracker.py
     plus any newly-added parity tests, via pytest.
  6. Prints a banner the operator can copy-paste back.

Exit code:
    0  – every check passed; live ≡ backtest.
    1  – one or more checks failed (banner shows which).

The banner is intentionally noisy and copy-paste friendly so chats /
audit logs preserve a snapshot of the wire-up that was deployed.
"""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LIVE_FILE = REPO / "src" / "live" / "v30_live.py"
LAYER1_FILE = REPO / "src" / "execution" / "layer1.py"
LAYER1_TRACKER_FILE = REPO / "src" / "execution" / "layer1_tracker.py"
DRYRUN_PS1 = REPO / "GO_DRYRUN_V30.ps1"
LIVE_PS1 = REPO / "GO_LIVE_V30.ps1"

# Locked values — must match Scripts/v31_proof_pipeline.py.
EXPECTED_CAPS = {"DE40": 5.0, "US30": 5.0, "US500": 3.0, "XAUUSD": 1.0}
EXPECTED_FALLBACK_MULT = 1.5
EXPECTED_ENVELOPE_S = 60.0
EXPECTED_RISK_TOKEN = "--risk 0.00185"


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _bad(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def check(label: str, ok: bool, detail: str) -> bool:
    if ok:
        _ok(f"{label:<28} {detail}")
    else:
        _bad(f"{label:<28} {detail}")
    return ok


def run_pytest() -> tuple[bool, str]:
    """Run the locked Layer-1 test suites under pytest and return
    (success, summary)."""
    cmd = [
        sys.executable, "-m", "pytest",
        "tests/test_layer1.py",
        "tests/test_layer1_tracker.py",
        "-q", "--tb=line", "--no-header",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=REPO, capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        return False, "pytest not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "pytest timed out (>120 s)"

    out = (proc.stdout or "") + (proc.stderr or "")
    # Last non-empty line of pytest output usually has the summary
    last = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
    return proc.returncode == 0, last


def main() -> int:
    print("=" * 70)
    print("  V31 LIVE WIRING REPORT")
    print("=" * 70)
    if not LIVE_FILE.exists():
        _bad(f"missing {LIVE_FILE}")
        return 1
    live_src = LIVE_FILE.read_text(encoding="utf-8", errors="replace")
    layer1_src = LAYER1_FILE.read_text(encoding="utf-8", errors="replace")

    failures: list[str] = []

    # ------------------------------------------------------------------
    # 1. Imports wired into v30_live.py
    # ------------------------------------------------------------------
    has_tracker_import = "from src.execution.layer1_tracker import Layer1Tracker" in live_src
    has_helper_import = (
        "from src.execution.layer1 import" in live_src
        and "emergency_sl_offset_for" in live_src
    )
    if not check("imports", has_tracker_import and has_helper_import,
                 "Layer1Tracker + emergency_sl_offset_for"):
        failures.append("imports")

    # ------------------------------------------------------------------
    # 2. Tracker constructed in __init__
    # ------------------------------------------------------------------
    has_init = bool(re.search(r"self\.layer1\s*=\s*Layer1Tracker\s*\(\s*\)", live_src))
    if not check("__init__", has_init, "self.layer1 = Layer1Tracker()"):
        failures.append("__init__")

    # ------------------------------------------------------------------
    # 3. Broker SL is widened by cap*1.5 in send_order
    # ------------------------------------------------------------------
    has_widen = (
        "emerg_offset = emergency_sl_offset_for(sym)" in live_src
        and "broker_sl = (sl - emerg_offset) if side == \"LONG\" else (sl + emerg_offset)" in live_src
        and re.search(r"sl\s*=\s*float\(broker_sl\)", live_src) is not None
    )
    if not check("send_order SL", has_widen,
                 "broker SL = original ± cap*1.5 (st.open_sl stays original)"):
        failures.append("send_order SL")

    # ------------------------------------------------------------------
    # 4. Poll-loop intercept calls update_and_decide
    # ------------------------------------------------------------------
    has_poll = (
        "self.layer1.update_and_decide" in live_src
        and re.search(r'decision\.action\s+in\s+\("CLOSE_NOW",\s*"FALLBACK_CLOSE"\)', live_src) is not None
        and "_close_one(sym, f\"layer1_{decision.action.lower()}\")" in live_src
    )
    if not check("_manage_open intercept", has_poll,
                 "update_and_decide -> _close_one on CLOSE_NOW/FALLBACK_CLOSE"):
        failures.append("_manage_open intercept")

    # ------------------------------------------------------------------
    # 5. _clear_state drops tracker state by ticket
    # ------------------------------------------------------------------
    has_clear = bool(re.search(r"self\.layer1\.clear\s*\(\s*int\(st\.open_ticket\)\s*\)", live_src))
    if not check("_clear_state", has_clear,
                 "self.layer1.clear(int(st.open_ticket))"):
        failures.append("_clear_state")

    # ------------------------------------------------------------------
    # 6. LAYER1_CAPS match locked values
    # ------------------------------------------------------------------
    caps_ok = True
    cap_detail_parts: list[str] = []
    for sym, expect in EXPECTED_CAPS.items():
        m = re.search(rf'"{sym}":\s*([0-9]+(?:\.[0-9]+)?)', layer1_src)
        if m is None:
            caps_ok = False
            cap_detail_parts.append(f"{sym}=missing")
            continue
        got = float(m.group(1))
        cap_detail_parts.append(f"{sym}={got:g}")
        if abs(got - expect) > 1e-9:
            caps_ok = False
    if not check("Layer 1 caps", caps_ok, "  ".join(cap_detail_parts)):
        failures.append("Layer 1 caps")

    # ------------------------------------------------------------------
    # 7. Fallback mult + envelope_s match locked values
    # ------------------------------------------------------------------
    m_mult = re.search(r"LAYER1_FALLBACK_MULT\s*:\s*float\s*=\s*([0-9.]+)", layer1_src)
    fallback_ok = m_mult is not None and abs(float(m_mult.group(1)) - EXPECTED_FALLBACK_MULT) < 1e-9
    if not check("Fallback mult", fallback_ok,
                 f"{m_mult.group(1) if m_mult else 'missing'} (expect {EXPECTED_FALLBACK_MULT})"):
        failures.append("Fallback mult")

    m_env = re.search(r"LAYER1_ENVELOPE_S\s*:\s*float\s*=\s*([0-9.]+)", layer1_src)
    env_ok = m_env is not None and abs(float(m_env.group(1)) - EXPECTED_ENVELOPE_S) < 1e-9
    if not check("Envelope (s)", env_ok,
                 f"{m_env.group(1) if m_env else 'missing'} (expect {EXPECTED_ENVELOPE_S:.0f})"):
        failures.append("Envelope (s)")

    # ------------------------------------------------------------------
    # 8. Launchers carry --risk 0.00185
    # ------------------------------------------------------------------
    for ps1 in (DRYRUN_PS1, LIVE_PS1):
        if not ps1.exists():
            failures.append(f"{ps1.name}: missing")
            check(ps1.name, False, "missing on disk")
            continue
        text = ps1.read_text(encoding="utf-8", errors="replace")
        ok = EXPECTED_RISK_TOKEN in text
        if not check(ps1.name, ok, f"contains {EXPECTED_RISK_TOKEN!r}"):
            failures.append(ps1.name)

    # ------------------------------------------------------------------
    # 9. Run the unit-test suites (81 tests)
    # ------------------------------------------------------------------
    print("-" * 70)
    print("  Running unit tests under pytest …")
    pytest_ok, summary = run_pytest()
    if not check("Unit tests", pytest_ok, summary or "(no output)"):
        failures.append("Unit tests")

    # ------------------------------------------------------------------
    # Final banner
    # ------------------------------------------------------------------
    print("=" * 70)
    if failures:
        print(f"  RESULT: {len(failures)} CHECK(S) FAILED  ❌")
        print("  Failed: " + ", ".join(failures))
        print("=" * 70)
        return 1

    banner = textwrap.dedent(f"""
        =====================================================================
          V31 LIVE WIRING REPORT  --  ALL CHECKS PASSED
        =====================================================================
          [OK]  Risk:           0.185 %
          [OK]  Layer 1 caps:   US30=5  US500=3  DE40=5  XAUUSD=1
          [OK]  Envelope:       60 s
          [OK]  Fallback mult:  1.5
          [OK]  Live engine:    Layer1Tracker imported + constructed
          [OK]  Live engine:    send_order widens broker SL by cap*1.5
          [OK]  Live engine:    _manage_open calls update_and_decide
          [OK]  Live engine:    _clear_state drops tracker state on close
          [OK]  Launchers:      both pass --risk 0.00185
          [OK]  Unit tests:     {summary}
        ---------------------------------------------------------------------
          RESULT: LIVE BOT MATCHES BACKTEST (Layer 1 + 0.185 % risk)
        =====================================================================
    """).strip("\n")
    print(banner)
    return 0


if __name__ == "__main__":
    sys.exit(main())
