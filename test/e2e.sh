#!/bin/bash
# test/e2e.sh -- end-to-end tests of client/run-client and server/run-server on
# the namespace topology. Run inside the a3test container:
#   docker run --rm --privileged --network none -v "$ROOT":/a3 a3test bash /a3/test/e2e.sh [std] [our] [impair] [logs]
# (no args = all). Results: test/results/<name>/..., test/results/summary.jsonl
set -u
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SEL="${*:-std our impair logs}"
want() { [[ " $SEL " == *" $1 "* ]]; }
trap cleanup EXIT INT TERM

mkdir -p "$RES" "$WORK"
: >"$SUMMARY"
bash "$ROOT/test/topo.sh" >/dev/null || { echo "topology failed"; exit 1; }
grep -q 'server.a3' /etc/hosts || echo "10.10.3.10 server.a3" >>/etc/hosts
# no DNS in the sandbox: make lookups fail fast (port 53 on lo is closed)
echo "nameserver 127.0.0.1" >/etc/resolv.conf 2>/dev/null || true
make_docroot
sleep 1

# rc NAME URL [EXTRA...] : run our client; sets RC, OUT, D (result dir), ELAPSED
runclient() {
    local name=$1 url=$2; shift 2
    D="$RES/$name"; mkdir -p "$D"; OUT="$D/out.bin"; rm -f "$OUT"
    local t0=$(now)
    X client timeout "${CTO:-60}" "$RUNC" "$url" -o "$OUT" --log "$D/client.log" "$@" 2>"$D/stderr.txt" >"$D/stdout.txt"
    RC=$?
    ELAPSED=$(since "$t0")
}
el() { echo "$ELAPSED"; }

# =========================== (a) standard servers ===========================
if want std; then
  if ! have_client; then result a_std SKIP "client/run-client missing"; else
    mkdir -p "$RES/a_std_servers"
    bg server "$RES/a_std_servers/http_server.txt" python3 -m http.server 80 --bind 0.0.0.0 --directory "$DOC"
    bg server "$RES/a_std_servers/chunked_server.txt" python3 "$ROOT/test/chunked_server.py" 8000 "$DOC"
    wait_http "http://$SIP/" && wait_http "http://$SIP:8000/echo" || echo "WARN: std servers not up"

    # name url expected_rc [expected_md5_file]
    stdcase() {
        local name=$1 url=$2 erc=$3 ref=${4:-}
        runclient "$name" "$url"
        X client curl -s -o "$D/curl.bin" "$url"
        local m=$(md5 "$OUT") mc=$(md5 "$D/curl.bin") ok=0 det="rc=$RC(exp $erc) $(el)"
        [ "$RC" = "$erc" ] || ok=1
        if [ "$erc" != 2 ]; then
            [ -n "$m" ] && [ "$m" = "$mc" ] || { ok=1; det="$det md5 ours=$m curl=$mc"; }
            [ -z "$ref" ] || [ "$m" = "$(md5 "$ref")" ] || { ok=1; det="$det md5!=original"; }
            grep -qE 'HTTP/1\.[01] [0-9]{3}' "$D/stderr.txt" || { ok=1; det="$det no status line on stderr"; }
        fi
        result "$name" $ok "$det"
    }
    stdcase a_std_index      "http://$SIP/index.html"          0 "$DOC/index.html"
    stdcase a_std_root       "http://$SIP/"                    0 "$DOC/index.html"
    stdcase a_std_bin300k    "http://$SIP/bin300k.bin"         0 "$DOC/bin300k.bin"
    stdcase a_std_bin5m      "http://$SIP/bin5m.bin"           0 "$DOC/bin5m.bin"
    stdcase a_std_query      "http://$SIP/index.html?q=tcp"    0 "$DOC/index.html"
    stdcase a_std_hostname   "http://server.a3/bin300k.bin"    0 "$DOC/bin300k.bin"
    stdcase a_std_404        "http://$SIP/nope.html"           1
    stdcase a_std_301        "http://$SIP/sub"                 1
    stdcase a_std_500        "http://$SIP:8000/status/500"     1
    stdcase a_std_chunked    "http://$SIP:8000/chunked/bin300k.bin"   0 "$DOC/bin300k.bin"
    stdcase a_std_chunked_lc "http://$SIP:8000/chunkedlc/bin1m.bin"   0 "$DOC/bin1m.bin"
    stdcase a_std_chunked_empty "http://$SIP:8000/chunked/empty.txt"  0 "$DOC/empty.txt"
    stdcase a_std_close      "http://$SIP:8000/close/bin300k.bin"     0 "$DOC/bin300k.bin"
    stdcase a_std_close_empty "http://$SIP:8000/close/empty.txt"      0 "$DOC/empty.txt"

    # request format: target, Host, Connection: close, no Accept-Encoding
    runclient a_std_request "http://server.a3:8000/echo/p?q=tcp&x=1"
    ok=0; det="rc=$RC"
    grep -q $'^GET /echo/p?q=tcp&x=1 HTTP/1.1\r$' "$OUT" || { ok=1; det="$det bad request line"; }
    grep -qi $'^host: server.a3:8000\r$' "$OUT" || { ok=1; det="$det bad Host"; }
    grep -qi $'^connection: close\r$' "$OUT" || { ok=1; det="$det no Connection: close"; }
    grep -qi '^accept-encoding' "$OUT" && { ok=1; det="$det has Accept-Encoding"; }
    result a_std_request $ok "$det"

    # stdout output (no -o)
    D="$RES/a_std_stdout"; mkdir -p "$D"
    X client timeout 60 "$RUNC" "http://$SIP/bin300k.bin" --log "$D/client.log" >"$D/stdout.bin" 2>"$D/stderr.txt"; RC=$?
    [ "$RC" = 0 ] && [ "$(md5 "$D/stdout.bin")" = "$(md5 "$DOC/bin300k.bin")" ]
    result a_std_stdout $? "rc=$RC"

    # exit code 2 cases
    errcase() {
        local name=$1; shift
        D="$RES/$name"; mkdir -p "$D"
        local t0=$(date +%s)
        X client timeout 60 "$RUNC" "$@" -o "$D/out.bin" 2>"$D/stderr.txt" >/dev/null; RC=$?
        result "$name" $([ "$RC" = 2 ] && echo 0 || echo 1) "rc=$RC (exp 2) $(( $(date +%s) - t0 ))s"
    }
    errcase a_err_refused  "http://$SIP:81/"
    errcase a_err_scheme   "ftp://$SIP/"
    errcase a_err_empty    "http://"
    errcase a_err_garbage  "not a url"
    errcase a_err_badport  "http://$SIP:99999/"
    errcase a_err_dns      "http://no-such-host.invalid/"
    killbg
  fi
