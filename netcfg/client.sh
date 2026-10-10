#!/bin/bash
# netcfg/client.sh -- configure the client host.
# Usage: client.sh IF_A IF_D
#   IF_A: interface on LAN A  (10.10.1.10/24)
#   IF_D: interface on Link D (10.10.4.2/30)
#
# Routing: a single default route via r1 (10.10.1.1) on LAN A, so every packet
# the client originates towards LANs B/C travels client -> r1 -> r2 -> server
# (A2) with source 10.10.1.10. Link D (10.10.4.0/30) is reached as a directly
# connected network only.
#
# Reverse-path filtering (A7 / report R3):
#   Effective rp_filter of an interface = max(conf.all.rp_filter, conf.IF.rp_filter).
#   The server (and r2) send replies to 10.10.1.10 over Link D (A3), so they
#   arrive on IF_D with source e.g. 10.10.3.10, while the client's own route
#   back to 10.10.3.10 points out of IF_A. Strict mode (1) would drop them.
#   We therefore set loose mode (2) on IF_D only: a packet is accepted if its
#   source is reachable through *any* interface (the default route covers it).
#   conf.all and IF_A stay strict (1); packets arriving on IF_A always come from
#   LAN A or from r1 whose sources route back via IF_A, so strict is fine there.
#
# The client is a host: IP forwarding is disabled (A4).
set -e

if [ "$#" -ne 2 ]; then
    echo "usage: $0 IF_A IF_D" >&2
    exit 1
fi
IF_A="$1"
IF_D="$2"

# Host, not a router (A4).
sysctl -q -w net.ipv4.ip_forward=0
sysctl -q -w "net/ipv4/conf/$IF_A/forwarding=0"
sysctl -q -w "net/ipv4/conf/$IF_D/forwarding=0"

# Reverse-path filtering (see header): strict on IF_A, loose on IF_D.
sysctl -q -w "net/ipv4/conf/$IF_A/rp_filter=1"
sysctl -q -w "net/ipv4/conf/$IF_D/rp_filter=2"

# Addresses (replace => safe to re-run).
ip addr replace 10.10.1.10/24 dev "$IF_A"
ip addr replace 10.10.4.2/30 dev "$IF_D"
ip link set dev "$IF_A" up
ip link set dev "$IF_D" up

# Everything not directly connected goes via r1 on LAN A (A2).
ip route replace default via 10.10.1.1 dev "$IF_A"
