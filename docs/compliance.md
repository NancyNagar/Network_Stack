# Compliance review — COL334 A3 (read-only gate)

Reviewer: compliance agent. Date: 2026-10-10.
Sources: handout `COL334_assignment_3.pdf` (full text), Piazza notes in `guideline.txt`,
`netcfg/*.sh`, `client/run-client`, `server/run-server`, `src/{packet,tcp,client,server}.py`,
`src/test_*.py`, `test/results/summary.jsonl`.

## Verdict

| Gate | Result |
|------|--------|
| Zero-mark rules (6.2 / 6.3 / 6.4) | **PASS** — no violation found |
| Python 3.10 (Ubuntu 22.04 image `a3test`, Python 3.10.12) | **PASS** — all 6 files compile; 85 unit tests OK (1 skipped) |
| Spec T1–T14 / log format | **PASS with RISKs** — 1 confirmed bug (RTO deadlock), T14 fairness failing in own test |
| Part A netcfg | **PASS** |
| Part C / Part D | **PASS with minor RISKs** |
| Submission structure §7 | **FAIL right now** — `README.md` and `report.pdf` are missing (both mandatory) |

Overall: **code gate PASS; submission gate FAIL until README.md + report.pdf exist.**

---

## 1. Zero-mark rules (6.2 / 6.3 / 6.4)

| Rule | Evidence | Status |
|------|----------|--------|
| All TCP through raw IP socket with IP_HDRINCL | `tcp.py:89-92` — `socket(AF_INET, SOCK_RAW, IPPROTO_TCP)` + `setsockopt(IPPROTO_IP, IP_HDRINCL, 1)`; the only TCP socket in the code (`RawIO`), used by both `connect()` (`tcp.py:269`) and `TCPListener` (`tcp.py:622`). Full IPv4 header built in `packet.py:76-83`. | OK |
| No SOCK_STREAM / TCP connect / listen / accept | grep: no `SOCK_STREAM`, no `.listen(`, no kernel `.accept(`. `TCPConnection.connect`/`TCPListener.accept` are our own methods. | OK |
| UDP `connect()` for route lookup (`tcp.py:71-79`) | `SOCK_DGRAM` socket; `connect()` on UDP sends no packet, only does a route lookup; the rule bans "connect() on a **TCP** socket". Piazza (Abhishek Jain thread): "The correct way is to query the kernel routing table." | **Safe** — keep; the docstring at `tcp.py:72-73` already explains it, be ready to explain in viva |
| No AF_PACKET | grep: none | OK |
| No packet/HTTP libs | Imports: `packet.py:3-4` socket, struct; `tcp.py:8-16` collections, random, select, socket, struct, time; `client.py:14-18` argparse, re, socket, sys, collections; `server.py:9-14` argparse, os, re, signal, sys, time. No scapy/dpkt/http.*/urllib (incl. urllib.parse)/requests/socketserver/asyncio. `socket.gethostbyname` (`client.py:353`) is a libc resolver call, not an HTTP lib. | OK |
| No subprocess/os.system/popen/exec* | grep over `src/*.py`: none. | OK |
| Launchers | `client/run-client`, `server/run-server`: mode `rwxrwxr-x`, `#!/bin/sh`, `exec python3 "$(dirname "$0")/../src/X.py" "$@"` — identical to the handout example; work from any cwd; `client.py`/`server.py` import siblings via `sys.path[0]` = `src/` (server also inserts it, `server.py:297`). | OK — make sure the zip tool preserves the exec bit (`zip -r` does) |

`src/test_client.py`, `src/test_server.py`: imports are only `io, os, random, shutil, tempfile, time, unittest` + the modules under test; no forbidden library or process spawning. **Harmless if they end up in the zip.** `src/__pycache__/` (root-owned `.pyc` from docker runs) must **not** be zipped ("no binaries").

## 2. Python 3.10

```
docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v <root>:/a3:ro a3test  -> Python 3.10.12
py_compile (cfile=/tmp/x.pyc): client.py packet.py server.py tcp.py test_client.py test_server.py  -> all ok
python3 -m unittest test_client test_server  -> Ran 85 tests, OK (skipped=1)
```
No 3.11+ features (no ExceptionGroup/`except*`, tomllib, `typing.Self`, TaskGroup, `datetime.UTC`, 3.12 f-strings). `float | None` appears only in `docs/contracts/tcp_api.md`, not in code.

## 3. Spec walk (Part B, tcp.py / packet.py)

