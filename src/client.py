#!/usr/bin/env python3

import argparse
import re
import socket
import sys
from collections import namedtuple

EXIT_OK = 0
EXIT_HTTP_STATUS = 1
EXIT_FAILURE = 2

RECV_SIZE = 65536
IDLE_TIMEOUT = 30.0

MAX_LINE = 64 * 1024
MAX_HEAD = 256 * 1024
USER_AGENT = "col334-a3-client/1.0"


class URLError(ValueError):
    pass


class ResponseError(Exception):
    pass


URL = namedtuple("URL", "host port target host_header")

_HOST_RE = re.compile(r"[A-Za-z0-9._~-]+")
_IPV4_LIKE_RE = re.compile(r"[0-9.]+")


def parse_url(url):
    url = url.strip()
    sep = url.find("://")
    if sep < 0 or url[:sep].lower() != "http":
        raise URLError("only http:// URLs are supported")
    rest = url[sep + 3:]

    hash_pos = rest.find("#")
    if hash_pos >= 0:
        rest = rest[:hash_pos]

    end = len(rest)
    for ch in "/?":
        pos = rest.find(ch)
        if 0 <= pos < end:
            end = pos
    authority, target = rest[:end], rest[end:]

    if not authority:
        raise URLError("missing host")
    if "@" in authority:
        raise URLError("user info in URLs is not supported")

    host, port = authority, 80
    if ":" in authority:
        host, port_str = authority.rsplit(":", 1)
        if port_str:
            if not port_str.isdigit() or not port_str.isascii():
                raise URLError("bad port: %r" % port_str)
            port = int(port_str)
            if not 1 <= port <= 65535:
                raise URLError("port out of range: %d" % port)

    if not host or not _HOST_RE.fullmatch(host):
        raise URLError("bad host: %r" % host)
    if _IPV4_LIKE_RE.fullmatch(host):
        parts = host.split(".")
        if len(parts) != 4 or not all(p.isdigit() and int(p) <= 255 for p in parts):
            raise URLError("bad IPv4 address: %r" % host)

    if target == "":
        target = "/"
    elif target.startswith("?"):
        target = "/" + target
    if any(ord(c) <= 0x20 or ord(c) >= 0x7F for c in target):
        raise URLError("path contains characters that need escaping")

    return URL(host, port, target, authority)


