#!/bin/bash
# netcfg/r2.sh -- configure router r2.
# Usage: r2.sh IF_B IF_C IF_D
#   IF_B: interface on LAN B  (10.10.2.2/24)
#   IF_C: interface on LAN C  (10.10.3.1/24)
#   IF_D: interface on Link D (10.10.4.1/30)
#
# Routing (A3, A6 -- no default route):
#   10.10.1.10/32 via 10.10.4.2 dev IF_D : everything for the client (in
#       particular server -> client) leaves over Link D (A3).
#   10.10.1.0/24  via 10.10.2.1 dev IF_B : the rest of LAN A (r1's 10.10.1.1)
#       is reached through r1 (A1). Longest-prefix match makes the /32 win
#       for the client.
#
# Reverse-path filtering (A7 / report R3):
#   Effective rp_filter = max(conf.all.rp_filter, conf.IF.rp_filter).
#   client -> server packets arrive on IF_B (from r1) with source 10.10.1.10,
#   but r2's route back to 10.10.1.10 is via IF_D. Strict mode on IF_B would
#   drop the whole client -> server direction. IF_B is set to loose (2): the
#   source only has to be routable via some interface. IF_C and IF_D keep
#   strict (1): packets on IF_C come from LAN C (connected) and packets on
#   IF_D come from 10.10.4.2 (connected) -- the client never sends anything
#   else over Link D because its only non-connected route is via r1.
#
# ICMP redirects: a redirect is only sent when a packet is forwarded out of
# the same interface it arrived on. r2 forwards IF_B -> IF_C, IF_C -> IF_D and
# IF_C -> IF_B only, so no redirects are generated; send_redirects is left at
# its default.
set -e

if [ "$#" -ne 3 ]; then
    echo "usage: $0 IF_B IF_C IF_D" >&2
    exit 1
fi
IF_B="$1"
IF_C="$2"
IF_D="$3"

# Router (A4).
sysctl -q -w net.ipv4.ip_forward=1
sysctl -q -w "net/ipv4/conf/$IF_B/forwarding=1"
sysctl -q -w "net/ipv4/conf/$IF_C/forwarding=1"
sysctl -q -w "net/ipv4/conf/$IF_D/forwarding=1"

# Reverse-path filtering (see header).
sysctl -q -w "net/ipv4/conf/$IF_B/rp_filter=2"
sysctl -q -w "net/ipv4/conf/$IF_C/rp_filter=1"
sysctl -q -w "net/ipv4/conf/$IF_D/rp_filter=1"

ip addr replace 10.10.2.2/24 dev "$IF_B"
ip addr replace 10.10.3.1/24 dev "$IF_C"
ip addr replace 10.10.4.1/30 dev "$IF_D"
ip link set dev "$IF_B" up
ip link set dev "$IF_C" up
ip link set dev "$IF_D" up

# No default route (A6).
ip route replace 10.10.1.10/32 via 10.10.4.2 dev "$IF_D"
ip route replace 10.10.1.0/24 via 10.10.2.1 dev "$IF_B"