| Req | Evidence | Status / reasoning |
|-----|----------|--------------------|
| T1 | `packet.py:76-83` full IPv4 header, checksum computed; `tcp.py:101,104-105` id starts random 1..65535, wraps 65535→1, never 0. TTL 64, DF set. | OK |
| T2 | `packet.py:68-74` full TCP header, pseudo-header checksum. | OK |
| T3 | Option only when `mss` given (`packet.py:63-66`); `mss=MY_MSS` only on SYN (`tcp.py:282`) and SYN-ACK (`tcp.py:303,399`); all other sends pass no mss (incl. listener late ACK `tcp.py:656-658`). Data size `min(MY_MSS, peer_mss)` (`tcp.py:206-207`) used in `_push` (`:597`) and `_retransmit_head` (`:530`). Peer without MSS → 536 (`:19`), still ≤ min(1460, ·). MSS 0 ignored (`if syn.mss:`). | OK |
| T4 | `packet.py:111` IP checksum, `:120` TCP checksum, malformed → `None`; dropped silently in `tcp.py:141-143` before logging. Options: MSS parsed, NOP/EOL/others skipped, bounds-checked (`packet.py:128-145`). | OK (see env note E1) |
| T5 | `tcp.py:138-140` port prefilter; `tcp.py:220-222` full 4-tuple match before log/processing. | OK |
| T6 | `tcp.py:169` `random.getrandbits(32)`. | OK |
| T7 | SYN resent every `SYN_RETRY`=1.0 s, give up after 8 s (`tcp.py:22-23, 278-292`); SYN-ACK same (`:294-309`), plus immediate SYN-ACK resend on duplicate SYN (`:398-400`). | OK (≤1 s, ≥6 s) |
| T8 | `tcp.py:376-384`: RST in SYN_SENT accepted when ACK=ISS+1 → ConnectionRefused; any matching RST later → abort + ConnectionReset. | OK |
| T9 | Peer FIN ACKed (`tcp.py:469-476`); `close()` (`:317-331`) sends FIN after all data ACKed, retransmits via RTO (≤0.5 s, `:338`, `:534-535`), waits for FIN-ACK + peer FIN with `FIN_GIVEUP`=2 s deadline (`:326-327`). Listener re-ACKs a late FIN from previous peer (`:646-660`). | OK |
| T10 window | `room = min(cwnd, peer_wnd) - flight` (`tcp.py:594`). | **RISK (low)** — limited transmit lets flight reach cwnd+2·MSS (`:592-593`), and the "crude persist" sends 1 byte when peer_wnd = 0 (`:595-596`), i.e. more unacknowledged data than the peer window allows. A log-based checker comparing `seq+len-ack` with the last RECV `win` would flag the probe. |
| T10 RTO | Initial 1 s, `min(..., MAX_RTO=1.0)` everywhere (`:190, :546, :571`). | OK |
| T10 RTO path | `_on_timer` sets `snd_nxt = snd_una + min(mss, len(snd_buf))` (`tcp.py:577-579`) while `_retransmit_head` sent only `min(mss, len, snd_max-snd_una)` (`:530-531`). | **RISK (medium, confirmed bug)** — whenever the last-sent segment is shorter than MSS and unsent data sits behind it (zero/small peer window probe, small-window runt), the RTO creates "phantom" in-flight bytes that were never sent. After their predecessor is ACKed, `snd_max == snd_una`, `_retransmit_head` sends 0 bytes, `_push` is blocked by flight ≥ cwnd → nothing is ever sent again → 10 s give-up kills a transfer that should complete. Reproduced with a fake-IO harness (peer_wnd=0 → 1-byte probe → RTO → window opens → 0 segments sent over 5 RTOs). Fix: in `_on_timer` set `snd_nxt` to `snd_una + bytes actually retransmitted`, or let `_retransmit_head` send `min(mss, len(snd_buf))` and raise `snd_max` accordingly. |
| T10 10 s | `tcp.py:554-557` (flight > 0 and no progress > 10 s). | OK |
| T11 | ACK for every data/FIN segment incl. duplicate, old and out-of-window ones (`tcp.py:476`); fixed window `RCV_WINDOW`=65535 in every segment (`:212, :658`); in-order exactly-once delivery via `rcv_nxt` trimming (`:455-463`). | OK |
| T12 | OOO kept if within 65535 of `rcv_nxt` (`:464-467`), drained on fill (`:478-491`); OOO FIN handled (`:469-473`). | OK |
| T13 | `random.randint(61000, 65535)` (`tcp.py:268`); server port from `-p` (`server.py:290, 249`). | OK |
| T14 | Reno + NewReno, IW10 (`tcp.py:495-536, 564-571`). | **RISK (high)** — own fairness test (Piazza conditions: 10 Mbit/s, 10 ms, 30-pkt queue) fails at N=2 and N=3: ratio ours/curl 2.18–2.81 (pass band [0.5, 2]); N=1 passes (1.37). Evidence: `test/results/summary.jsonl` (e_fairness_N2/N3 lines), `test/results/e_fairness_N3/`. Our flows are ~2.5× more aggressive than CUBIC; root cause not determined by this review (candidates to check: per-segment ACKs vs Linux delayed ACK interacting with per-ACK cwnd growth, limited transmit + inflation during recovery, go-back-N bursts after RTO). |
| Log format | `packet.py:86-92` exactly `DIR SRC:SPORT > DST:DPORT ttl= id= seq= ack= flags=0x%02x win= len= mss=`; `ack=0` printed for SYN (`tcp.py:210`); SEND logs the values put in the packet (logged after `io.send` set `ip_id`, `tcp.py:214-215`); RECV only after checksum + 4-tuple match (`tcp.py:251-253`; listener logs only the SYN it accepts `:632-634` and late FINs of the previous connection `:654-655`); flush after every line (`packet.py:157-159`). Own validator: `test/results/d_log_problems.txt` empty. | OK |
| Source address | Kernel route lookup (`tcp.py:265`) → 10.10.1.10 toward the server, as Piazza requires. Server replies from SYN's dst (`tcp.py:635`). | OK |

