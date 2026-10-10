#!/bin/bash
# test/check_netcfg.sh -- verify A1-A8 on the namespace topology.
# Usage (in container): bash /a3/test/check_netcfg.sh   (builds topology itself
# unless NO_TOPO=1). Prints PASS/FAIL per requirement; exit 0 iff all PASS.
ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
if [ "${NO_TOPO:-0}" != 1 ]; then
    source "$ROOT/test/topo.sh" >/dev/null || { echo "FAIL topo build"; exit 1; }
else
    export C_A=c-a C_D=c-d R1_A=r1-a R1_B=r1-b R2_B=r2-b R2_C=r2-c R2_D=r2-d S_C=s-c
fi
set +e
sleep 1.5   # let link state / ARP settle
declare -A RES DET
fail() { RES[$1]=FAIL; DET[$1]="${DET[$1]}$2; "; }
pass() { [ -z "${RES[$1]}" ] && RES[$1]=PASS; }
X() { local ns=$1; shift; ip netns exec "$ns" "$@"; }

LANADDR="10.10.1.10 10.10.1.1 10.10.2.1 10.10.2.2 10.10.3.1 10.10.3.10"
# ---------- A1 reachability
for ns in client r1 r2 server; do
    for a in $LANADDR; do
        X $ns ping -c1 -W1 -q $a >/dev/null 2>&1 || fail A1 "$ns->$a"
    done
done
X client ping -c1 -W1 -q 10.10.4.1 >/dev/null 2>&1 || fail A1 "client->10.10.4.1"
X r2     ping -c1 -W1 -q 10.10.4.2 >/dev/null 2>&1 || fail A1 "r2->10.10.4.2"
pass A1

# ---------- A2 client->server path: client->r1->r2->server
rg() { X "$@" 2>&1 | head -1; }
o=$(rg client ip route get 10.10.3.10);                         [[ $o == *"via 10.10.1.1 dev $C_A"* && $o == *"src 10.10.1.10"* ]] || fail A2 "client: $o"
o=$(rg r1 ip route get 10.10.3.10 from 10.10.1.10 iif $R1_A);   [[ $o == *"via 10.10.2.2 dev $R1_B"* ]] || fail A2 "r1: $o"
o=$(rg r2 ip route get 10.10.3.10 from 10.10.1.10 iif $R2_B);   [[ $o == *"dev $R2_C"* && $o != *"via"* ]] || fail A2 "r2: $o"
o=$(rg server ip route get 10.10.3.10 from 10.10.1.10 iif $S_C); [[ $o == local* ]] || fail A2 "server: $o"
tr=$(X client traceroute -n -q1 -w1 -m6 10.10.3.10 2>/dev/null | awk 'NR>1{print $2}' | xargs)
[[ $tr == "10.10.1.1 10.10.4.1 10.10.3.10" || $tr == "10.10.1.1 10.10.2.2 10.10.3.10" ]] || fail A2 "traceroute: $tr"
# traffic counters: client->server must be forwarded by r1 (LAN A -> LAN B)
X r1 iptables -I FORWARD -i $R1_A -o $R1_B -s 10.10.1.10 -d 10.10.3.10 -j ACCEPT
X r2 iptables -I FORWARD -i $R2_B -o $R2_C -s 10.10.1.10 -d 10.10.3.10 -j ACCEPT
X client ping -c3 -i0.2 -W1 -q 10.10.3.10 >/dev/null 2>&1
c1=$(X r1 iptables -L FORWARD -vnx | awk 'NR==3{print $1}'); c2=$(X r2 iptables -L FORWARD -vnx | awk 'NR==3{print $1}')
[ "${c1:-0}" -ge 3 ] && [ "${c2:-0}" -ge 3 ] || fail A2 "fwd counters r1=$c1 r2=$c2"
X r1 iptables -F FORWARD; X r2 iptables -F FORWARD
pass A2

# ---------- A3 server->client path: server->r2->client over Link D
o=$(rg server ip route get 10.10.1.10);                          [[ $o == *"via 10.10.3.1 dev $S_C"* ]] || fail A3 "server: $o"
o=$(rg r2 ip route get 10.10.1.10 from 10.10.3.10 iif $R2_C);    [[ $o == *"via 10.10.4.2 dev $R2_D"* ]] || fail A3 "r2: $o"
o=$(rg client ip route get 10.10.1.10 from 10.10.3.10 iif $C_D); [[ $o == local* ]] || fail A3 "client accepts on IF_D: $o"
tr=$(X server traceroute -n -q1 -w1 -m6 10.10.1.10 2>/dev/null | awk 'NR>1{print $2}' | xargs)
[[ $tr == "10.10.3.1 10.10.1.10" ]] || fail A3 "traceroute: $tr"
X client iptables -I INPUT -i $C_D -s 10.10.3.10 -j ACCEPT
X r1 iptables -I FORWARD -s 10.10.3.10 -d 10.10.1.10 -j ACCEPT
X server ping -c3 -i0.2 -W1 -q 10.10.1.10 >/dev/null 2>&1 || fail A3 "server ping client"
c1=$(X client iptables -L INPUT -vnx | awk 'NR==3{print $1}'); c2=$(X r1 iptables -L FORWARD -vnx | awk 'NR==3{print $1}')
[ "${c1:-0}" -ge 3 ] || fail A3 "client IF_D rx count=$c1"
[ "${c2:-0}" -eq 0 ] || fail A3 "r1 forwarded server->client ($c2)"
X client iptables -D INPUT -i $C_D -s 10.10.3.10 -j ACCEPT; X r1 iptables -F FORWARD
pass A3

