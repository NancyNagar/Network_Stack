#!/usr/bin/env python3
"""Validate packet-log files against the handout format (section 2.6).
Usage: validate_logs.py ROLE FILE...   ROLE = client|server
Prints one line per problem (max 20 per file); exit 0 iff all valid."""
import re
import sys

RX = re.compile(r"^(SEND|RECV) (\d+\.\d+\.\d+\.\d+):(\d+) > (\d+\.\d+\.\d+\.\d+):(\d+) "
                r"ttl=(\d+) id=(\d+) seq=(\d+) ack=(\d+) flags=0x([0-9a-f]{2}) "
                r"win=(\d+) len=(\d+) mss=(\d+|-)$")
role = sys.argv[1]
bad = 0
for fn in sys.argv[2:]:
    n = 0
    errs = []
    with open(fn, "rb") as f:
        raw = f.read()
    if raw and not raw.endswith(b"\n"):
        errs.append("no trailing newline")
    for i, line in enumerate(raw.decode("ascii", "replace").split("\n")[:-1], 1):
        n += 1
        m = RX.match(line)
        if not m:
            errs.append("%d: bad format: %r" % (i, line))
            continue
        d, sip, sp, dip, dp, ttl, ipid, seq, ack, fl, win, ln, mss = m.groups()
        fl = int(fl, 16)
        for v, lim, nm in ((ttl, 255, "ttl"), (ipid, 65535, "id"), (seq, 2**32 - 1, "seq"),
                           (ack, 2**32 - 1, "ack"), (win, 65535, "win"), (sp, 65535, "port"),
                           (dp, 65535, "port")):
            if int(v) > lim:
                errs.append("%d: %s out of range" % (i, nm))
        syn = fl & 0x02
        if d == "SEND":
            if int(ipid) == 0:
                errs.append("%d: SEND id=0" % i)
            if syn and mss != "1460":
                errs.append("%d: SEND SYN without mss=1460" % i)
            if not syn and mss != "-":
                errs.append("%d: SEND non-SYN carries mss" % i)
            if int(ln) > 1460:
                errs.append("%d: SEND len>1460" % i)
            if int(win) != 65535:
                errs.append("%d: SEND win!=65535" % i)
            local_port = int(sp)
        else:
            if syn and mss == "-":
                errs.append("%d: RECV SYN without mss" % i)
            local_port = int(dp)
        if role == "client" and not 61000 <= local_port <= 65535:
            errs.append("%d: client port %d not in 61000-65535" % (i, local_port))
    if n == 0:
        errs.append("empty log")
    for e in errs[:20]:
        print("%s: %s" % (fn, e))
    if len(errs) > 20:
        print("%s: ... %d more" % (fn, len(errs) - 20))
    bad += bool(errs)
sys.exit(1 if bad else 0)
