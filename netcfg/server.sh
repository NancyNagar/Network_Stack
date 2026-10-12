#!/bin/bash
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
