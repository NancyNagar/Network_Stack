#!/bin/bash
set -e

if [ "$#" -ne 2 ]; then
    echo "usage: $0 IF_A IF_D" >&2
    exit 1
fi
IF_A="$1"
IF_D="$2"

sysctl -q -w net.ipv4.ip_forward=0
sysctl -q -w "net/ipv4/conf/$IF_A/forwarding=0"
sysctl -q -w "net/ipv4/conf/$IF_D/forwarding=0"

sysctl -q -w "net/ipv4/conf/$IF_A/rp_filter=1"
sysctl -q -w "net/ipv4/conf/$IF_D/rp_filter=2"

ip addr replace 10.10.1.10/24 dev "$IF_A"
ip addr replace 10.10.4.2/30 dev "$IF_D"
ip link set dev "$IF_A" up
ip link set dev "$IF_D" up

ip route replace default via 10.10.1.1 dev "$IF_A"