Other TCP notes (no ticket): no RST is ever sent (fine; grader drops kernel RSTs per Piazza). A stale retransmitted SYN of the previous client could tie the listener up for 8 s (edge case).

## 4. Crash paths

* **Server** (`server.py:251-280`): `accept()` exceptions caught; `handle_connection` wrapped in `except Exception`; close falls back to abort. `read_head` swallows all recv errors (`:197-201`); head capped at 64 KB → 400; path errors caught (`:118, :150, :155`). OK.
  **RISK (low):** `log_line` (`server.py:238-240`) is called inside the `except` handlers (`:256, :265, :268, :270`); if stderr is a broken pipe/closed fd, the `OSError` escapes `serve()` and the server exits. Wrap `log_line` in `try/except OSError`.
* **Client** exit codes: usage error 2 (argparse, `client.py:419-421`); invalid URL / DNS → 2 (`:367-375`); connect errors incl. refused/timeout/PermissionError → 2 (`:377-380`); TCP errors / IO errors during transfer → abort + 2 (`:389-399`); malformed/incomplete → 2 (`:385-388`); complete 2xx → 0, other → 1 (`:342`). Silent server → `RecvTimeout` after 30 s → 2. OK.
  **RISK (low):** `_run` checks `self.error` before `done()` (`tcp.py:235-236, :256`), so if the last data segment and an RST arrive in the same poll batch, `recv()` raises ConnectionReset and the buffered bytes are discarded → exit 2 for a response that was actually complete. Linux returns queued data before ECONNRESET. Fix: in `recv()`, return `rcv_buf` contents if non-empty before raising the stored error.

## 5. Part C / Part D vs handout

Part C (`client.py`): one GET, target = path+query verbatim (`:96-105`), Host = authority exactly as written (`:105, :111`), `Connection: close`, no Accept-Encoding (`:108-116`); Content-Length, chunked (extensions/trailers ignored, `:264-284`), close-delimited; case-insensitive headers; body only to `-o`/stdout, status line to stderr (`:336`); no redirect following; `conn.close()` (T9) after complete response (`:402`). OK.

Part D (`server.py`): defaults `-p 8080 -d ./www`; order 400 → 501 → 403 → 404 → 200 (`:131-147`); request-line check `METHOD SP TARGET SP HTTP/x.y` and target starts with `/` (`:92-107`); query stripped, `..` segment → 403, dir / trailing `/` → index.html (`:110-121, :141-145`); every response HTTP/1.1 with Content-Length, Content-Type, Connection: close (`:55-66`), HTML error bodies, HEAD = same headers without body (`:69-74, :160-162`); Content-Type table case-insensitive (`:28-48`); 10 s head deadline → close without response (`:186-201, :259`); one connection at a time. OK.

