# COL334 Assignment 3 — TCP/IPv4 over raw sockets + HTTP

**Team chill_chill**
- Shivanshu Aryan — 2024CS10237
- Nancy Nagar — 2024CS10518

**Language / runtime:** Python 3 (tested with Python 3.10 on Ubuntu 22.04), standard library only.

**Build:** nothing to build. The launchers run the sources directly:

```
sudo ./server/run-server [-p PORT] [-d DOCROOT] [--log LOGFILE]
sudo ./client/run-client URL [-o OUTFILE] [--log LOGFILE]
```

## Layout

| Path | Contents |
|------|----------|
| `netcfg/*.sh` | Part A: per-machine static configuration (`ip` and `sysctl` only) |
| `src/packet.py` | IPv4/TCP header build + parse, checksums, MSS option, packet log |
| `src/tcp.py` | TCP connection and listener, plus three components: `RttEstimator` (RTO), `NewReno` (congestion window) and `ReceiveQueue` (in-order delivery and out-of-order buffer) |
| `src/client.py` | Part C HTTP client |
| `src/server.py` | Part D HTTP server |
| `client/run-client`, `server/run-server` | launchers |

## Running on the VMs

The kernel has no TCP socket on our ports, so it answers our peers' SYN / SYN-ACK
with RSTs. Drop those once per boot (done by hand, not part of netcfg):

```
# client VM
sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST --sport 61000:65535 -j DROP
# server VM
sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST --sport 8080:8083 -j DROP
```
