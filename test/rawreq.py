#!/usr/bin/env python3
"""Test-only raw HTTP request over kernel TCP (stdlib socket).
Usage: rawreq.py HOST PORT PAYLOAD [HOLD_SECONDS]
PAYLOAD uses Python escapes (e.g. 'GARBAGE\\r\\n\\r\\n'). Sends it, optionally
keeps the connection open without sending more, then reads until close (max
20 s) and prints the response to stdout. Last stderr line: 'closed_after=<s>'."""
import socket
import sys
import time

host, port, payload = sys.argv[1], int(sys.argv[2]), sys.argv[3]
hold = float(sys.argv[4]) if len(sys.argv) > 4 else 0
data = payload.encode().decode("unicode_escape").encode("latin-1")
s = socket.create_connection((host, port), timeout=20)
t0 = time.time()
s.sendall(data)
out = b""
try:
    while True:
        d = s.recv(65536)
        if not d:
            break
        out += d
except socket.timeout:
    pass
sys.stdout.buffer.write(out)
sys.stderr.write("closed_after=%.1f\n" % (time.time() - t0))