# ---------- A4 forwarding
sv() { X $1 sysctl -n "$2" 2>/dev/null; }
[ "$(sv r1 net.ipv4.ip_forward)" = 1 ] && [ "$(sv r2 net.ipv4.ip_forward)" = 1 ] || fail A4 "routers ip_forward"
[ "$(sv client net.ipv4.ip_forward)" = 0 ] && [ "$(sv server net.ipv4.ip_forward)" = 0 ] || fail A4 "hosts ip_forward"
for i in $C_A $C_D; do [ "$(sv client net/ipv4/conf/$i/forwarding)" = 0 ] || fail A4 "client $i forwarding"; done
[ "$(sv server net/ipv4/conf/$S_C/forwarding)" = 0 ] || fail A4 "server forwarding"
pass A4

# ---------- A5 r1: connected + exactly one other route (default)
rt=$(X r1 ip -4 route show table all | grep -v '^local \|^broadcast \|table local')
nonconn=$(echo "$rt" | grep -v 'proto kernel scope link' | grep -c .)
conn=$(echo "$rt" | grep -c 'proto kernel scope link')
def=$(echo "$rt" | grep -c '^default via 10.10.2.2')
[ "$nonconn" = 1 ] && [ "$def" = 1 ] && [ "$conn" = 2 ] || fail A5 "r1 routes: $(echo "$rt" | tr '\n' '|')"
pass A5

# ---------- A6 r2 no default
X r2 ip -4 route show table all | grep -q '^default\|0\.0\.0\.0/0' && fail A6 "r2 has default"
pass A6

# ---------- A7 rp_filter (effective = max(all, iface)); r1/server strict
eff() { local a i; a=$(sv $1 net.ipv4.conf.all.rp_filter); i=$(sv $1 net/ipv4/conf/$2/rp_filter); echo $(( a > i ? a : i )); }
for p in "r1 $R1_A" "r1 $R1_B" "server $S_C"; do set -- $p; [ "$(eff $1 $2)" = 1 ] || fail A7 "$1/$2 not strict"; done
for ns in client r1 r2 server; do
    [ "$(sv $ns net.ipv4.conf.all.rp_filter)" = 1 ] || fail A7 "$ns conf.all changed"
done
RPINFO="client: $C_A=$(eff client $C_A) $C_D=$(eff client $C_D); r1: $R1_A=$(eff r1 $R1_A) $R1_B=$(eff r1 $R1_B); r2: $R2_B=$(eff r2 $R2_B) $R2_C=$(eff r2 $R2_C) $R2_D=$(eff r2 $R2_D); server: $S_C=$(eff server $S_C)"
pass A7

# ---------- A8 static only + no other interface touched + idempotent
for ns in client r1 r2 server; do
    X $ns ip -4 addr show | grep -q dynamic && fail A8 "$ns dynamic address"
    X $ns ip -4 route show table all | grep -q 'proto \(dhcp\|bird\|zebra\|ospf\|bgp\|rip\|ra\)' && fail A8 "$ns dynamic route"
    o=$(X $ns ip -o link show spare0); [[ $o == *"state DOWN"* && $o != *",UP"* ]] || fail A8 "$ns spare0 touched"
    X $ns ip addr show spare0 | grep -q inet && fail A8 "$ns spare0 got address"
    [ "$(sv $ns net/ipv4/conf/spare0/rp_filter)" = 1 ] || fail A8 "$ns spare0 rp_filter"
done
pgrep -x 'dhclient|dhcpcd|bird|zebra|ospfd|ripd' >/dev/null && fail A8 "daemon running"
before=$(for ns in client r1 r2 server; do X $ns ip -4 route; X $ns ip -4 -br addr; done)
X client bash "$ROOT/netcfg/client.sh" $C_A $C_D && X r1 bash "$ROOT/netcfg/r1.sh" $R1_A $R1_B && \
X r2 bash "$ROOT/netcfg/r2.sh" $R2_B $R2_C $R2_D && X server bash "$ROOT/netcfg/server.sh" $S_C || fail A8 "re-run failed"
after=$(for ns in client r1 r2 server; do X $ns ip -4 route; X $ns ip -4 -br addr; done)
[ "$before" = "$after" ] || fail A8 "re-run changed state"
# scripts use only ip and sysctl
for f in client r1 r2 server; do
    bad=$(grep -v '^\s*#' "$ROOT/netcfg/$f.sh" | grep -oE '^\s*[a-z_][a-z0-9_-]*' | sed 's/^\s*//' | sort -u | grep -vxE 'ip|sysctl|set|if|then|fi|echo|exit|[A-Z_]+|else|elif' )
    [ -z "$bad" ] || fail A8 "$f.sh uses: $bad"
done
pass A8

echo "----------------------------------------"
all=0
for r in A1 A2 A3 A4 A5 A6 A7 A8; do
    printf '%-4s %s %s\n' "$r" "${RES[$r]}" "${DET[$r]}"
    [ "${RES[$r]}" = PASS ] || all=1
done
echo "rp_filter (effective): $RPINFO"
exit $all
