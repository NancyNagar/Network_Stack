import collections
import enum
import random
import select
import socket
import struct
import time

import packet
from packet import Segment, SYN, ACK, FIN, RST, PSH

MY_MSS = 1460
DEFAULT_PEER_MSS = 536
RCV_WINDOW = 65535

SYN_RETRY = 0.9
HANDSHAKE_GIVEUP = 8.0
MIN_RTO = 0.2
MAX_RTO = 0.9
IDLE_GIVEUP = 10.0
FIN_GIVEUP = 2.0
MAX_SEND_BUFFER = 1 << 20
INITIAL_CWND_SEGS = 10
SEND_BURST = 4

MOD = 1 << 32


def seq_add(a, n):
    return (a + n) % MOD


def seq_diff(a, b):
    return (a - b) % MOD


def seq_lt(a, b):
    return 0 < (b - a) % MOD < (1 << 31)


def seq_le(a, b):
    return a == b or seq_lt(a, b)


class State(enum.Enum):
    CLOSED = 0
    SYN_SENT = 1
    SYN_RCVD = 2
    ESTABLISHED = 3


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
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dst_ip, dst_port or 9))
        return s.getsockname()[0]
    finally:
        s.close()


class RawIO:

    def __init__(self, port):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_RAW,
                                  socket.IPPROTO_TCP)
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
        self.ip_id = self.ip_id % 0xFFFF + 1
        data = seg.build()
        for _ in range(3):
            try:
                self.sock.sendto(data, (seg.dst, 0))
                return True
            except (BlockingIOError, InterruptedError):
                select.select([], [self.sock], [], 0.05)
            except OSError:
                return False
        return False

    def poll(self, timeout):
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
            seg = packet.parse(raw)
            if seg is not None:
                self.pending.append(seg)

    def next(self):
        return self.pending.popleft() if self.pending else None

    def close(self):
        self.sock.close()


class RttEstimator:
    def __init__(self):
        self.srtt = None
        self.rttvar = None
        self.rto = MAX_RTO

    def observe(self, r):
        if self.srtt is None:
            self.srtt, self.rttvar = r, r / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - r)
            self.srtt = 0.875 * self.srtt + 0.125 * r
        self.rto = min(self.srtt + max(4 * self.rttvar, MIN_RTO), MAX_RTO)

    def back_off(self):
        self.rto = min(self.rto * 2, MAX_RTO)

    def clamp(self, ceiling):
        self.rto = min(self.rto, ceiling)


class NewReno:

    def __init__(self):
        self.cwnd = 0.0
        self.ssthresh = float(1 << 30)
        self.dupacks = 0
        self.in_recovery = False
        self.recover = None

    def open_window(self, mss):
        self.cwnd = float(INITIAL_CWND_SEGS * mss)

    def window(self, mss):
        if self.in_recovery or self.dupacks >= 3:
            return self.cwnd
        return self.cwnd + self.dupacks * mss

    def on_new_ack(self, acked, una, mss):
        self.dupacks = 0
        if self.in_recovery:
            if seq_lt(una, self.recover):
                self.cwnd = max(self.cwnd - acked + mss, float(mss))
                return True
            self.in_recovery = False
            self.cwnd = self.ssthresh
        elif self.cwnd < self.ssthresh:
            self.cwnd += min(acked, mss)
        else:
            self.cwnd += mss * mss / self.cwnd
        return False

    def on_dupack(self, flight, una, snd_max, mss):
        self.dupacks += 1
        if self.in_recovery:
            self.cwnd += mss
            return False
        fresh = self.recover is None or not seq_lt(una, self.recover)
        if self.dupacks != 3 or not fresh:
            return False
        self.ssthresh = max(flight / 2, 2.0 * mss)
        self.cwnd = self.ssthresh + 3 * mss
        self.in_recovery = True
        self.recover = snd_max
        return True

    def on_timeout(self, flight, snd_max, mss):
        self.ssthresh = max(flight / 2, 2.0 * mss)
        self.cwnd = float(mss)
        self.in_recovery = False
        self.dupacks = 0
        self.recover = snd_max


