"""
v31_migrate_sizer_state.py — In-place atomic migration of the Merton sizer's
saved state from base_risk_pct=0.00170 → 0.00185 (or any chosen value).

Why this script exists
----------------------
MertonGZSizer.from_state(strict_config=True) compares the persisted
`base_risk_pct` against the running engine's config and raises ValueError
on mismatch. Without this migration the bot would discard ~50+ live trades
of accumulated μ̂/σ̂² and fall back to a 15-trade cold-start warm-up.

The Merton statistics themselves (μ̂, σ̂², R-history) are stored in
**dimensionless R-units**, so they remain valid after the base-risk scale
change. Only the metadata field needs updating.

Safety rails
------------
1) Reads the existing JSON, validates schema and target field present.
2) Writes a timestamped backup alongside the original.
3) Atomic write: tmp file + os.replace (Windows-safe, never half-written).
4) Verifies the new file parses and contains the expected new value.
5) Refuses to run if state file is older than `MAX_AGE_DAYS` (stale).
6) Refuses to run if `_n_seen` is empty (nothing to preserve anyway —
   you may as well cold-start).

Usage
-----
    python Scripts/v31_migrate_sizer_state.py
    python Scripts/v31_migrate_sizer_state.py --new-base 0.00185
    python Scripts/v31_migrate_sizer_state.py --state-path "C:/PropBot/Results/v30_state/sizer_mertongz.json"
    python Scripts/v31_migrate_sizer_state.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

DEFAULT_PATH      = Path("Results/v30_state/sizer_mertongz.json")
DEFAULT_NEW_BASE  = 0.00185
EXPECTED_OLD_BASE = 0.00170
MAX_AGE_DAYS      = 14


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-path", type=Path, default=DEFAULT_PATH,
                    help=f"Path to sizer state JSON (default: {DEFAULT_PATH})")
    ap.add_argument("--new-base", type=float, default=DEFAULT_NEW_BASE,
                    help=f"New base_risk_pct (default: {DEFAULT_NEW_BASE})")
    ap.add_argument("--expected-old-base", type=float, default=EXPECTED_OLD_BASE,
                    help="Refuse to migrate unless current value matches this "
                         f"(default: {EXPECTED_OLD_BASE}). Pass --force to skip.")
    ap.add_argument("--force", action="store_true",
                    help="Skip the old-base sanity check and stale-file check")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would happen, write nothing")
    args = ap.parse_args()

    p = Path(args.state_path).expanduser().resolve()
    if not p.is_file():
        print(f"[ERROR] state file not found: {p}", file=sys.stderr)
        return 2

    age_days = (time.time() - p.stat().st_mtime) / 86400.0
    if age_days > MAX_AGE_DAYS and not args.force:
        print(f"[ERROR] state file is {age_days:.1f} days old (>{MAX_AGE_DAYS} d). "
              f"Use --force to override.", file=sys.stderr)
        return 3

    raw = p.read_text(encoding="utf-8")
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[ERROR] state file is not valid JSON: {e}", file=sys.stderr)
        return 4

    cfg = state.get("cfg") or state.get("config")
    if not isinstance(cfg, dict):
        print("[ERROR] no 'cfg' / 'config' block in state — schema unknown",
              file=sys.stderr)
        return 5

    if "base_risk_pct" not in cfg:
        print("[ERROR] cfg.base_risk_pct missing in state", file=sys.stderr)
        return 6

    n_seen_total = sum((state.get("_n_seen") or {}).values()) if isinstance(state.get("_n_seen"), dict) else 0
    cur_base = float(cfg["base_risk_pct"])
    new_base = float(args.new_base)

    print("=" * 72)
    print(f"  Merton sizer state migration")
    print(f"  ---------------------------")
    print(f"  File           : {p}")
    print(f"  Last modified  : {age_days:.2f} days ago")
    print(f"  Schema version : {state.get('schema', '(unknown)')}")
    print(f"  Trades seen    : {n_seen_total}")
    print(f"  Symbols w/ μ̂   : {len((state.get('_mu') or {}))}")
    print(f"  Current base   : {cur_base:.6f}  ({cur_base*100:.3f}%)")
    print(f"  Target base    : {new_base:.6f}  ({new_base*100:.3f}%)")
    print("=" * 72)

    if not args.force and abs(cur_base - args.expected_old_base) > 1e-9:
        print(f"[ABORT] cur base {cur_base} != expected {args.expected_old_base}. "
              f"Use --force to override.", file=sys.stderr)
        return 7

    if abs(cur_base - new_base) < 1e-12:
        print("[NOOP] state already at the target base_risk_pct.")
        return 0

    if n_seen_total == 0 and not args.force:
        print("[ABORT] no live trades in state (n_seen=0). Cold-start is fine; "
              "delete the file or pass --force.", file=sys.stderr)
        return 8

    # Mutate
    cfg["base_risk_pct"] = new_base

    # Backup (timestamped, idempotent)
    bk = p.with_suffix(p.suffix + f".bak.{int(time.time())}")
    if args.dry_run:
        print(f"[DRY-RUN] would write backup -> {bk.name}")
        print(f"[DRY-RUN] would update {p.name} cfg.base_risk_pct = {new_base}")
        return 0

    shutil.copy2(p, bk)
    print(f"[OK] backup saved -> {bk.name}")

    # Atomic write (write tmp + replace)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, p)

    # Verify
    re_state = json.loads(p.read_text(encoding="utf-8"))
    re_base = float(re_state["cfg"]["base_risk_pct"])
    if abs(re_base - new_base) > 1e-12:
        print(f"[FATAL] verify failed — file shows {re_base}", file=sys.stderr)
        return 9

    print(f"[OK] migration complete: cfg.base_risk_pct = {re_base:.6f}  "
          f"({re_base*100:.3f}%)")
    print(f"     n_seen preserved   = {n_seen_total}  (μ̂/σ̂²/R-history intact)")
    print(f"     backup retained    = {bk}")
    print()
    print("  Next: restart the bot. The Merton sizer will load this file at startup")
    print("  and resume scaling from your existing μ̂/σ̂² but with the new 0.185% unit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