Part A (`netcfg/`): only `ip` and `sysctl` (plus shell builtins `echo/exit/[`); interfaces only via `$1..$3`; addresses per Table 1; client default via r1 (A2), r2 /32 to 10.10.1.10 via Link D (A3), r1 single default route (A5), r2 no default (A6), forwarding 0 on hosts / 1 on routers (A4), rp_filter strict on r1 and server; loose only on client IF_D and r2 IF_B where the asymmetric path requires it (A7); static only (A8). OK.
Note (no ticket): `sysctl net.ipv4.ip_forward=…` is global and rewrites `forwarding` of every interface (incl. lo / an SSH adapter). That is the conventional way and what a grader checking A4 will read; acceptable.

## 6. Submission structure (§7)

| Item | State |
|------|-------|
| `netcfg/{client,r1,r2,server}.sh` | present, executable |
| `client/run-client`, `server/run-server` | present, executable |
| `src/` | present; exclude `src/__pycache__/` (root-owned .pyc = binaries); `test_*.py` optional/harmless |
| `README.md` (team, language/runtime, how to build) | **MISSING** |
| `report.pdf` | **MISSING** (`report/` is empty) |
| Makefile | absent (optional; Python needs no build — README should say so) |
| Must not include | `test/results/`, pcaps, `www/` big files, `*.zip`, `__pycache__` |
| Zip name | `A3_<ENTRY1>_<ENTRY2>.zip`, entries sorted, capitals, no spaces/hyphens |

## 7. Environment notes (no code change possible)

* **E1** Raw-socket receivers see unfinished TCP checksums when the sender's NIC uses TX checksum offload on veth (namespaces); T4 then drops those packets. Our own topology disables offloads (`test/topo.sh:36-38`); the grader's namespaces are assumed to do the same. On the report VMs, if the client drops everything from `python3 -m http.server`, disable offload/GRO with `ethtool -K <if> tx off rx off gro off gso off tso off` (outside the submitted code, not in netcfg).
* **E2** Kernel RSTs: handled by the grader per Piazza; on VMs use a manual iptables OUTPUT DROP, never in netcfg or code.

## Tickets (most severe first)

```json
[
 {"id":"C-001","from":"compliance","to":"orchestrator","req":"§7","test":"submission structure","expected":"README.md (team, language/runtime, build steps) and report.pdf at zip root","actual":"both missing; report/ is empty","evidence":"ls <root>: no README.md, no report.pdf","priority":"high"},
 {"id":"C-002","from":"compliance","to":"orchestrator","req":"T14","test":"e_fairness_N2, e_fairness_N3","expected":"ours/curl throughput ratio within [0.5,2]","actual":"ratio 2.18-2.81 (ours ~2.4 Mbit/s vs curl ~0.9 Mbit/s at N=3); N=1 passes (1.37)","evidence":"test/results/summary.jsonl (e_fairness_N2/N3 lines); src/tcp.py:495-536,564-571","priority":"high"},
 {"id":"C-003","from":"compliance","to":"orchestrator","req":"T10/T12","test":"fake-IO harness: peer_wnd=0 -> 1-byte probe -> RTO -> window opens","expected":"transfer resumes","actual":"0 segments sent over 5 RTOs; phantom flight 1459 B with snd_max==snd_una -> 10 s give-up","evidence":"src/tcp.py:577-579 sets snd_nxt=snd_una+min(mss,len(snd_buf)) but src/tcp.py:530-531 only resent min(mss,len,snd_max-snd_una)","priority":"medium"},
 {"id":"C-004","from":"compliance","to":"orchestrator","req":"T10","test":"code review","expected":"flight <= min(cwnd, peer window) always","actual":"1-byte probe sent when peer_wnd=0; limited transmit allows cwnd+2*MSS","evidence":"src/tcp.py:592-596","priority":"low"},
 {"id":"C-005","from":"compliance","to":"orchestrator","req":"Part C exit code / T8","test":"code review","expected":"bytes already received in order are delivered before a reset is reported","actual":"_run raises the stored ConnectionReset before done() is checked, discarding rcv_buf -> exit 2 on an otherwise complete response if data+RST arrive in one batch","evidence":"src/tcp.py:235-236,256,365","priority":"low"},
 {"id":"C-006","from":"compliance","to":"http-server","req":"Part D (never exit because of a client)","test":"code review","expected":"serve loop survives any error","actual":"log_line OSError (closed/broken stderr) raised inside except handlers escapes serve()","evidence":"src/server.py:238-240,256,265,268,270","priority":"low"},
 {"id":"C-007","from":"compliance","to":"orchestrator","req":"§7 no binaries","test":"packaging","expected":"no .pyc in zip","actual":"src/__pycache__/ with root-owned .pyc exists","evidence":"src/__pycache__/*.cpython-310.pyc","priority":"low"}
]
```
