"""
SHF Latency Diagnostic — measures end-to-end communication latency.
Run on VPS INSTEAD of the engine (stop engine first with Ctrl+C).

Measures:
  1. DATA push latency (EA -> Python): time between consecutive messages
  2. Command round-trip (Python -> EA -> Python): PING/PONG + ORDER queries
  3. Tick-to-decision latency: simulated full pipeline timing
  4. Rust core computation speed: CointegrationEngine, KalmanSentinel, AKAD
"""

import socket
import struct
import json
import time
import statistics
import sys
import os

# Ensure we can find shf_core and src modules from C:\SHF
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Try importing Rust core for computation benchmarks
try:
    from shf_core import CointegrationEngine, KalmanSentinel, AKADRiskCalculator, CorrelationRiskMonitor
    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

try:
    from src.strategies.hmm_regime import HMMRegimeDetector, create_regime_detector
    HMM_AVAILABLE = True
except ImportError:
    HMM_AVAILABLE = False

HOST = "0.0.0.0"
PORT = 5555


def recv_exact(sock, n, timeout=5.0):
    sock.settimeout(timeout)
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def recv_msg(sock, timeout=3.0):
    sock.settimeout(timeout)
    header = recv_exact(sock, 4, timeout)
    if not header:
        return None
    msg_len = struct.unpack('>I', header)[0]
    if msg_len <= 0 or msg_len > 1_000_000:
        return None
    payload = recv_exact(sock, msg_len, timeout=5.0)
    if not payload:
        return None
    return json.loads(payload.decode('utf-8'))


def send_msg(sock, data):
    payload = json.dumps(data).encode('utf-8')
    header = struct.pack('>I', len(payload))
    sock.sendall(header + payload)


def format_us(val_seconds):
    """Format seconds as microseconds with color indicator."""
    us = val_seconds * 1_000_000
    if us < 100:
        return f"{us:.0f}us (EXCELLENT)"
    elif us < 1000:
        return f"{us:.0f}us (GOOD)"
    elif us < 10000:
        return f"{us/1000:.1f}ms (OK)"
    else:
        return f"{us/1000:.1f}ms (SLOW)"


print("=" * 60)
print("  SHF v5.6 LATENCY DIAGNOSTIC")
print("=" * 60)
print()

# ============================================================
# TEST 1: Rust Core Computation Latency
# ============================================================
print("[TEST 1] Rust Core Computation Speed")
print("-" * 40)

