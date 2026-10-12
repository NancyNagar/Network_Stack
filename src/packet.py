import socket
import struct

FIN = 0x01
SYN = 0x02
RST = 0x04
PSH = 0x08
ACK = 0x10
URG = 0x20

IP_HDR_LEN = 20
TCP_HDR_LEN = 20
DEFAULT_TTL = 64


def checksum(data):
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def pseudo_header(src, dst, tcp_len):
    return struct.pack("!4s4sBBH", socket.inet_aton(src), socket.inet_aton(dst),
                       0, socket.IPPROTO_TCP, tcp_len)


class Segment:

    def __init__(self, src, sport, dst, dport, seq=0, ack=0, flags=0,
                 win=65535, payload=b"", mss=None, ttl=DEFAULT_TTL, ip_id=0):
        self.src = src
        self.sport = sport
        self.dst = dst
        self.dport = dport
        self.seq = seq
        self.ack = ack
        self.flags = flags
        self.win = win
        self.payload = payload
        self.mss = mss
        self.ttl = ttl
        self.ip_id = ip_id

    def seg_len(self):
        n = len(self.payload)
        if self.flags & SYN:
            n += 1
        if self.flags & FIN:
            n += 1
        return n

    def build(self):
        options = b""
        if self.mss is not None:
            options = struct.pack("!BBH", 2, 4, self.mss)
        data_offset = (TCP_HDR_LEN + len(options)) // 4

        tcp_hdr = struct.pack("!HHIIBBHHH", self.sport, self.dport,
                              self.seq, self.ack, data_offset << 4,
                              self.flags, self.win, 0, 0) + options
        tcp_len = len(tcp_hdr) + len(self.payload)
        csum = checksum(pseudo_header(self.src, self.dst, tcp_len)
                        + tcp_hdr + self.payload)
        tcp_hdr = tcp_hdr[:16] + struct.pack("!H", csum) + tcp_hdr[18:]

        total_len = IP_HDR_LEN + tcp_len
        ip_hdr = struct.pack("!BBHHHBBH4s4s", (4 << 4) | 5, 0, total_len,
                             self.ip_id, 0x4000,
                             self.ttl, socket.IPPROTO_TCP, 0,
                             socket.inet_aton(self.src),
                             socket.inet_aton(self.dst))
        ip_csum = checksum(ip_hdr)
        ip_hdr = ip_hdr[:10] + struct.pack("!H", ip_csum) + ip_hdr[12:]
        return ip_hdr + tcp_hdr + self.payload

    def log_line(self, direction):
        mss = "-" if self.mss is None else str(self.mss)
        return ("%s %s:%d > %s:%d ttl=%d id=%d seq=%d ack=%d flags=0x%02x "
                "win=%d len=%d mss=%s" % (
                    direction, self.src, self.sport, self.dst, self.dport,
                    self.ttl, self.ip_id, self.seq, self.ack, self.flags,
                    self.win, len(self.payload), mss))


def parse(raw):
    if len(raw) < IP_HDR_LEN:
        return None
    ver_ihl = raw[0]
    if ver_ihl >> 4 != 4:
        return None
    ihl = (ver_ihl & 0x0F) * 4
    if ihl < IP_HDR_LEN or len(raw) < ihl:
        return None
    total_len, ip_id, frag, ttl, proto = struct.unpack("!HHHBB", raw[2:10])
    if proto != socket.IPPROTO_TCP or total_len < ihl or total_len > len(raw):
        return None
    if frag & 0x3FFF:
        return None
    if checksum(raw[:ihl]) != 0:
        return None

    raw = raw[:total_len]
    src = socket.inet_ntoa(raw[12:16])
    dst = socket.inet_ntoa(raw[16:20])
    tcp = raw[ihl:]
    if len(tcp) < TCP_HDR_LEN:
        return None
    if checksum(pseudo_header(src, dst, len(tcp)) + tcp) != 0:
        return None

    sport, dport, seq, ack, off, flags, win = struct.unpack("!HHIIBBH", tcp[:16])
    hdr_len = (off >> 4) * 4
    if hdr_len < TCP_HDR_LEN or hdr_len > len(tcp):
        return None

    mss = None
    opts = tcp[TCP_HDR_LEN:hdr_len]
    i = 0
    while i < len(opts):
        kind = opts[i]
        if kind == 0:
            break
        if kind == 1:
            i += 1
            continue
        if i + 1 >= len(opts):
            break
        length = opts[i + 1]
        if length < 2 or i + length > len(opts):
            break
        if kind == 2 and length == 4:
            mss = struct.unpack("!H", opts[i + 2:i + 4])[0]
        i += length

    return Segment(src, sport, dst, dport, seq, ack, flags, win,
                   tcp[hdr_len:], mss, ttl, ip_id)


class PacketLog:

    def __init__(self, path):
        self.f = open(path, "w")

    def write_line(self, line):
        self.f.write(line + "\n")
        self.f.flush()

    def sent(self, seg):
        self.write_line(seg.log_line("SEND"))

    def received(self, seg):
        self.write_line(seg.log_line("RECV"))
