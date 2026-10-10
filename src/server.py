"""HTTP/1.1 file server (Part D) on top of our user-space TCP.

Usage: sudo ./server/run-server [-p PORT] [-d DOCROOT] [--log LOGFILE]

One connection at a time: read the request head, send one response,
close. Nothing a client does should be able to stop the accept loop.
"""

import argparse
import os
import re
import signal
import sys
import time

HEAD_TIMEOUT = 10.0          # seconds from accept to a complete request head
MAX_HEAD = 64 * 1024         # larger heads get a 400
CHUNK = 64 * 1024            # file read / send unit

REASONS = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    501: "Not Implemented",
}

CONTENT_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}
DEFAULT_TYPE = "application/octet-stream"

VERSION_RE = re.compile(r"HTTP/[0-9]\.[0-9]")


def content_type(path):
    ext = os.path.splitext(path)[1].lower()
    return CONTENT_TYPES.get(ext, DEFAULT_TYPE)


def http_date():
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())


def make_header(code, ctype, length):
    lines = [
        "HTTP/1.1 %d %s" % (code, REASONS[code]),
        "Date: " + http_date(),
        "Server: col334-a3",
        "Content-Type: " + ctype,
        "Content-Length: %d" % length,
        "Connection: close",
        "",
        "",
    ]
    return "\r\n".join(lines).encode("ascii")


def error_response(code, head_only=False):
    text = "%d %s" % (code, REASONS[code])
    body = ("<!DOCTYPE html>\n<html><head><title>%s</title></head>"
            "<body><h1>%s</h1></body></html>\n" % (text, text)).encode("ascii")
    header = make_header(code, "text/html", len(body))
    return code, header, (None if head_only else body)


def find_head_end(buf, start=0):
    """Index just past the blank line ending the head, or -1.

    Accepts CRLF CRLF as well as bare LF LF (some hand-typed clients).
    """
    best = -1
    i = buf.find(b"\r\n\r\n", start)
    if i >= 0:
        best = i + 4
    j = buf.find(b"\n\n", start)
    if j >= 0 and (best < 0 or j + 2 < best):
        best = j + 2
    return best


def parse_request_line(head):
    """Return (method, target) or None if the request line is malformed."""
    line = head.split(b"\n", 1)[0]
    if line.endswith(b"\r"):
        line = line[:-1]
    # surrogateescape keeps arbitrary bytes intact for later filesystem use
    line = line.decode("utf-8", "surrogateescape")
    parts = line.split(" ")
    if len(parts) != 3 or not all(parts):
        return None
    method, target, version = parts
    if not VERSION_RE.fullmatch(version):
        return None
    if not target.startswith("/"):
        return None
    return method, target


def resolve(docroot, path):
    """Map a URL path to a regular file under docroot, or None."""
    fs_path = os.path.join(docroot, path.lstrip("/"))
    try:
        if path.endswith("/") or os.path.isdir(fs_path):
            fs_path = os.path.join(fs_path, "index.html")
        if os.path.isfile(fs_path):
            return fs_path
    except (OSError, ValueError):
        # e.g. embedded NUL bytes, over-long names
        pass
    return None


def build_response(head, docroot):
    """Decide the response for a complete request head.

    Returns (status, header_bytes, body) where body is None (HEAD),
    bytes (error pages) or an open binary file that the caller must
    stream and close.
    """
    req = parse_request_line(head)
    if req is None:
        return error_response(400)
    method, target = req
    if method not in ("GET", "HEAD"):
        return error_response(501)
    head_only = method == "HEAD"

    # The query string is not part of the path, so the ".." check and the
    # file mapping both look only at what precedes the first "?".
    path = target.split("?", 1)[0]
    if ".." in path.split("/"):
        return error_response(403, head_only)

    fs_path = resolve(docroot, path)
    if fs_path is None:
        return error_response(404, head_only)
    try:
        f = open(fs_path, "rb")
    except (OSError, ValueError):
        # exists but unreadable (permissions etc.)
        return error_response(404, head_only)
    try:
        size = os.fstat(f.fileno()).st_size
    except OSError:
        f.close()
        return error_response(404, head_only)

    header = make_header(200, content_type(fs_path), size)
    if head_only:
        f.close()
        return 200, header, None
    return 200, header, _SizedFile(f, size)


class _SizedFile:
    """An open file plus the length promised in Content-Length."""

    def __init__(self, f, size):
        self.f = f
        self.size = size

    def chunks(self):
        left = self.size
        while left > 0:
            data = self.f.read(min(CHUNK, left))
            if not data:
                break       # file shrank underneath us; nothing more to send
            left -= len(data)
            yield data

    def close(self):
        self.f.close()


def read_head(conn, deadline):
    """Read from conn until a full request head arrives.

    Returns the head bytes, b"" if it grew past MAX_HEAD, or None if the
    client timed out / closed / errored first.
    """
    buf = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            data = conn.recv(65536, timeout=remaining)
        except Exception:
            # RecvTimeout, ConnectionReset, ... -> no response either way
            return None
        if not data:
            return None
        start = max(0, len(buf) - 3)
        buf += data
        end = find_head_end(buf, start)
        if end >= 0:
            return buf[:end]
        if len(buf) > MAX_HEAD:
            return b""


def handle_connection(conn, docroot, deadline):
    """Serve one request on conn. Returns the status sent, or None.

    The caller is responsible for closing conn.
    """
    head = read_head(conn, deadline)
    if head is None:
        return None
    if head == b"":
        status, header, body = error_response(400)
    else:
        status, header, body = build_response(head, docroot)

    if isinstance(body, _SizedFile):
        try:
            conn.send(header)
            for data in body.chunks():
                conn.send(data)
        finally:
            body.close()
    else:
        conn.send(header + body if body else header)
    return status


def log_line(msg):
    try:
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        sys.stderr.flush()
    except (OSError, ValueError):
        pass        # a broken stderr must not take the server down


def serve(port, docroot, log_path):
    # Imported here so the request logic above can be tested without tcp.py
    import tcp
    import packet

    log = packet.PacketLog(log_path) if log_path else None
    listener = tcp.TCPListener(port, log)
    log_line("serving %s on port %d" % (os.path.abspath(docroot), port))
    try:
        while True:
            try:
                conn = listener.accept()
            except Exception as e:
                log_line("accept failed: %r" % (e,))
                time.sleep(0.1)
                continue
            deadline = time.monotonic() + HEAD_TIMEOUT
            peer = "%s:%s" % (getattr(conn, "remote_ip", "?"),
                              getattr(conn, "remote_port", "?"))
            reset = False
            try:
                status = handle_connection(conn, docroot, deadline)
                log_line("%s -> %s" % (peer, status if status else "no response"))
            except tcp.ConnectionReset:
                reset = True
                log_line("%s reset the connection" % peer)
            except Exception as e:
                log_line("%s error: %r" % (peer, e))
            try:
                if reset:
                    conn.abort()
                else:
                    conn.close()
            except Exception:
                try:
                    conn.abort()
                except Exception:
                    pass
    finally:
        try:
            listener.close()
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Minimal HTTP/1.1 file server")
    ap.add_argument("-p", dest="port", type=int, default=8080)
    ap.add_argument("-d", dest="docroot", default="./www")
    ap.add_argument("--log", dest="log", default=None)
    args = ap.parse_args(argv)
    if not 0 < args.port < 65536:
        ap.error("port out of range")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    # Turn SIGTERM into a normal exit so the listener gets closed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        serve(args.port, args.docroot, args.log)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