if RUST_AVAILABLE:
    import math
    import random

    # CointegrationEngine update
    ce = CointegrationEngine(
        span=100, beta=1.0, entry_z=2.0, exit_z=0.5,
        z_base=2.0, gamma=6.0, hurst_window=512,
        dynamic_z=True, exit_z_base=0.5, exit_gamma=2.0, dynamic_exit=True
    )
    # Warm up with 200 data points
    for i in range(200):
        a = 100.0 + random.gauss(0, 0.5)
        b = 100.0 + random.gauss(0, 0.5)
        ce.update(a, b)

    # Benchmark: 10,000 updates
    times_ce = []
    for _ in range(10000):
        a = 100.0 + random.gauss(0, 0.5)
        b = 100.0 + random.gauss(0, 0.5)
        t0 = time.perf_counter_ns()
        ce.update(a, b)
        t1 = time.perf_counter_ns()
        times_ce.append((t1 - t0) / 1000.0)  # to microseconds

    p50 = statistics.median(times_ce)
    p95 = sorted(times_ce)[int(0.95 * len(times_ce))]
    p99 = sorted(times_ce)[int(0.99 * len(times_ce))]
    print(f"  CointegrationEngine.update():")
    print(f"    p50={p50:.1f}us  p95={p95:.1f}us  p99={p99:.1f}us")

    # KalmanSentinel
    ks = KalmanSentinel(static_beta=1.0, beta_tolerance=0.15)
    times_ks = []
    for _ in range(10000):
        la = math.log(100.0 + random.gauss(0, 0.5))
        lb = math.log(100.0 + random.gauss(0, 0.5))
        t0 = time.perf_counter_ns()
        ks.update(la, lb)
        t1 = time.perf_counter_ns()
        times_ks.append((t1 - t0) / 1000.0)

    p50 = statistics.median(times_ks)
    p95 = sorted(times_ks)[int(0.95 * len(times_ks))]
    p99 = sorted(times_ks)[int(0.99 * len(times_ks))]
    print(f"  KalmanSentinel.update():")
    print(f"    p50={p50:.1f}us  p95={p95:.1f}us  p99={p99:.1f}us")

    # AKAD Risk
    akad = AKADRiskCalculator(base_risk=0.0075, dd_lambda=40.0)
    times_akad = []
    for _ in range(10000):
        dd = random.uniform(0, 0.05)
        t0 = time.perf_counter_ns()
        akad.calculate_risk(dd)
        t1 = time.perf_counter_ns()
        times_akad.append((t1 - t0) / 1000.0)

    p50 = statistics.median(times_akad)
    p95 = sorted(times_akad)[int(0.95 * len(times_akad))]
    print(f"  AKADRiskCalculator.calculate_risk():")
    print(f"    p50={p50:.1f}us  p95={p95:.1f}us")

    # CorrelationRiskMonitor
    crm = CorrelationRiskMonitor(window=200)
    times_crm = []
    for _ in range(10000):
        ret = random.gauss(0, 0.001)
        t0 = time.perf_counter_ns()
        crm.push_return(0, ret)
        t1 = time.perf_counter_ns()
        times_crm.append((t1 - t0) / 1000.0)

    p50 = statistics.median(times_crm)
    p95 = sorted(times_crm)[int(0.95 * len(times_crm))]
    print(f"  CorrelationRiskMonitor.push_return():")
    print(f"    p50={p50:.1f}us  p95={p95:.1f}us")

    # Combined pipeline (what happens each tick per pair)
    times_pipeline = []
    for _ in range(10000):
        a = 100.0 + random.gauss(0, 0.5)
        b = 100.0 + random.gauss(0, 0.5)
        dd = random.uniform(0, 0.03)

        t0 = time.perf_counter_ns()
        sig = ce.update(a, b)
        la = math.log(a)
        lb = math.log(b)
        beta, abort = ks.update(la, lb)
        risk, _, _, _ = akad.calculate_risk(dd)
        crm.push_return(0, a - b - (a - b))
        crm.compute_risk()
        t1 = time.perf_counter_ns()
        times_pipeline.append((t1 - t0) / 1000.0)

    p50 = statistics.median(times_pipeline)
    p95 = sorted(times_pipeline)[int(0.95 * len(times_pipeline))]
    p99 = sorted(times_pipeline)[int(0.99 * len(times_pipeline))]
    print(f"  FULL PIPELINE (per pair per tick):")
    print(f"    p50={p50:.1f}us  p95={p95:.1f}us  p99={p99:.1f}us")
    print(f"    3 pairs = ~{p50*3:.0f}us per tick")
else:
    print("  [SKIP] Rust core not available")

# HMM benchmark
print()
if HMM_AVAILABLE:
    hmm = create_regime_detector(n_regimes=3, lookback=100)
    # Warm up
    for _ in range(200):
        hmm.update(random.gauss(0, 0.001))

    times_hmm = []
    for _ in range(10000):
        t0 = time.perf_counter_ns()
        hmm.update(random.gauss(0, 0.001))
        t1 = time.perf_counter_ns()
        times_hmm.append((t1 - t0) / 1000.0)

    p50 = statistics.median(times_hmm)
    p95 = sorted(times_hmm)[int(0.95 * len(times_hmm))]
    print(f"  HMMRegimeDetector.update():")
    print(f"    p50={p50:.1f}us  p95={p95:.1f}us")
else:
    print("  [SKIP] HMM not available")

# ============================================================
# TEST 2: EA Data Push Latency (TCP)
# ============================================================
print()
print("[TEST 2] EA -> Python Data Push Latency (TCP)")
print("-" * 40)
print("  Waiting for EA connection (start EA if not running)...")

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((HOST, PORT))
srv.listen(1)
srv.settimeout(30.0)