def build_request(url):
    lines = [
        "GET %s HTTP/1.1" % url.target,
        "Host: %s" % url.host_header,
        "User-Agent: %s" % USER_AGENT,
        "Accept: */*",
        "Connection: close",
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


class Reader:

    def __init__(self, conn, timeout=IDLE_TIMEOUT):
        self.conn = conn
        self.timeout = timeout
        self.buf = bytearray()
        self.eof = False

    def _fill(self):
        if self.eof:
            return False
        data = self.conn.recv(RECV_SIZE, timeout=self.timeout)
        if not data:
            self.eof = True
            return False
        self.buf += data
        return True

    def read_line(self, limit=MAX_LINE):
        start = 0
        while True:
            nl = self.buf.find(b"\n", start)
            if nl >= 0:
                break
            if len(self.buf) > limit:
                raise ResponseError("line too long")
            start = len(self.buf)
            if not self._fill():
                raise ResponseError("connection closed in the middle of a line")
        if nl > limit:
            raise ResponseError("line too long")
        line = bytes(self.buf[:nl])
        del self.buf[:nl + 1]
        if line.endswith(b"\r"):
            line = line[:-1]
        return line

    def read_exact(self, n, write):
        while n > 0:
            if not self.buf and not self._fill():
                raise ResponseError("connection closed with %d body bytes missing" % n)
            piece = bytes(self.buf[:n])
            del self.buf[:len(piece)]
            n -= len(piece)
            write(piece)

    def read_to_eof(self, write):
        while True:
            if self.buf:
                write(bytes(self.buf))
                self.buf.clear()
            if not self._fill():
                return


Head = namedtuple("Head", "status_line version code reason headers")

_STATUS_RE = re.compile(rb"HTTP/(\d)\.(\d) (\d{3})(?: (.*))?")
_TOKEN_RE = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _parse_status_line(line):
    m = _STATUS_RE.fullmatch(line)
    if not m:
        raise ResponseError("malformed status line: %r" % line[:100])
    version = (int(m.group(1)), int(m.group(2)))
    reason = (m.group(4) or b"").decode("latin-1")
    return version, int(m.group(3)), reason


def _read_one_head(reader):
    status_line = reader.read_line()
    version, code, reason = _parse_status_line(status_line)

    headers = {}
    last = None
    total = len(status_line)
    while True:
        line = reader.read_line()
        total += len(line) + 2
        if total > MAX_HEAD:
            raise ResponseError("response head too large")
        if not line:
            break
        if line[:1] in (b" ", b"\t"):
            if last is None:
                raise ResponseError("continuation line before any header")
            values = headers[last]
            values[-1] = values[-1] + " " + line.strip().decode("latin-1")
            continue
        name, colon, value = line.partition(b":")
        if not colon or not _TOKEN_RE.fullmatch(name):
            raise ResponseError("malformed header line: %r" % line[:100])
        last = name.decode("ascii").lower()
        headers.setdefault(last, []).append(value.strip(b" \t").decode("latin-1"))

    return Head(status_line.decode("latin-1"), version, code, reason, headers)


def read_head(reader):
    while True:
        head = _read_one_head(reader)
        if not 100 <= head.code < 200:
            return head


def _header_tokens(headers, name):
    tokens = []
    for value in headers.get(name, []):
        tokens += [t.strip().lower() for t in value.split(",") if t.strip()]
    return tokens


def body_framing(head):
    if 100 <= head.code < 200 or head.code in (204, 304):
        return ("none",)

    codings = _header_tokens(head.headers, "transfer-encoding")
    if codings:
        if codings[-1] == "chunked":
            return ("chunked",)
        return ("close",)

    lengths = _header_tokens(head.headers, "content-length")
    if lengths:
        if len(set(lengths)) != 1 or not lengths[0].isdigit() or not lengths[0].isascii():
            raise ResponseError("invalid Content-Length: %r" % head.headers["content-length"])
        return ("length", int(lengths[0]))

    return ("close",)


_HEX_RE = re.compile(rb"[0-9A-Fa-f]{1,16}")


def _read_chunked(reader, write):
    while True:
        line = reader.read_line()
        size_str = line.split(b";", 1)[0].strip()
        if not _HEX_RE.fullmatch(size_str):
            raise ResponseError("bad chunk size line: %r" % line[:100])
        size = int(size_str, 16)

        if size == 0:
            break
        reader.read_exact(size, write)
        if reader.read_line(limit=2) != b"":
            raise ResponseError("missing CRLF after chunk data")

    try:
        while reader.read_line():
            pass
    except ResponseError:
        pass


def read_body(reader, head, write):
    framing = body_framing(head)
    kind = framing[0]
    if kind == "chunked":
        _read_chunked(reader, write)
    elif kind == "length":
        reader.read_exact(framing[1], write)
    elif kind == "close":
        reader.read_to_eof(write)


class Output:

    def __init__(self, path, stdout):
        self.path = path
        self.stdout = stdout
        self.f = None

    def open(self):
        if self.path is None:
            self.f = self.stdout
        else:
            self.f = open(self.path, "wb")

    def write(self, data):
        self.f.write(data)

    def close(self):
        if self.f is None:
            return
        if self.f is self.stdout:
            self.f.flush()
        else:
            self.f.close()
        self.f = None


def fetch(conn, url, out, stderr):
    conn.send(build_request(url))
    reader = Reader(conn)
    head = read_head(reader)
    stderr.write(head.status_line + "\n")
    stderr.flush()

    out.open()
    read_body(reader, head, out.write)
    out.close()
    return EXIT_OK if 200 <= head.code < 300 else EXIT_HTTP_STATUS


def _tcp_connect(ip, port, log_path):
    import packet
    import tcp
    log = packet.PacketLog(log_path) if log_path else None
    return tcp.TCPConnection.connect(ip, port, log=log)


def _resolve(host):
    return socket.gethostbyname(host)


def run(url_str, out_path=None, log_path=None, connect=_tcp_connect,
        resolve=_resolve, stdout=None, stderr=None):
    if stdout is None:
        stdout = sys.stdout.buffer
    if stderr is None:
        stderr = sys.stderr

    def fail(msg):
        stderr.write("error: %s\n" % msg)
        return EXIT_FAILURE

    try:
        url = parse_url(url_str)
    except URLError as e:
        return fail("invalid URL: %s" % e)

    try:
        ip = resolve(url.host)
    except (OSError, UnicodeError) as e:
        return fail("cannot resolve %s: %s" % (url.host, e))

    try:
        conn = connect(ip, url.port, log_path)
    except Exception as e:
        return fail("cannot connect to %s:%d: %s" % (ip, url.port, _describe(e)))

    out = Output(out_path, stdout)
    try:
        code = fetch(conn, url, out, stderr)
    except ResponseError as e:
        out.close()
        conn.close()
        return fail("bad response: %s" % e)
    except BaseException as e:
        try:
            out.close()
        except OSError:
            pass
        conn.abort()
        if isinstance(e, Exception):
            return fail(_describe(e))
        raise

    conn.close()
    return code


def _describe(e):
    msg = str(e)
    return "%s: %s" % (type(e).__name__, msg) if msg else type(e).__name__


def main(argv=None):
    ap = argparse.ArgumentParser(prog="run-client",
                                 description="Download one URL over our own TCP.")
    ap.add_argument("url")
    ap.add_argument("-o", dest="outfile", help="write the body here instead of stdout")
    ap.add_argument("--log", dest="logfile", help="packet log file")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_FAILURE

    try:
        return run(args.url, args.outfile, args.logfile)
    except KeyboardInterrupt:
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
