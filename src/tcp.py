"""TCP over raw IPv4 sockets.

Single threaded: every blocking call (connect, accept, send, recv, close) runs
the event loop itself -- read packets from the raw socket, process them, fire
timers and push out whatever the windows allow.
"""

import collections
import random
import select
import socket
import struct
import time

import packet
from packet import Segment, SYN, ACK, FIN, RST, PSH

MY_MSS = 1460
DEFAULT_PEER_MSS = 536      # RFC 1122 default when the peer sends no MSS option
RCV_WINDOW = 65535

SYN_RETRY = 1.0             # T7: retransmit SYN / SYN-ACK every second ...
HANDSHAKE_GIVEUP = 8.0      # ... and keep trying for at least 6 s
MIN_RTO = 0.2
MAX_RTO = 1.0               # T10: RTO never above 1 s
IDLE_GIVEUP = 10.0          # T10: nothing acknowledged for 10 s
FIN_GIVEUP = 2.0            # T9
MAX_SEND_BUFFER = 1 << 20   # send() blocks while more than this is queued
INITIAL_CWND_SEGS = 10

MOD = 1 << 32


def seq_add(a, n):
    return (a + n) % MOD


def seq_diff(a, b):
    """a - b in sequence space (assumes a is not before b)."""
    return (a - b) % MOD


def seq_lt(a, b):
    return 0 < (b - a) % MOD < (1 << 31)


def seq_le(a, b):
    return a == b or seq_lt(a, b)


class TCPError(Exception):
    pass


class ConnectionRefused(TCPError):
    pass


class ConnectionReset(TCPError):
    pass


class TCPTimeout(TCPError):
    pass


class RecvTimeout(TCPError):
    pass


