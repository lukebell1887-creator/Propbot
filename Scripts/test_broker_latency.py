"""
VPS-to-Broker Network Latency Check
Finds MT5's broker connection and measures network ping.
"""
import subprocess
import re
import statistics
import time
import socket
import struct
import json

print("=" * 60)
print("  VPS-to-BROKER Network Latency Check")
print("=" * 60)
print()

# Method 1: Find MT5's active broker connections via netstat
print("[1] Finding MT5 broker connections...")
try:
    result = subprocess.run(
        ['netstat', '-n', '-o'],
        capture_output=True, text=True, timeout=10
    )
    
    # Find connections on common MT5 broker ports (443, 80, 443x)
    broker_ips = set()
    lines = result.stdout.split('\n')
    for line in lines:
        # Look for ESTABLISHED TCP connections (not localhost)
        if 'ESTABLISHED' in line and '127.0.0.1' not in line.split()[2] if len(line.split()) >= 3 else False:
            parts = line.split()
            if len(parts) >= 3:
                remote = parts[2]
                # Extract IP:port
                if ':' in remote:
                    ip = remote.rsplit(':', 1)[0]
                    port = remote.rsplit(':', 1)[1]
                    # Skip local/private IPs and common non-broker ports
                    if not ip.startswith('10.') and not ip.startswith('192.168.') and not ip.startswith('0.'):
                        try:
                            port_num = int(port)
                            # MT5 typically connects on 443 or high ports
                            if port_num == 443 or port_num > 1024:
                                broker_ips.add(ip)
                        except:
                            pass
    
    if broker_ips:
        print(f"  Found {len(broker_ips)} external connections")
        for ip in broker_ips:
            print(f"    -> {ip}")
    else:
        print("  No external connections found. MT5 might not be connected.")

except Exception as e:
    print(f"  Error: {e}")
    broker_ips = set()

# Method 2: Ping each found IP
print()
print("[2] Pinging broker servers...")
print("-" * 40)

ping_results = {}
for ip in broker_ips:
    try:
        result = subprocess.run(
            ['ping', '-n', '10', '-w', '2000', ip],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout
        
        # Parse ping results
        times = re.findall(r'time[=<](\d+)ms', output)
        if times:
            ping_ms = [int(t) for t in times]
            avg = statistics.mean(ping_ms)
            mn = min(ping_ms)
            mx = max(ping_ms)
            jitter = statistics.stdev(ping_ms) if len(ping_ms) > 1 else 0
            lost_match = re.search(r'(\d+)% loss', output)
            loss = lost_match.group(1) if lost_match else '?'
            
            ping_results[ip] = {
                'avg': avg, 'min': mn, 'max': mx, 
                'jitter': jitter, 'loss': loss, 'count': len(ping_ms)
            }
            
            print(f"  {ip}:")
            print(f"    Ping: avg={avg:.0f}ms  min={mn}ms  max={mx}ms")
            print(f"    Jitter: {jitter:.1f}ms  Loss: {loss}%")
            
            if avg < 1:
                print(f"    Rating: EXCELLENT (co-located or same DC)")
            elif avg < 5:
                print(f"    Rating: EXCELLENT (same city)")
            elif avg < 20:
                print(f"    Rating: GOOD (nearby region)")
            elif avg < 50:
                print(f"    Rating: OK (same continent)")
            elif avg < 100:
                print(f"    Rating: FAIR (cross-continent)")
            else:
                print(f"    Rating: SLOW (consider closer VPS)")
        else:
            print(f"  {ip}: No response (firewall may block ICMP)")
            
    except subprocess.TimeoutExpired:
        print(f"  {ip}: Timed out")
    except Exception as e:
        print(f"  {ip}: Error — {e}")

# Method 3: Also try TCP connect latency (works even if ICMP is blocked)
print()
print("[3] TCP Connection Latency (more reliable than ping)...")
print("-" * 40)

for ip in broker_ips:
    tcp_times = []
    for _ in range(5):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            t0 = time.perf_counter()
            s.connect((ip, 443))
            t1 = time.perf_counter()
            tcp_times.append((t1 - t0) * 1000)
            s.close()
        except:
            pass
        time.sleep(0.1)
    
    if tcp_times:
        avg = statistics.mean(tcp_times)
        mn = min(tcp_times)
        print(f"  {ip} (TCP:443):")
        print(f"    Connect: avg={avg:.1f}ms  min={mn:.1f}ms")
        
        if avg < 2:
            print(f"    Rating: EXCELLENT")
        elif avg < 10:
            print(f"    Rating: GOOD")
        elif avg < 30:
            print(f"    Rating: OK")
        else:
            print(f"    Rating: SLOW")

# Also connect to EA to get broker server name
print()
print("[4] Checking broker name from EA...")
print("-" * 40)

try:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.settimeout(5.0)
    srv.bind(("0.0.0.0", 5555))
    srv.listen(1)
    client, _ = srv.accept()
    client.settimeout(3.0)
    
    # Read 4-byte header
    header = b''
    while len(header) < 4:
        header += client.recv(4 - len(header))
    msg_len = struct.unpack('>I', header)[0]
    payload = b''
    while len(payload) < msg_len:
        payload += client.recv(msg_len - len(payload))
    
    msg = json.loads(payload.decode('utf-8'))
    account = msg.get('a', {})
    server = account.get('server', 'Unknown')
    print(f"  Broker Server: {server}")
    
    client.close()
    srv.close()
except Exception as e:
    print(f"  Could not get broker info: {e}")
    print(f"  (This is normal if the engine is already running)")

# Summary
print()
print("=" * 60)
print("  FULL LATENCY CHAIN SUMMARY")
print("=" * 60)
print()
print("  Market Tick Flow:")
print("    Broker Server  --[network]-->  MT5 EA  --[TCP:localhost]-->  Python Engine")
print()

best_ping = None
for ip, data in ping_results.items():
    if best_ping is None or data['avg'] < best_ping:
        best_ping = data['avg']

if best_ping is not None:
    print(f"  VPS <-> Broker:      ~{best_ping:.0f}ms (network)")
else:
    print(f"  VPS <-> Broker:      (ICMP blocked — check TCP latency above)")

print(f"  EA <-> Python:       <1ms (localhost TCP)")
print(f"  Python Compute:      ~0.5ms (Rust pipeline × 3 pairs)")
print(f"  ───────────────────────────")

if best_ping is not None:
    total = best_ping + 1 + 0.5
    print(f"  TOTAL tick-to-decision: ~{total:.0f}ms")
    print()
    
    if total < 10:
        print("  VERDICT: EXCELLENT — institutional-grade latency")
    elif total < 30:
        print("  VERDICT: GOOD — competitive for pairs trading")  
    elif total < 100:
        print("  VERDICT: OK — acceptable for mean-reversion strategies")
    else:
        print("  VERDICT: Consider moving VPS closer to broker")
else:
    print(f"  TOTAL: Check TCP latency results above")

print()
