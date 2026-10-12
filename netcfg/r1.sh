#!/bin/bash
set -e

if [ "$#" -ne 2 ]; then
    echo "usage: $0 IF_A IF_B" >&2
    exit 1
fi
IF_A="$1"
IF_B="$2"

sysctl -q -w net.ipv4.ip_forward=1
sysctl -q -w "net/ipv4/conf/$IF_A/forwarding=1"
sysctl -q -w "net/ipv4/conf/$IF_B/forwarding=1"

sysctl -q -w "net/ipv4/conf/$IF_A/rp_filter=1"
sysctl -q -w "net/ipv4/conf/$IF_B/rp_filter=1"

ip addr replace 10.10.1.1/24 dev "$IF_A"
ip addr replace 10.10.2.1/24 dev "$IF_B"
ip link set dev "$IF_A" up
ip link set dev "$IF_B" up

ip route replace default via 10.10.2.2 dev "$IF_B"
