#!/bin/bash
# netcfg/r1.sh -- configure router r1.
# Usage: r1.sh IF_A IF_B
#   IF_A: interface on LAN A (10.10.1.1/24)
#   IF_B: interface on LAN B (10.10.2.1/24)
#
# Routing (A5): besides the two connected networks, r1 has exactly one route,
# a default route via r2 (10.10.2.2). LAN C and Link D are reached through it.
#
# Reverse-path filtering (A7 / report R3): stays strict (1) everywhere, as
# required. Every packet r1 receives on IF_A comes from LAN A (connected) and
# every packet on IF_B comes from r2 with a source on LAN B or LAN C, both of
# which route back via IF_B (the default route), so strict mode never drops
# legitimate traffic. (Traffic sourced from 10.10.4.2 would arrive on IF_A
# and fail the strict check; that is why the client must use 10.10.1.10 as the
# source for everything routed via r1.)
#
# ICMP redirects: r1 never forwards a packet back out of the interface it came
# in on (LAN A <-> LAN B only), so it never emits redirects; nothing to change.
set -e

if [ "$#" -ne 2 ]; then
    echo "usage: $0 IF_A IF_B" >&2
    exit 1
fi
IF_A="$1"
IF_B="$2"

# Router (A4).
sysctl -q -w net.ipv4.ip_forward=1
sysctl -q -w "net/ipv4/conf/$IF_A/forwarding=1"
sysctl -q -w "net/ipv4/conf/$IF_B/forwarding=1"

# Strict reverse-path filtering on both interfaces (A7).
sysctl -q -w "net/ipv4/conf/$IF_A/rp_filter=1"
sysctl -q -w "net/ipv4/conf/$IF_B/rp_filter=1"

ip addr replace 10.10.1.1/24 dev "$IF_A"
ip addr replace 10.10.2.1/24 dev "$IF_B"
ip link set dev "$IF_A" up
ip link set dev "$IF_B" up

# The single non-connected route (A5).
ip route replace default via 10.10.2.2 dev "$IF_B"
