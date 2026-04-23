"""
Fail-the-build test: the live bot (src/live/v23_live.py) and the backtest
(Scripts/backtest_v23_final.py) MUST use identical sizer parameters.
Previous param drift between these two files was RISK #3 in the 2026-04 audit.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "src" / "live" / "v23_live.py"
BT   = ROOT / "Scripts" / "backtest_v23_final.py"


def _extract_sizer_params(src: str) -> dict:
    """Grabs the MertonGZSizerConfig(...) values using regex — no eval."""
    # capture the CONFIG block
    block = re.search(r"MertonGZSizerConfig\((.*?)\)", src, re.DOTALL)
    assert block, "MertonGZSizerConfig(...) not found"
    body = block.group(1)
    out = {}
    for key in ("base_risk_pct", "cap_mult", "gamma",
                "ewma_alpha", "warmup_trades", "dd_cap_pct"):
        m = re.search(rf"{key}\s*=\s*([A-Za-z0-9_.]+)", body)
        if m:
            out[key] = m.group(1)
    return out


def _extract_dataclass_params(src: str) -> dict:
    """Grabs the @dataclass V23LiveConfig field defaults."""
    out = {}
    for key in ("base_risk_pct", "cap_mult", "gamma",
                "ewma_alpha", "warmup_trades", "dd_cap_pct"):
        m = re.search(rf"{key}:\s*[a-zA-Z]+\s*=\s*([A-Za-z0-9_.]+)", src)
        if m:
            out[key] = m.group(1)
    return out


def test_live_and_backtest_sizer_match():
    live_src = LIVE.read_text(encoding="utf-8")
    bt_src   = BT.read_text(encoding="utf-8")

    # In live file, the *dataclass defaults* ARE the source of truth.
    live_params = _extract_dataclass_params(live_src)
    bt_params   = _extract_sizer_params(bt_src)

    # In the backtest file, `base_risk_pct=risk` where risk is a variable.
    # Replace that with the top-level RISK constant.
    risk_m = re.search(r"RISK\s*=\s*([0-9.]+)", bt_src)
    assert risk_m, "RISK constant not found in backtest_v23_final.py"
    if bt_params.get("base_risk_pct") == "risk":
        bt_params["base_risk_pct"] = risk_m.group(1)

    # Compare
    keys = ("base_risk_pct", "cap_mult", "gamma",
            "ewma_alpha", "warmup_trades", "dd_cap_pct")
    mismatch = [k for k in keys
                if k in live_params and k in bt_params
                and live_params[k] != bt_params[k]]

    assert not mismatch, (
        f"Sizer params drift between live and backtest: {mismatch}\n"
        f"  live     : { {k:live_params.get(k) for k in keys} }\n"
        f"  backtest : { {k:bt_params.get(k)   for k in keys} }"
    )
