#!/bin/bash
# test/fairness.sh -- T14 fairness: N x (our client <- our server) vs N x (curl <- python3 -m http.server)
# sharing one bottleneck. Run inside the a3test container:
#   docker run --rm --privileged --network none -v "$ROOT":/a3 a3test bash /a3/test/fairness.sh
# Env: NS="1 2" (N values), WARM=5 (s, ignored warm-up), DUR=20 (s, measurement window), SIZE=80M
#
# Bottleneck (Piazza: "10 Mbit/s, 10 ms delay, 30-packet queue on Link D (r2->client),
# plus 10 ms delay on r1->r2"), built as:
#   r2 IF_D egress: netem delay 10ms (root, 1:)  ->  tbf rate 10mbit burst 5000 limit 45000 (child 10:)
#                   i.e. 10 ms propagation, then a 10 Mbit/s server with a 45000 B (= 30 x 1500 B) FIFO
#   r1 IF_B egress: netem delay 10ms
# (topo.sh disables TSO/GSO/GRO so kernel TCP cannot sneak 64 KB super-packets through as "1 packet".)
# Throughput per flow = bytes received by the client on IF_D from that flow's server port (iptables
# INPUT counters in the client ns, i.e. after the bottleneck) over the common window [WARM, WARM+DUR] while all 2N flows are active.
# Pass if mean(ours)/mean(curl) is within [0.5, 2].
set -u
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
NS="${NS:-1 2}"; WARM="${WARM:-5}"; DUR="${DUR:-20}"; SIZE="${SIZE:-80M}"
trap cleanup EXIT INT TERM
mkdir -p "$RES" "$WORK"; touch "$SUMMARY"
echo "nameserver 127.0.0.1" >/etc/resolv.conf 2>/dev/null || true

if ! have_client || ! have_server; then result e_fairness SKIP "launchers missing"; exit 0; fi

if [ "${NO_TOPO:-0}" != 1 ]; then bash "$ROOT/test/topo.sh" >/dev/null || exit 1; fi
FDOC="$WORK/fdoc"; mkdir -p "$FDOC"; [ -f "$FDOC/big.bin" ] || head -c "$SIZE" /dev/zero >"$FDOC/big.bin"

bottleneck() {
    X r1 tc qdisc replace dev r1-b root netem delay 10ms limit 1000
    X r2 tc qdisc del dev r2-d root 2>/dev/null
    X r2 tc qdisc add dev r2-d root handle 1: netem delay 10ms limit 1000
    X r2 tc qdisc add dev r2-d parent 1:1 handle 10: tbf rate 10mbit burst 5000 limit 45000
}
counters() { # prints "port bytes" per flow: bytes arriving at the client on IF_D (after the bottleneck)
    X client iptables -L INPUT -vnx | awk '$0 ~ /spt:/ {for(i=1;i<=NF;i++) if($i ~ /^spt:/){sub("spt:","",$i); print $i, $2}}'
}

for N in $NS; do
    name="e_fairness_N$N"; D="$RES/$name"; rm -rf "$D"; mkdir -p "$D"
    bottleneck
    X client iptables -F INPUT
    ports_our=(); ports_std=()
    for i in $(seq 0 $((N-1))); do
        po=$((8080+i)); ps=$((9080+i)); ports_our+=($po); ports_std+=($ps)
        X client iptables -A INPUT -i c-d -p tcp --sport $po
        X client iptables -A INPUT -i c-d -p tcp --sport $ps
        bg server "$D/server_$po.stdout" "$RUNS" -p $po -d "$FDOC" --log "$D/server_$po.log"
        bg server "$D/httpserver_$ps.txt" python3 -m http.server $ps --bind 0.0.0.0 --directory "$FDOC"
    done
    sleep 1.5
    T=$((WARM+DUR+30))
    for i in $(seq 0 $((N-1))); do
        bg client "$D/client_${ports_our[$i]}.stderr" timeout $T "$RUNC" "http://$SIP:${ports_our[$i]}/big.bin" -o "$WORK/out_our_$i" --log "$D/client_${ports_our[$i]}.log"
        bg client "$D/curl_${ports_std[$i]}.stderr" timeout $T curl -s -o "$WORK/out_std_$i" "http://$SIP:${ports_std[$i]}/big.bin"
    done
    sleep "$WARM"; counters >"$D/c0.txt"
    sleep "$DUR";  counters >"$D/c1.txt"
    killbg
    X r2 tc -s qdisc show dev r2-d >"$D/qdisc.txt"
    out=$(python3 - "$D" "$DUR" "${ports_our[*]}" "${ports_std[*]}" <<'PY'
import sys
d, dur = sys.argv[1], float(sys.argv[2])
our = [int(p) for p in sys.argv[3].split()]; std = [int(p) for p in sys.argv[4].split()]
rd = lambda f: {int(a): int(b) for a, b in (l.split() for l in open(f) if l.strip())}
c0, c1 = rd(d + "/c0.txt"), rd(d + "/c1.txt")
mbps = {p: (c1.get(p, 0) - c0.get(p, 0)) * 8 / dur / 1e6 for p in our + std}
mo = sum(mbps[p] for p in our) / len(our); ms = sum(mbps[p] for p in std) / len(std)
ratio = mo / ms if ms > 0 else float("inf")
x = list(mbps.values()); jain = sum(x) ** 2 / (len(x) * sum(v * v for v in x)) if any(x) else 0
lines = ["port %d %-4s %.2f Mbit/s" % (p, "ours" if p in our else "curl", mbps[p]) for p in our + std]
open(d + "/flows.txt", "w").write("\n".join(lines) + "\nratio=%.2f jain=%.3f total=%.2f\n" % (ratio, jain, sum(x)))
ok = 0.5 <= ratio <= 2.0
print("%d ratio=%.2f jain=%.3f total=%.2fMbit/s ours=[%s] curl=[%s]" % (0 if ok else 1, ratio, jain, sum(x),
      " ".join("%.2f" % mbps[p] for p in our), " ".join("%.2f" % mbps[p] for p in std)))
PY
)
    result "$name" "${out%% *}" "${out#* }"
    X client iptables -F INPUT
done
netem_clear
