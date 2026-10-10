#!/usr/bin/env python3
"""Check a raw HTTP response (stdin) from our server.
Usage: check_resp.py STATUS [GET|HEAD] [EXPECTED_CONTENT_TYPE] [EXPECTED_BODY_LEN]
Checks: HTTP/1.1 status line with STATUS, Content-Length matches body (GET) or
is present (HEAD, body must be empty), Content-Type present (and equal if given),
Connection: close. Prints 'ok' or the problems; exit 0 iff ok."""
import sys

raw = sys.stdin.buffer.read()
want = sys.argv[1]
method = sys.argv[2] if len(sys.argv) > 2 else "GET"
ctype = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else None
blen = int(sys.argv[4]) if len(sys.argv) > 4 else None
errs = []
if b"\r\n\r\n" not in raw:
    print("no complete head; got %r" % raw[:200])
    sys.exit(1)
head, body = raw.split(b"\r\n\r\n", 1)
lines = head.decode("latin-1").split("\r\n")
parts = lines[0].split(" ", 2)
if len(parts) < 2 or parts[0] != "HTTP/1.1" or parts[1] != want:
    errs.append("status line %r" % lines[0])
h = {}
for l in lines[1:]:
    if ":" in l:
        k, v = l.split(":", 1)
        h[k.strip().lower()] = v.strip()
cl = h.get("content-length")
if cl is None or not cl.isdigit():
    errs.append("missing/bad Content-Length")
elif method == "HEAD":
    if body:
        errs.append("HEAD response has %d body bytes" % len(body))
    if blen is not None and int(cl) != blen:
        errs.append("Content-Length %s != %d" % (cl, blen))
elif int(cl) != len(body):
    errs.append("Content-Length %s != body %d" % (cl, len(body)))
elif blen is not None and len(body) != blen:
    errs.append("body %d != %d" % (len(body), blen))
if "content-type" not in h:
    errs.append("missing Content-Type")
elif ctype and h["content-type"].split(";")[0].strip().lower() != ctype:
    errs.append("Content-Type %r != %r" % (h["content-type"], ctype))
if h.get("connection", "").lower() != "close":
    errs.append("Connection header %r" % h.get("connection"))
if want != "200" and method == "GET" and b"<" not in body:
    errs.append("error body not HTML")
print("ok" if not errs else "; ".join(errs))
sys.exit(1 if errs else 0)
