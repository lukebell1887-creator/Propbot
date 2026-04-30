"""
_holding_time_audit.py — answer the user's question:

  "What is the average holding time per symbol over the 3-month backtest?"

Reads the v30 seed file used by the live bot and reports:
  - count of trades per symbol
  - mean / median / min / max holding time
  - distribution buckets (<5min, 5-30min, 30-120min, 2-8h, >8h)
  - whether US500 actually exits faster than the others
"""
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path


PATH = Path("Results/v30_fresh_trades.json")


def parse_dt(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(float(s))
    s = str(s).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        # last-ditch parsers
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                pass
    return None


def main():
    if not PATH.exists():
        print(f"FILE NOT FOUND: {PATH.resolve()}")
        return
    blob = json.loads(PATH.read_text())
    # File can be either list of trades, or {trades: [...]}, or {result: {trades: [...]}}
    if isinstance(blob, dict):
        if "trades" in blob:
            trades = blob["trades"]
        elif "result" in blob and isinstance(blob["result"], dict):
            trades = blob["result"].get("trades", [])
        else:
            trades = []
    else:
        trades = blob

    if not trades:
        print("No trades found in file.")
        return

    # Find the right keys (probe first trade)
    sample = trades[0]
    print("Sample keys:", sorted(sample.keys()))
    print()

    # Detect entry/exit time keys
    entry_keys = ["entry_time", "entry_at", "open_time", "open_at", "entry_dt", "t_entry", "t_in"]
    exit_keys = ["exit_time", "exit_at", "close_time", "close_at", "exit_dt", "t_exit", "t_out"]
    sym_keys = ["symbol", "sym", "ticker"]

    def find_key(d, candidates):
        for k in candidates:
            if k in d:
                return k
        return None

    e_key = find_key(sample, entry_keys)
    x_key = find_key(sample, exit_keys)
    s_key = find_key(sample, sym_keys)

    print(f"Detected keys -> entry='{e_key}', exit='{x_key}', symbol='{s_key}'")
    print()

    if not (e_key and x_key and s_key):
        print("Could not find required keys. First trade:")
        print(json.dumps(sample, indent=2, default=str)[:1500])
        return

    by_sym = defaultdict(list)  # symbol -> list of hold_minutes
    skipped = 0
    for t in trades:
        sym = t.get(s_key)
        e = parse_dt(t.get(e_key))
        x = parse_dt(t.get(x_key))
        if not (sym and e and x):
            skipped += 1
            continue
        delta_min = (x - e).total_seconds() / 60.0
        if delta_min < 0 or delta_min > 60 * 24 * 7:  # sanity: < a week
            skipped += 1
            continue
        by_sym[sym].append(delta_min)

    print(f"Total trades: {len(trades)}    skipped (bad/missing times): {skipped}")
    print()

    # Sort symbols
    syms = sorted(by_sym.keys())

    # Header
    print(f"{'SYMBOL':<10} {'COUNT':>6} {'MEAN':>8} {'MEDIAN':>8} {'MIN':>7} {'MAX':>7}   "
          f"{'<5m':>5} {'5-30m':>6} {'30m-2h':>7} {'2-8h':>5} {'>8h':>5}")
    print("-" * 100)

    for sym in syms:
        hs = by_sym[sym]
        if not hs:
            continue
        m = statistics.mean(hs)
        med = statistics.median(hs)
        lo = min(hs)
        hi = max(hs)
        b1 = sum(1 for v in hs if v < 5)
        b2 = sum(1 for v in hs if 5 <= v < 30)
        b3 = sum(1 for v in hs if 30 <= v < 120)
        b4 = sum(1 for v in hs if 120 <= v < 480)
        b5 = sum(1 for v in hs if v >= 480)
        print(f"{sym:<10} {len(hs):>6} {m:>7.1f}m {med:>7.1f}m {lo:>6.1f}m {hi:>6.1f}m   "
              f"{b1:>5} {b2:>6} {b3:>7} {b4:>5} {b5:>5}")

    # Print sorted ranking by mean (ascending = fastest)
    print()
    print("RANKING (fastest → slowest by MEAN hold time):")
    ranked = sorted(syms, key=lambda s: statistics.mean(by_sym[s]) if by_sym[s] else 0)
    for i, sym in enumerate(ranked, 1):
        m = statistics.mean(by_sym[sym])
        print(f"  {i}. {sym:<8} mean = {m:6.1f} min")

    # Comment on the original claim
    print()
    print("ORIGINAL CLAIM CHECK:")
    print("  Claim: 'US500 works by getting in and out quickly'")
    if "US500" in by_sym:
        us500_mean = statistics.mean(by_sym["US500"])
        others_mean = [statistics.mean(by_sym[s]) for s in syms if s != "US500" and by_sym[s]]
        avg_others = statistics.mean(others_mean) if others_mean else 0
        if us500_mean < avg_others:
            print(f"  ✓ TRUE: US500 mean {us500_mean:.1f}m < avg of others {avg_others:.1f}m")
        else:
            print(f"  ✗ FALSE: US500 mean {us500_mean:.1f}m >= avg of others {avg_others:.1f}m")
    else:
        print("  US500 not in the data set!")


if __name__ == "__main__":
    main()
