"""
TCP Bridge Diagnostic v2 — test EA data flow
Sends ACK after each received message in case MT5 needs it.
"""
import socket
import struct
import json
import time

HOST = "0.0.0.0"
PORT = 5555

def recv_exact(sock, n, timeout=5.0):
    sock.settimeout(timeout)
    data = b''
    while len(data) < n:
        try:
            chunk = sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        except socket.timeout:
            if len(data) > 0:
                print(f"    [partial read: got {len(data)}/{n} bytes before timeout]")
            return None
    return data

def recv_msg(sock, timeout=3.0):
    header = recv_exact(sock, 4, timeout)
    if not header:
        return None
    msg_len = struct.unpack('>I', header)[0]
    if msg_len <= 0 or msg_len > 1_000_000:
        print(f"  [!] Invalid msg_len: {msg_len} (header bytes: {header.hex()})")
        return None
    payload = recv_exact(sock, msg_len, timeout=5.0)
    if not payload:
        print(f"  [!] Failed to read {msg_len} bytes payload")
        return None
    return json.loads(payload.decode('utf-8'))

def send_msg(sock, data):
    """Send length-prefixed JSON message back to EA."""
    payload = json.dumps(data).encode('utf-8')
    header = struct.pack('>I', len(payload))
    sock.sendall(header + payload)

print(f"TCP Diagnostic v2 starting on {HOST}:{PORT}")
print("Waiting for EA to connect...")

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((HOST, PORT))
srv.listen(1)
srv.settimeout(60.0)

try:
    client, addr = srv.accept()
    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"EA connected from {addr}")
    print()

    count = 0
    start_time = time.time()
    
    while count < 30 and (time.time() - start_time) < 15:  # 30 msgs or 15 seconds
        msg = recv_msg(client, timeout=3.0)
        if msg is None:
            elapsed = time.time() - start_time
            print(f"  [!] No message at t={elapsed:.1f}s (after {count} messages)")
            if count == 0:
                break  # No messages at all
            continue

        count += 1
        mt = msg.get('mt', '???')
        
        if mt == 'DATA':
            quotes = msg.get('q', {})
            if count <= 3 or count % 10 == 0:  # Print first 3 and every 10th
                print(f"[{count}] DATA | Symbols: {list(quotes.keys())} | "
                      f"Balance: {msg.get('a',{}).get('balance','?')} | "
                      f"Time: {msg.get('t',{}).get('datetime','?')}")
                for sym, q in quotes.items():
                    print(f"    {sym}: bid={q.get('bid')}, ask={q.get('ask')}")
            else:
                print(f"[{count}] DATA | {len(quotes)} symbols | ok")
        else:
            print(f"[{count}] Non-DATA: {json.dumps(msg)[:200]}")

    elapsed = time.time() - start_time
    rate = count / elapsed if elapsed > 0 else 0
    print(f"\nDiagnostic complete: {count} messages in {elapsed:.1f}s ({rate:.1f} msg/s)")
    
    if count <= 1:
        print("\n*** PROBLEM: EA only sent 1 message then stopped ***")
        print("Check MT5 Experts tab for error messages.")
        print("Also check: did you recompile AND re-attach the EA?")

except socket.timeout:
    print("[!] No EA connection within 60s")
except Exception as e:
    print(f"[!] Error: {e}")
    import traceback
    traceback.print_exc()
finally:
    try:
        client.close()
    except:
        pass
    srv.close()
    print("Server closed.")