class ReceiveQueue:

    def __init__(self):
        self.nxt = 0
        self.ready = bytearray()
        self.parked = {}
        self.fin_at = None
        self.fin_seen = False

    def absorb(self, seq, data, fin):
        if fin:
            self.fin_at = seq_add(seq, len(data))
        if data:
            self._place(seq, data)
        if self.fin_at is not None and not self.fin_seen and self.nxt == self.fin_at:
            self.nxt = seq_add(self.nxt, 1)
            self.fin_seen = True
            self.parked.clear()

    def _place(self, seq, data):
        if seq_lt(seq, self.nxt):
            skip = seq_diff(self.nxt, seq)
            if skip >= len(data):
                return
            data, seq = data[skip:], self.nxt
        if seq == self.nxt:
            self.ready += data
            self.nxt = seq_add(self.nxt, len(data))
            self._release_parked()
        elif seq_diff(seq, self.nxt) < RCV_WINDOW:
            prev = self.parked.get(seq)
            if prev is None or len(prev) < len(data):
                self.parked[seq] = data

    def _release_parked(self):
        advanced = True
        while advanced and self.parked:
            advanced = False
            for start in list(self.parked):
                chunk = self.parked[start]
                end = seq_add(start, len(chunk))
                if seq_le(end, self.nxt):
                    del self.parked[start]
                elif seq_le(start, self.nxt):
                    del self.parked[start]
                    self.ready += chunk[seq_diff(self.nxt, start):]
                    self.nxt = end
                    advanced = True

    def readable(self):
        return bool(self.ready) or self.fin_seen

    def read(self, limit):
        out = bytes(self.ready[:limit])
        del self.ready[:limit]
        return out


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

        self.state = State.CLOSED
        self.error = None
        self.closed = False

        self.iss = random.getrandbits(32)
        self.snd_una = self.iss
        self.snd_nxt = self.iss
        self.snd_max = self.iss
        self.snd_buf = bytearray()
        self.peer_mss = DEFAULT_PEER_MSS
        self.peer_wnd = RCV_WINDOW
        self.fin_sent = False
        self.fin_seq = None
        self.fin_acked = False

        self.cc = NewReno()
        self.rtt = RttEstimator()
        self.rx = ReceiveQueue()
        self.rto_deadline = None
        self.rtt_seq = None
        self.rtt_start = 0.0
        self.last_progress = time.monotonic()
        self.last_heard = time.monotonic()

        self._by_state = {
            State.SYN_SENT: self._seg_in_syn_sent,
            State.SYN_RCVD: self._seg_in_syn_rcvd,
            State.ESTABLISHED: self._seg_in_established,
        }

    def _mss(self):
        return min(MY_MSS, self.peer_mss)

    def _flight(self):
        return seq_diff(self.snd_nxt, self.snd_una)

    def _arm_timer(self):
        self.rto_deadline = time.monotonic() + self.rtt.rto

    def _matches(self, seg):
        return (seg.src == self.remote_ip and seg.sport == self.remote_port and
                seg.dst == self.local_ip and seg.dport == self.local_port)

    def _xmit(self, flags, seq, payload=b"", mss=None):
        ack = self.rx.nxt if flags & ACK else 0
        seg = Segment(self.local_ip, self.local_port, self.remote_ip,
                      self.remote_port, seq, ack, flags, RCV_WINDOW,
                      bytes(payload), mss)
        if self.io.send(seg) and self.log:
            self.log.sent(seg)

    def _send_ack(self):
        self._xmit(ACK, self.snd_nxt)

    def _fail(self, exc):
        self.error = exc
        self.abort()

    def _run(self, done, deadline=None):
        while True:
            if done():
                return True
            if self.error is not None:
                raise self.error
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return False
            self.io.poll(max(self._sleep_for(now, deadline), 0))
            self._drain_inbox()
            self._on_timer()
            self._push()

    def _sleep_for(self, now, deadline):
        wait = 0.5
        if self.rto_deadline is not None:
            wait = min(wait, self.rto_deadline - now)
        if deadline is not None:
            wait = min(wait, deadline - now)
        return wait

    def _drain_inbox(self):
        while True:
            seg = self.io.next()
            if seg is None:
                return
            if not self._matches(seg):
                continue
            if self.log:
                self.log.received(seg)
            self.last_heard = time.monotonic()
            self._on_segment(seg)
            if self.error is not None:
                raise self.error

    @classmethod
    def connect(cls, dst_ip, dst_port, log=None):
        try:
            src_ip = source_address_for(dst_ip, dst_port)
        except OSError as e:
            raise TCPError("no route to %s: %s" % (dst_ip, e))
        sport = random.randint(61000, 65535)
        conn = cls(RawIO(sport), log, src_ip, sport, dst_ip, dst_port,
                   owns_io=True)
        try:
            conn._handshake(State.SYN_SENT, SYN)
        except BaseException:
            conn.abort()
            raise
        return conn

    def _passive_open(self, syn):
        self.rx.nxt = seq_add(syn.seq, 1)
        if syn.mss:
            self.peer_mss = syn.mss
        self.peer_wnd = syn.win
        self._handshake(State.SYN_RCVD, SYN | ACK)

    def _handshake(self, state, flags):
        self.state = state
        start = time.monotonic()
        while True:
            self._xmit(flags, self.iss, mss=MY_MSS)
            self.snd_nxt = self.snd_max = seq_add(self.iss, 1)
            if time.monotonic() - start >= HANDSHAKE_GIVEUP:
                break
            retry_at = min(time.monotonic() + SYN_RETRY, start + HANDSHAKE_GIVEUP)
            if self._run(lambda: self.state is State.ESTABLISHED, retry_at):
                return
            if time.monotonic() - start >= HANDSHAKE_GIVEUP:
                break
        raise TCPTimeout("handshake timed out")

    def _established(self):
        self.state = State.ESTABLISHED
        self.snd_una = seq_add(self.iss, 1)
        self.cc.open_window(self._mss())
        self.last_progress = time.monotonic()

    def close(self):
        if self.closed:
            return
        try:
            if self.state is State.ESTABLISHED and self.error is None:
                self._run(lambda: not self.snd_buf)
                self._send_fin()
                self._run(lambda: self.fin_acked and self.rx.fin_seen,
                          time.monotonic() + FIN_GIVEUP)
        except (TCPError, OSError):
            pass
        finally:
            self.abort()

    def _send_fin(self):
        self.fin_seq = self.snd_nxt
        self.fin_sent = True
        self._xmit(FIN | ACK, self.fin_seq)
        self.snd_nxt = self.snd_max = seq_add(self.fin_seq, 1)
        self.rtt.clamp(0.5)
        self._arm_timer()

    def abort(self):
        if self.closed:
            return
        self.closed = True
        self.state = State.CLOSED
        if self.owns_io:
            self.io.close()

    def send(self, data):
        if self.error is not None:
            raise self.error
        if self.closed or self.fin_sent:
            raise TCPError("connection closed")
        self.snd_buf += data
        if self._flight() == 0:
            self.last_progress = time.monotonic()
        self._push()
        self._run(lambda: len(self.snd_buf) <= MAX_SEND_BUFFER)

    def recv(self, max_bytes=65536, timeout=None):
        if self.closed:
            raise TCPError("connection closed")
        deadline = None if timeout is None else time.monotonic() + timeout
        if not self._run(self.rx.readable, deadline):
            raise RecvTimeout("recv timed out")
        return self.rx.read(max_bytes)

    def _on_segment(self, seg):
        if seg.flags & RST:
            self._on_reset(seg)
            return
        handler = self._by_state.get(self.state)
        if handler is not None:
            handler(seg)

    def _on_reset(self, seg):
        if self.state is State.SYN_SENT:
            if seg.flags & ACK and seg.ack == seq_add(self.iss, 1):
                self._fail(ConnectionRefused("connection refused"))
            return
        self._fail(ConnectionReset("connection reset by peer"))

    def _seg_in_syn_sent(self, seg):
        if not (seg.flags & SYN and seg.flags & ACK and
                seg.ack == seq_add(self.iss, 1)):
            return
        self.rx.nxt = seq_add(seg.seq, 1)
        if seg.mss:
            self.peer_mss = seg.mss
        self.peer_wnd = seg.win
        self._established()
        self._send_ack()

    def _seg_in_syn_rcvd(self, seg):
        if seg.flags & SYN and not seg.flags & ACK:
            self._xmit(SYN | ACK, self.iss, mss=MY_MSS)
            return
        if seg.flags & ACK and seg.ack == seq_add(self.iss, 1):
            self._established()
            self._seg_in_established(seg)

    def _seg_in_established(self, seg):
        if seg.flags & SYN:
            self._send_ack()
            return
        if seg.flags & ACK:
            self._on_ack(seg)
        if seg.payload or seg.flags & FIN:
            self.rx.absorb(seg.seq, seg.payload, bool(seg.flags & FIN))
            self._send_ack()

    def _on_ack(self, seg):
        ack = seg.ack
        if seq_lt(self.snd_una, ack) and seq_le(ack, self.snd_max):
            self._on_advance(seg)
        elif ack == self.snd_una:
            is_dup = (not seg.payload and not seg.flags & (SYN | FIN) and
                      self._flight() > 0 and seg.win == self.peer_wnd)
            self.peer_wnd = seg.win
            if is_dup and self.cc.on_dupack(self._flight(), self.snd_una,
                                            self.snd_max, self._mss()):
                self._retransmit_head()

    def _on_advance(self, seg):
        ack = seg.ack
        acked = seq_diff(ack, self.snd_una)
        data_acked = acked
        if self.fin_sent and seq_lt(self.fin_seq, ack):
            self.fin_acked = True
            data_acked -= 1
        del self.snd_buf[:data_acked]
        self.snd_una = ack
        if seq_lt(self.snd_nxt, ack):
            self.snd_nxt = ack
        self.peer_wnd = seg.win
        now = time.monotonic()
        self.last_progress = now

        if self.rtt_seq is not None and seq_le(self.rtt_seq, ack):
            self.rtt.observe(now - self.rtt_start)
            self.rtt_seq = None

        if self.cc.on_new_ack(acked, self.snd_una, self._mss()):
            self._retransmit_head()
        self.rto_deadline = now + self.rtt.rto if self._flight() else None

    def _retransmit_head(self):
        self.rtt_seq = None
        if self.snd_buf:
            n = min(self._mss(), len(self.snd_buf),
                    seq_diff(self.snd_max, self.snd_una))
            if n > 0:
                self._xmit(ACK | PSH, self.snd_una, self.snd_buf[:n])
        elif self.fin_sent and not self.fin_acked:
            self._xmit(FIN | ACK, self.fin_seq)
        self._arm_timer()

    def _gave_up_waiting(self, now):
        if self._flight() > 0 and now - self.last_progress > IDLE_GIVEUP:
            return True
        return (bool(self.snd_buf) and self._flight() == 0 and
                now - max(self.last_progress, self.last_heard) > IDLE_GIVEUP)

    def _on_timer(self):
        if self.state is not State.ESTABLISHED:
            return
        now = time.monotonic()
        if self._gave_up_waiting(now):
            self._fail(TCPTimeout("no acknowledgement for %d s" % IDLE_GIVEUP))
            return
        if self.rto_deadline is None or now < self.rto_deadline:
            return
        if self._flight() == 0:
            self.rto_deadline = None
            return
        self._on_rto()

    def _on_rto(self):
        mss = self._mss()
        self.cc.on_timeout(self._flight(), self.snd_max, mss)
        self.rtt.back_off()
        if self.fin_sent and not self.snd_buf:
            self.snd_nxt = self.snd_max
        else:
            self.snd_nxt = self.snd_una
        self._retransmit_head()
        if self.snd_buf:
            n = min(mss, len(self.snd_buf), seq_diff(self.snd_max, self.snd_una))
            self.snd_nxt = seq_add(self.snd_una, n)

    def _wait_for_burst(self, flight, unsent, window, room, mss):
        if flight == 0 or unsent <= (SEND_BURST - 1) * mss:
            return False
        if window < (SEND_BURST + 1) * mss:
            return False
        return room < SEND_BURST * mss

    def _push(self):
        if self.state is not State.ESTABLISHED or self.closed or self.fin_sent:
            return
        mss = self._mss()
        while True:
            flight = self._flight()
            unsent = len(self.snd_buf) - flight
            if unsent <= 0:
                return
            window = min(self.cc.window(mss), self.peer_wnd)
            room = window - flight
            n = int(min(mss, unsent, room))
            if n <= 0 or (n < min(mss, unsent) and flight > 0):
                return
            if self._wait_for_burst(flight, unsent, window, room, mss):
                return
            seq = self.snd_nxt
            self._xmit(ACK | PSH if n == unsent else ACK, seq,
                       self.snd_buf[flight:flight + n])
            if flight == 0:
                self.last_progress = time.monotonic()
            self.snd_nxt = seq_add(seq, n)
            if seq_lt(self.snd_max, self.snd_nxt):
                if self.rtt_seq is None:
                    self.rtt_seq = self.snd_nxt
                    self.rtt_start = time.monotonic()
                self.snd_max = self.snd_nxt
            if self.rto_deadline is None:
                self._arm_timer()


class TCPListener:

    def __init__(self, port, log=None):
        self.port = port
        self.log = log
        self.io = RawIO(port)
        self.last = None

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
