#!/bin/bash
# test/topo.sh -- build the autograder-like topology in network namespaces and
# run netcfg/*.sh in them. Must run as root (inside the a3test container:
#   docker run --rm --privileged --network none -v "$ROOT":/a3 a3test bash /a3/test/topo.sh
# ).  Sourcing is fine too: it exports the interface names below.
#
#   client [c-a]---LAN A---[r1-a] r1 [r1-b]---LAN B---[r2-b] r2 [r2-c]---LAN C---[s-c] server
#   client [c-d]-------------------Link D---------------------[r2-d] r2
#
# Each ns also gets a 'spare0' dummy interface (down, no address) so that
# check_netcfg.sh can verify the scripts touch no other interface.
set -e
ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
NSS="client r1 r2 server"
export C_A=c-a C_D=c-d R1_A=r1-a R1_B=r1-b R2_B=r2-b R2_C=r2-c R2_D=r2-d S_C=s-c

for ns in $NSS; do ip netns del "$ns" 2>/dev/null || true; done
for ns in $NSS; do
    ip netns add "$ns"
    ip -n "$ns" link set lo up
    # strict rp_filter everywhere *before* interfaces exist (default) and after
    ip netns exec "$ns" sysctl -q -w net.ipv4.conf.all.rp_filter=1 net.ipv4.conf.default.rp_filter=1
done

ip link add "$C_A"  netns client type veth peer name "$R1_A" netns r1
ip link add "$R1_B" netns r1     type veth peer name "$R2_B" netns r2
ip link add "$R2_C" netns r2     type veth peer name "$S_C"  netns server
ip link add "$C_D"  netns client type veth peer name "$R2_D" netns r2

for ns in $NSS; do
    ip -n "$ns" link add spare0 type dummy
    for ifc in $(ip -n "$ns" -o link show | awk -F': ' '{print $2}' | cut -d@ -f1); do
        ip netns exec "$ns" sysctl -q -w "net/ipv4/conf/$ifc/rp_filter=1"
        case "$ifc" in lo|spare0) ;; *)
            # Raw-socket receivers must see full checksums from kernel TCP (curl/wget).
            ip netns exec "$ns" ethtool -K "$ifc" tx off rx off >/dev/null 2>&1 || true
            # no GSO/GRO super-packets: keeps the 30-packet bottleneck queue honest for kernel TCP
            ip netns exec "$ns" ethtool -K "$ifc" gso off gro off tso off >/dev/null 2>&1 || true
            ip -n "$ns" link set "$ifc" down ;;
        esac
    done
done

ip netns exec client bash "$ROOT/netcfg/client.sh" "$C_A" "$C_D"
ip netns exec r1     bash "$ROOT/netcfg/r1.sh"     "$R1_A" "$R1_B"
ip netns exec r2     bash "$ROOT/netcfg/r2.sh"     "$R2_B" "$R2_C" "$R2_D"
ip netns exec server bash "$ROOT/netcfg/server.sh" "$S_C"

# Kernel-RST suppression, exactly as the grading namespaces do it.
ip netns exec client iptables -A OUTPUT -p tcp --tcp-flags RST RST --sport 61000:65535 -j DROP
ip netns exec server iptables -A OUTPUT -p tcp --tcp-flags RST RST --sport 8080:8083 -j DROP
echo "topo: ready"
