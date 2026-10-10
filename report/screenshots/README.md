# Screenshots for report.pdf

Drop each PNG here with exactly the file name below. Then rebuild from `report/`:
`pdflatex report.tex && pdflatex report.tex && rm -f report.aux report.log report.out`.
Any missing file shows up in the PDF as a red "SCREENSHOT TODO" box.

The handout says every screenshot must come from the four real VMs (not the namespace testbed),
must be readable, and must show the command that produced it. `IF_A`, `IF_C` etc. stand for the real
interface names on that VM (check with `ip -br link`).

## Preparation (once, after booting)

1. On every VM, set rp_filter to 1 on every interface (the handout's starting state). Then run that VM's
   script as root: `sudo ./netcfg/client.sh IF_A IF_D`, `sudo ./netcfg/r1.sh IF_A IF_B`,
   `sudo ./netcfg/r2.sh IF_B IF_C IF_D`, `sudo ./netcfg/server.sh IF_C`.
2. Kernel-RST drop rules. Piazza allows these, but they are not in netcfg and must be added by hand:
   - client: `sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST --sport 61000:65535 -j DROP`
   - server: `sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST --sport 8080:8083 -j DROP`

   Always start our server with `-p` in 8080-8083. For any other port, change the server rule to match.
3. While the NAT adapter is still attached, install `traceroute`, `curl`, `wget` and `tcpdump`.
4. On the server VM, create the test files:
   `mkdir -p ~/www ~/r4 && cp <repo>/www/index.html ~/www/ ~/r4/ && head -c 300000 /dev/urandom > ~/www/bin300k.bin && cp ~/www/bin300k.bin ~/r4/`

## Files

| File | VM | What to run / show |
|---|---|---|
| `r1_adapters_client.png` | host | Hypervisor network settings of the client VM showing every adapter (LAN A, Link D internal networks). On VirtualBox, a screenshot of each Adapter tab, or `VBoxManage showvminfo client \| grep NIC` on the host. For UTM, the QEMU Arguments pane is acceptable (Piazza). |
| `r1_adapters_r1.png` | host | Same for r1 (LAN A, LAN B). |
| `r1_adapters_r2.png` | host | Same for r2 (LAN B, LAN C, Link D). |
| `r1_adapters_server.png` | host | Same for server (LAN C). |
| `r1_ipaddr_route_client.png` | client | `ip -br addr; ip route` |
| `r1_ipaddr_route_r1.png` | r1 | `ip -br addr; ip route` |
| `r1_ipaddr_route_r2.png` | r2 | `ip -br addr; ip route` |
| `r1_ipaddr_route_server.png` | server | `ip -br addr; ip route` |
| `r2_traceroute_c2s.png` | client | `traceroute -n 10.10.3.10`. Expected hops: 10.10.1.1, 10.10.4.1, 10.10.3.10 |
| `r2_traceroute_s2c.png` | server | `traceroute -n 10.10.1.10`. Expected hops: 10.10.3.1, 10.10.1.10 |
| `r3_rpfilter.png` | all four | `sysctl -a 2>/dev/null \| grep '\.rp_filter'` on each VM (a 2x2 collage, or four terminals in one image). Expected: client IF_A=1, IF_D=2; r1 all 1; r2 IF_B=2, IF_C=1, IF_D=1; server 1; `all`=1 everywhere. |
| `r4_html.png` | server + client | Server: `cd ~/r4 && python3 -m http.server 8000`. Client: `sudo ./client/run-client http://10.10.3.10:8000/index.html -o a.html; echo $?` then `curl -s -o a_curl.html http://10.10.3.10:8000/index.html; md5sum a.html a_curl.html` |
| `r4_binary.png` | client | `sudo ./client/run-client http://10.10.3.10:8000/bin300k.bin -o b.bin; echo $?` then `curl -s -o b_curl.bin http://10.10.3.10:8000/bin300k.bin; md5sum b.bin b_curl.bin; ls -l b.bin` |
| `r5_three_clients.png` | server + client | Server: `md5sum ~/www/bin300k.bin; sudo ./server/run-server -p 8080 -d ~/www`. Client, one after another: `curl -s -o c.bin http://10.10.3.10:8080/bin300k.bin; echo $?`, then `wget -q -O w.bin http://10.10.3.10:8080/bin300k.bin; echo $?`, then `sudo ./client/run-client http://10.10.3.10:8080/bin300k.bin -o o.bin; echo $?`, then `md5sum c.bin w.bin o.bin`. Put the server's md5sum in the same image (split terminal). |
| `r6_capture.png` | server (+ client) | Server terminal 1: `sudo ./server/run-server -p 8080 -d ~/www`. Server terminal 2: `sudo tcpdump -nn -i IF_C 'tcp port 8080'`. Client: `sudo ./client/run-client http://10.10.3.10:8080/index.html -o x.html`. Capture on the server: on the client, sent packets leave via IF_A and replies arrive on IF_D, so one interface only shows half the connection (or use `-i any` on the client). Use a small file so the SYN, SYN-ACK, ACK, request, data and both FINs fit on one screen. |
