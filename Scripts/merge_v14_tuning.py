#!/usr/bin/env python3
"""Merge partial v14 optimizer tuning JSONs into a single file."""

from __future__ import annotations
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True,
                      help="Partial tuning JSON files to merge.")
    ap.add_argument("--out", type=Path,
                      default=ROOT / "Results" / "v14_per_symbol_tuning.json")
    a = ap.parse_args()

    merged = {"results": {}, "summary": {"kept": [], "dropped": [],
                                             "total_candidates": 0}}
    for path in a.inputs:
        with open(path) as f:
            d = json.load(f)
        for sym, r in d.get("results", {}).items():
            if sym in merged["results"]:
                print(f"WARN: {sym} already present — using the latest "
                       f"(from {path})")
            merged["results"][sym] = r
            status = r.get("status", "?")
            if status.startswith("KEEP"):
                if sym not in merged["summary"]["kept"]:
                    merged["summary"]["kept"].append(sym)
                if sym in merged["summary"]["dropped"]:
                    merged["summary"]["dropped"].remove(sym)
            else:
                if sym not in merged["summary"]["dropped"]:
                    merged["summary"]["dropped"].append(sym)

    merged["summary"]["total_candidates"] = len(merged["results"])

    with open(a.out, "w") as f:
        json.dump(merged, f, indent=2, default=str)

    print(f"\nMerged {len(a.inputs)} files into {a.out}")
    print(f"  KEPT:    {merged['summary']['kept']}")
    print(f"  DROPPED: {merged['summary']['dropped']}")


if __name__ == "__main__":
    main()
