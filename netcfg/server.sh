#!/bin/bash
# netcfg/server.sh -- configure the server host.
# Usage: server.sh IF_C
#   IF_C: interface on LAN C (10.10.3.10/24)
#
# Routing: a single default route via r2 (10.10.3.1). r2 then sends traffic
# for the client over Link D (A3).
#
# Reverse-path filtering (A7 / report R3): stays strict (1). All traffic
# reaches the server on its only interface, and every source address routes
# back through that same interface, so strict mode never drops anything.
#
# The server is a host: IP forwarding is disabled (A4).
set -e

if [ "$#" -ne 1 ]; then
    echo "usage: $0 IF_C" >&2
    exit 1
fi
IF_C="$1"

sysctl -q -w net.ipv4.ip_forward=0
sysctl -q -w "net/ipv4/conf/$IF_C/forwarding=0"
sysctl -q -w "net/ipv4/conf/$IF_C/rp_filter=1"

ip addr replace 10.10.3.10/24 dev "$IF_C"
ip link set dev "$IF_C" up

ip route replace default via 10.10.3.1 dev "$IF_C"