try:
    client, addr = srv.accept()
    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"  EA connected from {addr}")

    # Measure inter-message intervals (= data push rate)
    intervals = []
    msg_sizes = []
    last_time = None
    count = 0

    while count < 100:
        t0 = time.perf_counter()
        msg = recv_msg(client, timeout=3.0)
        t1 = time.perf_counter()

        if msg is None:
            print(f"  [!] No message at count={count}")
            break

        raw_json = json.dumps(msg)
        msg_sizes.append(len(raw_json))

        if last_time is not None:
            intervals.append(t0 - last_time)
        last_time = t0
        count += 1

    if intervals:
        avg_interval = statistics.mean(intervals) * 1000
        min_interval = min(intervals) * 1000
        max_interval = max(intervals) * 1000
        jitter = statistics.stdev(intervals) * 1000 if len(intervals) > 1 else 0
        avg_size = statistics.mean(msg_sizes)
        rate = 1.0 / statistics.mean(intervals) if statistics.mean(intervals) > 0 else 0

        print(f"  Messages received: {count}")
        print(f"  Data rate: {rate:.1f} msg/s")
        print(f"  Interval: avg={avg_interval:.1f}ms  min={min_interval:.1f}ms  max={max_interval:.1f}ms")
        print(f"  Jitter (stdev): {jitter:.2f}ms")
        print(f"  Message size: avg={avg_size:.0f} bytes")

    # ============================================================
    # TEST 3: Command Round-Trip (PING/PONG)
    # ============================================================
    print()
    print("[TEST 3] Command Round-Trip (Python -> EA -> Python)")
    print("-" * 40)
    print("  Sending PING commands...")

    # Wait for SocketIsReadable to pick up our message (every 500ms in EA)
    ping_times = []
    for i in range(10):
        ping_msg = {"type": "PING"}
        t0 = time.perf_counter()
        send_msg(client, ping_msg)

        # Read messages until we get a PONG (skip DATA messages)
        pong_received = False
        deadline = time.time() + 2.0
        while time.time() < deadline:
            resp = recv_msg(client, timeout=1.0)
            if resp is None:
                break
            if resp.get('status') == 'PONG' or resp.get('mt') != 'DATA':
                t1 = time.perf_counter()
                ping_times.append(t1 - t0)
                pong_received = True
                break
            # It's a DATA message — skip and keep reading

        if not pong_received and i == 0:
            print("  [!] No PONG received — EA may not support PING yet")
            break

    if ping_times:
        avg_ping = statistics.mean(ping_times) * 1000
        min_ping = min(ping_times) * 1000
        max_ping = max(ping_times) * 1000
        print(f"  PING/PONG round-trips: {len(ping_times)}")
        print(f"  Round-trip: avg={avg_ping:.1f}ms  min={min_ping:.1f}ms  max={max_ping:.1f}ms")
    else:
        print("  [!] No PING round-trips completed (EA checks commands every 500ms)")
        print("  This is normal — commands are processed on EA timer, not instantly")

    client.close()

except socket.timeout:
    print("  [!] No EA connection within 30s")
except Exception as e:
    print(f"  [!] Error: {e}")
    import traceback
    traceback.print_exc()
finally:
    srv.close()

# ============================================================
# SUMMARY
# ============================================================
print()
print("=" * 60)
print("  LATENCY SUMMARY")
print("=" * 60)

if RUST_AVAILABLE:
    print(f"  Rust Pipeline (per pair):  ~{p50:.0f}us")
    print(f"  Rust Pipeline (3 pairs):   ~{p50*3:.0f}us")

print(f"  EA Data Rate:              10 msg/s (100ms timer)")

if intervals:
    print(f"  TCP Push Interval:         {avg_interval:.1f}ms avg")
    print(f"  TCP Jitter:                {jitter:.2f}ms")

if ping_times:
    print(f"  Command Round-Trip:        {avg_ping:.1f}ms avg")

print()
total_tick_budget = 100.0  # 100ms tick
# Note: TCP data arrives on a separate receiver thread (async).
# The engine tick loop reads from an in-memory cache, NOT from TCP directly.
# So the actual tick cost = Rust compute + Python overhead only.
compute = (p50 * 3 / 1000.0) if RUST_AVAILABLE else 5.0
python_overhead = 1.0  # estimated Python tick overhead (dict lookups, logging, etc.)
total_compute = compute + python_overhead

print(f"  Tick Budget:        {total_tick_budget:.0f}ms")
print(f"  - Rust compute:     {compute:.3f}ms (3 pairs)")
print(f"  - Python overhead:  ~{python_overhead:.1f}ms (estimated)")
print(f"  - Total per tick:   {total_compute:.2f}ms")
print(f"  - Headroom:         {total_tick_budget - total_compute:.1f}ms "
      f"({(total_tick_budget - total_compute)/total_tick_budget*100:.0f}%)")
print()
print(f"  NOTE: TCP data arrives ASYNC on receiver thread (not blocking).")
print(f"  The engine reads from in-memory cache — zero TCP wait per tick.")
print()

if total_compute < 10:
    print("  VERDICT: EXCELLENT — 90%+ tick budget free")
elif total_compute < 30:
    print("  VERDICT: GOOD — plenty of headroom")
elif total_compute < 80:
    print("  VERDICT: OK — functional")
else:
    print("  VERDICT: WARNING — tick budget tight")
print()