def source_address_for(dst_ip, dst_port):
    """Ask the kernel routing table which local address reaches dst_ip.
    connect() on a UDP socket sends nothing; it only performs the route lookup."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dst_ip, dst_port or 9))
        return s.getsockname()[0]
    finally:
        s.close()


class RawIO:
    """The raw socket, shared by a listener and the connections it accepts.
    Only packets addressed to `port` are parsed; everything else is skipped
    before the (comparatively expensive) checksum verification."""

    def __init__(self, port):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_RAW,
                                  socket.IPPROTO_TCP)
        # We supply the IPv4 header ourselves.
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
        for opt in (getattr(socket, "SO_RCVBUFFORCE", 33), socket.SO_RCVBUF):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, opt, 4 << 20)
                break
            except OSError:
                continue
        self.sock.setblocking(False)
        self.pending = collections.deque()
        self.ip_id = random.randint(1, 0xFFFF)

    def send(self, seg):
        seg.ip_id = self.ip_id
        self.ip_id = self.ip_id % 0xFFFF + 1     # 1..65535, never 0 (T1)
        data = seg.build()
        for _ in range(3):
            try:
                self.sock.sendto(data, (seg.dst, 0))
                return True
            except (BlockingIOError, InterruptedError):
                select.select([], [self.sock], [], 0.05)
            except OSError:
                # e.g. no route right now; treat it as a lost packet
                return False
        return False

    def poll(self, timeout):
        """Wait up to `timeout` seconds for packets and queue the valid ones."""
        if self.pending:
            return
        if timeout is not None and timeout < 0:
            timeout = 0
        try:
            r, _, _ = select.select([self.sock], [], [], timeout)
        except InterruptedError:
            return
        if not r:
            return
        for _ in range(512):
            try:
                raw = self.sock.recv(65535)
            except (BlockingIOError, InterruptedError):
                break
            if len(raw) < 24:
                continue
            ihl = (raw[0] & 0x0F) * 4
            if len(raw) < ihl + 4 or \
                    struct.unpack("!H", raw[ihl + 2:ihl + 4])[0] != self.port:
                continue
            seg = packet.parse(raw)       # None if malformed / bad checksum (T4)
            if seg is not None:
                self.pending.append(seg)

    def next(self):
        return self.pending.popleft() if self.pending else None

    def close(self):
        self.sock.close()


class TCPConnection:

    def __init__(self, io, log, local_ip, local_port, remote_ip, remote_port,
                 owns_io):
        self.io = io
        self.log = log
        self.owns_io = owns_io
        self.local_ip = local_ip
        self.local_port = local_port
        self.remote_ip = remote_ip
        self.remote_port = remote_port

        self.state = "CLOSED"
        self.error = None
        self.closed = False

        # send side
        self.iss = random.getrandbits(32)        # T6
        self.snd_una = self.iss
        self.snd_nxt = self.iss
        self.snd_max = self.iss                  # highest sequence ever sent
        self.snd_buf = bytearray()               # unacked + unsent data, starts at snd_una
        self.peer_mss = DEFAULT_PEER_MSS
        self.peer_wnd = RCV_WINDOW
        self.fin_sent = False
        self.fin_seq = None
        self.fin_acked = False

        # congestion control (Reno with NewReno partial-ACK handling)
        self.cwnd = 0.0
        self.ssthresh = float(1 << 30)
        self.dupacks = 0
        self.in_recovery = False
        self.recover = None

        # timers
        self.srtt = None
        self.rttvar = None
        self.rto = MAX_RTO
        self.rto_deadline = None
        self.rtt_seq = None
        self.rtt_start = 0.0
        self.last_progress = time.monotonic()
        self.last_heard = time.monotonic()

        # receive side
        self.rcv_nxt = 0
        self.rcv_buf = bytearray()
        self.ooo = {}                            # seq -> payload, out of order (T12)
        self.peer_fin_seq = None
        self.fin_received = False

    # ---------------------------------------------------------------- helpers

    def _mss(self):
        return min(MY_MSS, self.peer_mss)

    def _xmit(self, flags, seq, payload=b"", mss=None):
        ack = self.rcv_nxt if flags & ACK else 0
        seg = Segment(self.local_ip, self.local_port, self.remote_ip,
                      self.remote_port, seq, ack, flags, RCV_WINDOW,
                      bytes(payload), mss)
        if self.io.send(seg) and self.log:
            self.log.sent(seg)

    def _send_ack(self):
        self._xmit(ACK, self.snd_nxt)

    def _matches(self, seg):
        return (seg.src == self.remote_ip and seg.sport == self.remote_port and
                seg.dst == self.local_ip and seg.dport == self.local_port)

    def _flight(self):
        return seq_diff(self.snd_nxt, self.snd_una)

    def _check_error(self):
        if self.error is not None:
            raise self.error

    def _run(self, done, deadline=None):
        """Run the event loop until done() is true (returns True) or the
        deadline passes (returns False). Raises on connection errors."""
        while True:
            if done():
                return True
            self._check_error()
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return False
            wait = 0.5
            if self.rto_deadline is not None:
                wait = min(wait, self.rto_deadline - now)
            if deadline is not None:
                wait = min(wait, deadline - now)
            self.io.poll(max(wait, 0))
            while True:
                seg = self.io.next()
                if seg is None:
                    break
                if self._matches(seg):        # T5
                    if self.log:
                        self.log.received(seg)
                    self.last_heard = time.monotonic()
                    self._on_segment(seg)
                    self._check_error()
            self._on_timer()
            self._push()

    # ------------------------------------------------------------- open/close

    @classmethod
    def connect(cls, dst_ip, dst_port, log=None):
        try:
            src_ip = source_address_for(dst_ip, dst_port)
        except OSError as e:
            raise TCPError("no route to %s: %s" % (dst_ip, e))
        sport = random.randint(61000, 65535)     # T13
        io = RawIO(sport)
        conn = cls(io, log, src_ip, sport, dst_ip, dst_port, owns_io=True)
        try:
            conn._active_open()
        except BaseException:
            conn.abort()
            raise
        return conn

    def _active_open(self):
        self.state = "SYN_SENT"
        start = time.monotonic()
        while True:
            self._xmit(SYN, self.iss, mss=MY_MSS)
            self.snd_nxt = self.snd_max = seq_add(self.iss, 1)
            elapsed = time.monotonic() - start
            if elapsed >= HANDSHAKE_GIVEUP:
                break
            retry_at = min(time.monotonic() + SYN_RETRY, start + HANDSHAKE_GIVEUP)
            if self._run(lambda: self.state == "ESTABLISHED", retry_at):
                return
            if time.monotonic() - start >= HANDSHAKE_GIVEUP:
                break
        raise TCPTimeout("handshake timed out")

    def _passive_open(self, syn):
        """Called by the listener with the SYN that was just received."""
        self.state = "SYN_RCVD"
        self.rcv_nxt = seq_add(syn.seq, 1)
        if syn.mss:
            self.peer_mss = syn.mss
        self.peer_wnd = syn.win
        start = time.monotonic()
        while True:
            self._xmit(SYN | ACK, self.iss, mss=MY_MSS)
            self.snd_nxt = self.snd_max = seq_add(self.iss, 1)
            retry_at = min(time.monotonic() + SYN_RETRY, start + HANDSHAKE_GIVEUP)
            if self._run(lambda: self.state == "ESTABLISHED", retry_at):
                return
            if time.monotonic() - start >= HANDSHAKE_GIVEUP:
                raise TCPTimeout("handshake timed out")

    def _established(self):
        self.state = "ESTABLISHED"
        self.snd_una = seq_add(self.iss, 1)
        self.cwnd = float(INITIAL_CWND_SEGS * self._mss())
        self.last_progress = time.monotonic()

    def close(self):
        """Graceful close (T9). Never raises."""
        if self.closed:
            return
        try:
            if self.state == "ESTABLISHED" and self.error is None:
                # Everything we queued must be acknowledged before the FIN.
                self._run(lambda: not self.snd_buf)
                self._send_fin()
                deadline = time.monotonic() + FIN_GIVEUP
                self._run(lambda: self.fin_acked and self.fin_received, deadline)
        except (TCPError, OSError):
            pass
        finally:
            self.abort()

    def _send_fin(self):
        self.fin_seq = self.snd_nxt
        self.fin_sent = True
        self._xmit(FIN | ACK, self.fin_seq)
        self.snd_nxt = self.snd_max = seq_add(self.fin_seq, 1)
        self.rto = min(self.rto, 0.5)
        self.rto_deadline = time.monotonic() + self.rto

    def abort(self):
        if self.closed:
            return
        self.closed = True
        self.state = "CLOSED"
        if self.owns_io:
            self.io.close()

    # -------------------------------------------------------------- data API

    def send(self, data):
        self._check_error()
        if self.closed or self.fin_sent:
            raise TCPError("connection closed")
        self.snd_buf += data
        self.last_progress = time.monotonic() if self._flight() == 0 \
            else self.last_progress
        self._push()
        self._run(lambda: len(self.snd_buf) <= MAX_SEND_BUFFER)

    def recv(self, max_bytes=65536, timeout=None):
        if self.closed:
            raise TCPError("connection closed")
        deadline = None if timeout is None else time.monotonic() + timeout
        if not self._run(lambda: self.rcv_buf or self.fin_received, deadline):
            raise RecvTimeout("recv timed out")
        if self.rcv_buf:
            out = bytes(self.rcv_buf[:max_bytes])
            del self.rcv_buf[:max_bytes]
            return out
        return b""

    # ------------------------------------------------------- incoming segments

    def _on_segment(self, seg):
        if seg.flags & RST:                      # T8
            if self.state == "SYN_SENT":
                if seg.flags & ACK and seg.ack == seq_add(self.iss, 1):
                    self.error = ConnectionRefused("connection refused")
                    self.abort()
                return
            self.error = ConnectionReset("connection reset by peer")
            self.abort()
            return

        if self.state == "SYN_SENT":
            if seg.flags & SYN and seg.flags & ACK and \
                    seg.ack == seq_add(self.iss, 1):
                self.rcv_nxt = seq_add(seg.seq, 1)
                if seg.mss:
                    self.peer_mss = seg.mss
                self.peer_wnd = seg.win
                self._established()
                self._send_ack()
            return

        if self.state == "SYN_RCVD":
            if seg.flags & SYN and not seg.flags & ACK:
                self._xmit(SYN | ACK, self.iss, mss=MY_MSS)   # our SYN-ACK was lost
                return
            if not (seg.flags & ACK and seg.ack == seq_add(self.iss, 1)):
                return
            self._established()
            # fall through: the ACK may carry the request already

        if seg.flags & SYN:
            # Retransmitted SYN-ACK (our ACK got lost) or SYN: just re-ACK.
            self._send_ack()
            return

        if seg.flags & ACK:
            self._on_ack(seg)
        if seg.payload or seg.flags & FIN:
            self._on_data(seg)

    def _on_ack(self, seg):
        ack = seg.ack
        if seq_lt(self.snd_una, ack) and seq_le(ack, self.snd_max):
            acked = seq_diff(ack, self.snd_una)
            data_acked = acked
            if self.fin_sent and seq_lt(self.fin_seq, ack):
                self.fin_acked = True
                data_acked -= 1
            del self.snd_buf[:data_acked]
            self.snd_una = ack
            if seq_lt(self.snd_nxt, ack):        # can happen after go-back-N
                self.snd_nxt = ack
            self.peer_wnd = seg.win
            now = time.monotonic()
            self.last_progress = now

            if self.rtt_seq is not None and seq_le(self.rtt_seq, ack):
                self._rtt_sample(now - self.rtt_start)
                self.rtt_seq = None

            self._cc_on_new_ack(acked)
            self.dupacks = 0
            self.rto_deadline = now + self.rto if self._flight() else None
            return

        if ack == self.snd_una:
            is_dup = (not seg.payload and not seg.flags & (SYN | FIN) and
                      self._flight() > 0 and seg.win == self.peer_wnd)
            self.peer_wnd = seg.win
            if is_dup:
                self._cc_on_dupack()

    def _on_data(self, seg):
        seq = seg.seq
        data = seg.payload
        if seg.flags & FIN:
            self.peer_fin_seq = seq_add(seq, len(data))

        if data:
            if seq_lt(seq, self.rcv_nxt):
                skip = seq_diff(self.rcv_nxt, seq)
                data = data[skip:] if skip < len(data) else b""
                seq = self.rcv_nxt
            if data:
                if seq == self.rcv_nxt:
                    self.rcv_buf += data
                    self.rcv_nxt = seq_add(self.rcv_nxt, len(data))
                    self._drain_ooo()
                elif seq_diff(seq, self.rcv_nxt) < RCV_WINDOW:
                    old = self.ooo.get(seq)
                    if old is None or len(old) < len(data):
                        self.ooo[seq] = data

        if (self.peer_fin_seq is not None and not self.fin_received and
                self.rcv_nxt == self.peer_fin_seq):
            self.rcv_nxt = seq_add(self.rcv_nxt, 1)
            self.fin_received = True
            self.ooo.clear()

        # T11: ACK every data segment, including duplicates and out-of-order.
        self._send_ack()

    def _drain_ooo(self):
        progress = True
        while progress and self.ooo:
            progress = False
            for s in list(self.ooo):
                d = self.ooo[s]
                end = seq_add(s, len(d))
                if seq_le(end, self.rcv_nxt):
                    del self.ooo[s]              # entirely old
                elif seq_le(s, self.rcv_nxt):
                    del self.ooo[s]
                    self.rcv_buf += d[seq_diff(self.rcv_nxt, s):]
                    self.rcv_nxt = end
                    progress = True

    # ---------------------------------------------------- congestion control

    def _cc_on_new_ack(self, acked):
        mss = self._mss()
        if self.in_recovery:
            if seq_lt(self.snd_una, self.recover):
                # Partial ACK: the next hole is lost too, resend it right away.
                self._retransmit_head()
                self.cwnd = max(self.cwnd - acked + mss, float(mss))
                return
            self.in_recovery = False
            self.cwnd = self.ssthresh
            return
        if self.cwnd < self.ssthresh:
            self.cwnd += min(acked, mss)                 # slow start
        else:
            self.cwnd += mss * mss / self.cwnd           # congestion avoidance

    def _cc_on_dupack(self):
        mss = self._mss()
        self.dupacks += 1
        if self.in_recovery:
            self.cwnd += mss                             # window inflation
            return
        if self.dupacks == 3 and (self.recover is None or
                                  not seq_lt(self.snd_una, self.recover)):
            # Fast retransmit / fast recovery
            self.ssthresh = max(self._flight() / 2, 2.0 * mss)
            self.cwnd = self.ssthresh + 3 * mss
            self.in_recovery = True
            self.recover = self.snd_max
            self._retransmit_head()

    def _retransmit_head(self):
        """Resend the first unacknowledged segment."""
        self.rtt_seq = None                              # Karn's algorithm
        if self.snd_buf:
            n = min(self._mss(), len(self.snd_buf),
                    seq_diff(self.snd_max, self.snd_una))
            if n > 0:
                self._xmit(ACK | PSH, self.snd_una, self.snd_buf[:n])
        elif self.fin_sent and not self.fin_acked:
            self._xmit(FIN | ACK, self.fin_seq)
        self.rto_deadline = time.monotonic() + self.rto

    def _rtt_sample(self, r):
        if self.srtt is None:
            self.srtt = r
            self.rttvar = r / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - r)
            self.srtt = 0.875 * self.srtt + 0.125 * r
        # Like Linux: the variance term is never below 200 ms.
        self.rto = min(self.srtt + max(4 * self.rttvar, MIN_RTO), MAX_RTO)

    # ---------------------------------------------------------------- timers

    def _on_timer(self):
        if self.state != "ESTABLISHED":
            return
        now = time.monotonic()
        stuck = self._flight() > 0 and now - self.last_progress > IDLE_GIVEUP
        if self.snd_buf and self._flight() == 0 and \
                now - max(self.last_progress, self.last_heard) > IDLE_GIVEUP:
            stuck = True                         # peer window stayed closed
        if stuck:
            self.error = TCPTimeout("no acknowledgement for %d s" % IDLE_GIVEUP)
            self.abort()
            return
        if self.rto_deadline is None or now < self.rto_deadline:
            return
        if self._flight() == 0:
            self.rto_deadline = None
            return

        # Retransmission timeout: collapse the window and go back to snd_una.
        mss = self._mss()
        self.ssthresh = max(self._flight() / 2, 2.0 * mss)
        self.cwnd = float(mss)
        self.in_recovery = False
        self.dupacks = 0
        self.recover = self.snd_max
        self.rto = min(self.rto * 2, MAX_RTO)
        if self.fin_sent and not self.snd_buf:
            self.snd_nxt = self.snd_max
        else:
            self.snd_nxt = self.snd_una
        self._retransmit_head()
        if self.snd_buf:
            n = min(mss, len(self.snd_buf), seq_diff(self.snd_max, self.snd_una))
            self.snd_nxt = seq_add(self.snd_una, n)

    def _push(self):
        """Send new data while both windows allow it (T10)."""
        if self.state != "ESTABLISHED" or self.closed or self.fin_sent:
            return
        mss = self._mss()
        while True:
            flight = self._flight()
            unsent = len(self.snd_buf) - flight
            if unsent <= 0:
                return
            cwnd = self.cwnd
            if not self.in_recovery and self.dupacks < 3:
                cwnd += self.dupacks * mss       # limited transmit (RFC 3042)
            room = min(cwnd, self.peer_wnd) - flight
            n = int(min(mss, unsent, room))
            if n <= 0:
                return
            if n < min(mss, unsent) and flight > 0:
                return                           # wait rather than send a runt segment
            seq = self.snd_nxt
            flags = ACK | PSH if n == unsent else ACK
            self._xmit(flags, seq, self.snd_buf[flight:flight + n])
            if flight == 0:
                self.last_progress = time.monotonic()
            self.snd_nxt = seq_add(seq, n)
            if seq_lt(self.snd_max, self.snd_nxt):
                if self.rtt_seq is None:
                    self.rtt_seq = self.snd_nxt
                    self.rtt_start = time.monotonic()
                self.snd_max = self.snd_nxt
            if self.rto_deadline is None:
                self.rto_deadline = time.monotonic() + self.rto


class TCPListener:

    def __init__(self, port, log=None):
        self.port = port
        self.log = log
        self.io = RawIO(port)
        self.last = None        # (conn tuple, rcv_nxt) of the previous connection

    def accept(self):
        while True:
            self.io.poll(1.0)
            while True:
                seg = self.io.next()
                if seg is None:
                    break
                if seg.flags & SYN and not seg.flags & (ACK | RST):
                    if self.log:
                        self.log.received(seg)
                    conn = TCPConnection(self.io, self.log, seg.dst, self.port,
                                         seg.src, seg.sport, owns_io=False)
                    try:
                        conn._passive_open(seg)
                    except TCPError:
                        conn.abort()
                        continue
                    self.last = conn
                    return conn
                self._late_segment(seg)

    def _late_segment(self, seg):
        """A FIN retransmitted by the previous peer (our last ACK was lost):
        acknowledge it so the peer can finish its close."""
        c = self.last
        if c is None or not seg.flags & FIN or seg.flags & RST:
            return
        if (seg.src, seg.sport, seg.dst) != (c.remote_ip, c.remote_port, c.local_ip):
            return
        if self.log:
            self.log.received(seg)
        reply = Segment(c.local_ip, c.local_port, c.remote_ip, c.remote_port,
                        c.snd_max, seq_add(seg.seq, len(seg.payload) + 1),
                        ACK, RCV_WINDOW)
        if self.io.send(reply) and self.log:
            self.log.sent(reply)

    def close(self):
        self.io.close()
