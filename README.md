# COL334 Assignment 3 — TCP/IPv4 over raw sockets + HTTP

**Team**
- Shivanshu Aryan — <ENTRY_NUMBER_1>
- <TEAMMATE_NAME> — <ENTRY_NUMBER_2>

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
| `src/tcp.py` | TCP state machine, reliable transfer, out-of-order buffer, Reno congestion control |
| `src/client.py` | Part C HTTP client |
| `src/server.py` | Part D HTTP server |
| `client/run-client`, `server/run-server` | launchers |

## Running on the VMs

The kernel has no TCP socket on our ports, so it answers our peers' SYN / SYN-ACK
with RSTs. Drop those once per boot (allowed on Piazza; not part of netcfg):

```
# client VM
sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST --sport 61000:65535 -j DROP
# server VM
sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST --sport 8080:8083 -j DROP
```

## Development testbed (not part of the submission)

`test/` rebuilds the topology with network namespaces inside a privileged
Ubuntu 22.04 container and runs the end-to-end, impairment (loss / reorder /
duplication / corruption) and fairness experiments:

```
docker run --rm --privileged --network none -v "$PWD":/a3 a3test bash /a3/test/check_netcfg.sh
docker run --rm --privileged --network none -v "$PWD":/a3 a3test bash /a3/test/e2e.sh
docker run --rm --privileged --network none -v "$PWD":/a3 a3test bash /a3/test/fairness.sh
```
