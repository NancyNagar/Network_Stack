#!/bin/bash
set -e

if [ "$#" -ne 3 ]; then
    echo "usage: $0 IF_B IF_C IF_D" >&2
    exit 1
fi
IF_B="$1"
IF_C="$2"
IF_D="$3"

sysctl -q -w net.ipv4.ip_forward=1
sysctl -q -w "net/ipv4/conf/$IF_B/forwarding=1"
sysctl -q -w "net/ipv4/conf/$IF_C/forwarding=1"
sysctl -q -w "net/ipv4/conf/$IF_D/forwarding=1"

sysctl -q -w "net/ipv4/conf/$IF_B/rp_filter=2"
sysctl -q -w "net/ipv4/conf/$IF_C/rp_filter=1"
sysctl -q -w "net/ipv4/conf/$IF_D/rp_filter=1"

ip addr replace 10.10.2.2/24 dev "$IF_B"
ip addr replace 10.10.3.1/24 dev "$IF_C"
ip addr replace 10.10.4.1/30 dev "$IF_D"
ip link set dev "$IF_B" up
ip link set dev "$IF_C" up
ip link set dev "$IF_D" up

ip route replace 10.10.1.10/32 via 10.10.4.2 dev "$IF_D"
ip route replace 10.10.1.0/24 via 10.10.2.1 dev "$IF_B"