fi

# ============================ (b) our server ==============================
start_our_server() { # NAME PORT
    mkdir -p "$RES/$1"
    bg server "$RES/$1/server.stdout" "$RUNS" -p "${2:-8080}" -d "$DOC" --log "$RES/$1/server.log"
    SPID=${BGPIDS[-1]}
    sleep 1
}
alive() { kill -0 "$SPID" 2>/dev/null; }

if want our; then
  if ! have_server; then result b_our SKIP "server/run-server missing"; else
    start_our_server b_our_server 8080
    U="http://$SIP:8080"
    for f in bin300k.bin bin5m.bin; do
        n=${f%.bin}
        D="$RES/b_curl_$n"; mkdir -p "$D"
        X client timeout 60 curl -s -o "$D/out.bin" "$U/$f"; rc=$?
        [ "$(md5 "$D/out.bin")" = "$(md5 "$DOC/$f")" ]; result "b_curl_$n" $? "curl rc=$rc"
        D="$RES/b_wget_$n"; mkdir -p "$D"
        X client timeout 60 wget -q -t 1 -O "$D/out.bin" "$U/$f"; rc=$?
        [ "$(md5 "$D/out.bin")" = "$(md5 "$DOC/$f")" ]; result "b_wget_$n" $? "wget rc=$rc"
        if have_client; then
            runclient "b_ours_$n" "$U/$f"
            [ "$RC" = 0 ] && [ "$(md5 "$OUT")" = "$(md5 "$DOC/$f")" ]; result "b_ours_$n" $? "rc=$RC $(el)"
        fi
    done

    # response semantics via raw kernel-TCP requests from the client ns
    # rawcase NAME PAYLOAD STATUS METHOD CTYPE [BODYLEN]
    rawcase() {
        local name=$1 payload=$2; shift 2
        D="$RES/$name"; mkdir -p "$D"
        X client timeout 30 python3 "$ROOT/test/rawreq.py" $SIP 8080 "$payload" >"$D/resp.bin" 2>"$D/stderr.txt"
        local o; o=$(python3 "$ROOT/test/check_resp.py" "$@" <"$D/resp.bin"); result "$name" $? "$o"
    }
    H='\r\nHost: x\r\nUser-Agent: t\r\n\r\n'
    rawcase b_get_index   "GET /index.html HTTP/1.1$H"   200 GET text/html $(stat -c %s "$DOC/index.html")
    rawcase b_get_root    "GET / HTTP/1.1$H"             200 GET text/html $(stat -c %s "$DOC/index.html")
    rawcase b_get_subdir  "GET /sub/ HTTP/1.1$H"         200 GET text/html $(stat -c %s "$DOC/sub/index.html")
    rawcase b_get_dir_noslash "GET /dir HTTP/1.1$H"      200 GET text/html $(stat -c %s "$DOC/dir/index.html")
    rawcase b_get_query   "GET /a.txt?x=1&y=/.. HTTP/1.1$H" 200 GET text/plain $(stat -c %s "$DOC/a.txt")
    rawcase b_get_http10  "GET /a.txt HTTP/1.0$H"        200 GET text/plain
    rawcase b_get_empty   "GET /empty.txt HTTP/1.1$H"    200 GET text/plain 0
    for e in "B.TXT text/plain" "d.json application/json" "s.css text/css" "j.js application/javascript" \
             "p.png image/png" "p.JPEG image/jpeg" "g.gif image/gif" "f.pdf application/pdf" "bin1m.bin application/octet-stream"; do
        set -- $e; rawcase "b_ctype_${1//./_}" "GET /$1 HTTP/1.1$H" 200 GET "$2" $(stat -c %s "$DOC/$1")
    done
    rawcase b_head        "HEAD /bin300k.bin HTTP/1.1$H" 200 HEAD application/octet-stream 307200
    rawcase b_head_404    "HEAD /nope HTTP/1.1$H"        404 HEAD
    rawcase b_404         "GET /nope.html HTTP/1.1$H"    404 GET text/html
    rawcase b_403_dotdot  "GET /../x HTTP/1.1$H"         403 GET text/html
    rawcase b_403_mid     "GET /sub/../index.html HTTP/1.1$H" 403 GET text/html
    rawcase b_403_vs_404  "GET /nope/../../x HTTP/1.1$H" 403 GET text/html
    rawcase b_501_post    "POST /index.html HTTP/1.1$H"  501 GET text/html
    rawcase b_501_delete  "DELETE /index.html HTTP/1.1$H" 501 GET text/html
    rawcase b_501_before_403 "PUT /../x HTTP/1.1$H"      501 GET text/html
    rawcase b_400_garbage "GARBAGE$H"                    400 GET text/html
    rawcase b_400_target  "GET index.html HTTP/1.1$H"    400 GET text/html
    rawcase b_400_version "GET / FOO/1.1$H"              400 GET text/html
    rawcase b_400_extra   "GET / HTTP/1.1 extra$H"       400 GET text/html
    rawcase b_400_before_501 "POST nope HTTP/1.1$H"      400 GET text/html

    # the same via curl
    D="$RES/b_curl_codes"; mkdir -p "$D"
    c1=$(X client curl -s -o /dev/null -w '%{http_code}' -m 20 "$U/nope")
    c2=$(X client curl -s -o /dev/null -w '%{http_code}' -m 20 --path-as-is "$U/../x")
    c3=$(X client curl -s -o /dev/null -w '%{http_code}' -m 20 -X POST "$U/index.html")
    c4=$(X client curl -s -I -m 20 "$U/bin300k.bin" | tr -d '\r' | tee "$D/head.txt" | head -1)
    [ "$c1$c2$c3" = 404403501 ] && [[ $c4 == "HTTP/1.1 200"* ]] && grep -qi '^content-length: 307200$' "$D/head.txt"
    result b_curl_codes $? "404->$c1 403->$c2 501->$c3 HEAD='$c4'"

    if have_client; then
        runclient b_ours_404 "$U/nope"
        [ "$RC" = 1 ] && grep -q '<' "$OUT"; result b_ours_404 $? "rc=$RC (exp 1)"
    fi

    # incomplete request head: server must close after ~10 s without response
    D="$RES/b_slow_head"; mkdir -p "$D"
    X client timeout 30 python3 "$ROOT/test/rawreq.py" $SIP 8080 'GET /index.html HTTP/1.1\r\nHost: x\r\n' >"$D/resp.bin" 2>"$D/stderr.txt"
    ca=$(sed -n 's/closed_after=//p' "$D/stderr.txt")
    python3 -c "import sys;t=float(sys.argv[1]);sys.exit(0 if 9<=t<=14 else 1)" "${ca:-99}" && [ ! -s "$D/resp.bin" ]
    result b_slow_head $? "closed_after=${ca}s resp_bytes=$(stat -c %s "$D/resp.bin")"

    # client vanishes mid-request; server must survive
    X client timeout 5 python3 -c "import socket;s=socket.create_connection(('$SIP',8080),3);s.send(b'GET / HT');s.close()" 2>/dev/null
    sleep 2
    D="$RES/b_survives"; mkdir -p "$D"
    X client timeout 30 curl -s -o "$D/out.bin" "$U/index.html"
    alive && [ "$(md5 "$D/out.bin")" = "$(md5 "$DOC/index.html")" ]; result b_survives $? "server alive after misbehaving clients"
    killbg
  fi
