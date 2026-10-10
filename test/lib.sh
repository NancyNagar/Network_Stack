#!/bin/bash
# test/lib.sh -- shared helpers for e2e.sh / fairness.sh (source it; run as root
# inside the a3test container).
ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RES="$ROOT/test/results"
SUMMARY="$RES/summary.jsonl"
WORK="${WORK:-/tmp/a3work}"
DOC="$WORK/docroot"
RUNC="${RUNC:-$ROOT/client/run-client}"   # override only for harness dry-runs
RUNS="${RUNS:-$ROOT/server/run-server}"
SIP=10.10.3.10
BGPIDS=()

X() { local ns=$1; shift; ip netns exec "$ns" "$@"; }

# bg NS LOGFILE CMD... : start a background process in a namespace
bg() { local ns=$1 log=$2; shift 2; ip netns exec "$ns" "$@" >"$log" 2>&1 & BGPIDS+=($!); }

killbg() {
    local p
    for p in "${BGPIDS[@]}"; do kill "$p" 2>/dev/null; done
    sleep 0.3
    for p in "${BGPIDS[@]}"; do kill -9 "$p" 2>/dev/null; done
    # anything still alive inside the namespaces (server instances, http.server ...)
    for ns in client r1 r2 server; do
        for p in $(ip netns pids "$ns" 2>/dev/null); do kill -9 "$p" 2>/dev/null; done
    done
    BGPIDS=()
}

fix_owner() { [ -d "$RES" ] && chown -R "$(stat -c %u:%g "$ROOT")" "$RES" 2>/dev/null; true; }
cleanup() { killbg; fix_owner; }

# result NAME PASS(0/1 or true/false) DETAIL
result() {
    local name=$1 ok=$2 detail=$3 b=false tag=FAIL
    [[ $ok == 0 || $ok == true ]] && { b=true; tag=PASS; }
    [[ $ok == SKIP ]] && { b=null; tag=SKIP; }
    python3 -c 'import json,sys; print(json.dumps({"name":sys.argv[1],"pass":json.loads(sys.argv[2]),"detail":sys.argv[3]}))' \
        "$name" "$b" "$detail" >>"$SUMMARY"
    printf '%-4s %-40s %s\n' "$tag" "$name" "$detail"
}

md5() { md5sum "$1" 2>/dev/null | cut -d' ' -f1; }

# wait until a TCP port answers a SYN (kernel servers) -- uses curl
wait_http() { local url=$1 i; for i in $(seq 1 50); do X client curl -s -o /dev/null -m 1 "$url" && return 0; sleep 0.2; done; return 1; }

make_docroot() {
    rm -rf "$DOC"; mkdir -p "$DOC/sub" "$DOC/dir"
    printf '<html><head><title>A3</title></head><body><h1>index</h1></body></html>\n' >"$DOC/index.html"
    printf '<html><body>sub index</body></html>\n' >"$DOC/sub/index.html"
    printf '<html><body>dir index</body></html>\n' >"$DOC/dir/index.html"
    echo "hello text" >"$DOC/a.txt"; echo "UPPER" >"$DOC/B.TXT"
    echo '{"k":1}' >"$DOC/d.json"; echo 'body{}' >"$DOC/s.css"; echo 'x=1' >"$DOC/j.js"
    head -c 1000 /dev/urandom >"$DOC/p.png"; head -c 1000 /dev/urandom >"$DOC/p.JPEG"
    head -c 1000 /dev/urandom >"$DOC/g.gif"; head -c 1000 /dev/urandom >"$DOC/f.pdf"
    : >"$DOC/empty.txt"
    head -c 307200 /dev/urandom >"$DOC/bin300k.bin"
    head -c 1048576 /dev/urandom >"$DOC/bin1m.bin"
    head -c 5242880 /dev/urandom >"$DOC/bin5m.bin"
    echo "secret" >"$WORK/x"
}

# netem helpers: impair r1->r2 (r1 IF_B egress) and r2->client (r2 IF_D egress)
netem_set() { # "$*" = netem args
    X r1 tc qdisc replace dev r1-b root netem "$@"
    X r2 tc qdisc replace dev r2-d root netem "$@"
}
netem_clear() {
    X r1 tc qdisc del dev r1-b root 2>/dev/null
    X r2 tc qdisc del dev r2-d root 2>/dev/null
    true
}

have_client() { [ -x "$RUNC" ]; }
now() { date +%s.%N; }
since() { python3 -c "import sys;print('%.2fs'%(float(sys.argv[2])-float(sys.argv[1])))" "$1" "$(now)"; }
have_server() { [ -x "$RUNS" ]; }