fi

# ============================ (c) impairment ===============================
if want impair; then
  if ! have_client || ! have_server; then result c_impair SKIP "launchers missing"; else
    declare -a IMP=(
        "loss5|loss 5%"
        "loss15|loss 15%"
        "reorder25|delay 10ms reorder 25% 50%"
        "dup5|duplicate 5%"
        "corrupt2|corrupt 2%"
        "combined|delay 10ms reorder 25% 50% loss 5% duplicate 5% corrupt 1%"
    )
    mkdir -p "$RES/c_servers"
    bg server "$RES/c_servers/http_server.txt" python3 -m http.server 80 --bind 0.0.0.0 --directory "$DOC"
    start_our_server c_our_server 8080
    wait_http "http://$SIP/" || true
    for spec in "${IMP[@]}"; do
        nm=${spec%%|*}; args=${spec#*|}
        netem_set $args
        CTO=150
        for f in bin300k.bin bin1m.bin; do
            n=${f%.bin}
            runclient "c_${nm}_ours_ours_$n" "http://$SIP:8080/$f"
            [ "$RC" = 0 ] && [ "$(md5 "$OUT")" = "$(md5 "$DOC/$f")" ]; result "c_${nm}_ours_ours_$n" $? "rc=$RC $(el) [$args]"
        done
        runclient "c_${nm}_ours_std" "http://$SIP/bin300k.bin"
        [ "$RC" = 0 ] && [ "$(md5 "$OUT")" = "$(md5 "$DOC/bin300k.bin")" ]; result "c_${nm}_ours_std" $? "rc=$RC $(el) [$args]"
        D="$RES/c_${nm}_curl_ours"; mkdir -p "$D"
        t0=$(now)
        X client timeout 150 curl -s -o "$D/out.bin" "http://$SIP:8080/bin300k.bin"; rc=$?
        ELAPSED=$(since "$t0")
        [ "$(md5 "$D/out.bin")" = "$(md5 "$DOC/bin300k.bin")" ]; result "c_${nm}_curl_ours" $? "curl rc=$rc $(el) [$args]"
        CTO=60
        netem_clear
    done
    alive; result c_server_alive $? "our server still running after impairment runs"
    killbg
  fi
fi

# ============================ (d) log format ===============================
if want logs; then
    cl=$(find "$RES" -name client.log -size +0 | sort); sl=$(find "$RES" -name server.log -size +0 | sort)
    if [ -z "$cl$sl" ]; then result d_logs SKIP "no logs"; else
        o=""; ok=0
        [ -n "$cl" ] && { o+=$(python3 "$ROOT/test/validate_logs.py" client $cl) || ok=1; }
        [ -n "$sl" ] && { o+=$(python3 "$ROOT/test/validate_logs.py" server $sl) || ok=1; }
        echo "$o" >"$RES/d_log_problems.txt"
        result d_log_format $ok "$(echo $cl $sl | wc -w) logs; $(echo "$o" | grep -c .) problems (see d_log_problems.txt)"
        # path check via TTL: server->client crosses only r2 (ttl 63), client->server crosses r1,r2 (62)
        o2=""
        for f in $(find "$RES" -path '*b_ours_*' -name client.log; find "$RES" -path '*a_std_bin*' -name client.log); do
            awk '$1=="RECV" && $5!="ttl=63"{print FILENAME": "$0; exit}' "$f"
        done >"$RES/d_ttl_problems.txt"
        [ -f "$RES/b_our_server/server.log" ] && awk '$1=="RECV" && $5!="ttl=62"{print FILENAME": "$0; exit}' "$RES/b_our_server/server.log" >>"$RES/d_ttl_problems.txt"
        nrecv=$(cat $(find "$RES" -path '*b_ours_*' -name client.log) /dev/null | grep -c '^RECV')
        [ ! -s "$RES/d_ttl_problems.txt" ] && [ "$nrecv" -gt 0 ]
        result d_log_ttl_path $? "client RECV ttl=63 (via r2 only), server RECV ttl=62 (via r1,r2); $nrecv client RECV lines checked"
        # first lines of a client log: SYN, SYN-ACK, ACK
        f=$(find "$RES" -path '*b_ours_bin300k*' -name client.log | head -1)
        if [ -n "$f" ]; then
            hs=$(head -3 "$f" | awk '{print $1, $9}' | xargs)
            [ "$hs" = "SEND flags=0x02 RECV flags=0x12 SEND flags=0x10" ]; result d_log_handshake $? "$hs"
        fi
    fi
fi

echo "---- summary: $(grep -c '"pass": true' "$SUMMARY") pass, $(grep -c '"pass": false' "$SUMMARY") fail, $(grep -c '"pass": null' "$SUMMARY") skip ($SUMMARY)"
