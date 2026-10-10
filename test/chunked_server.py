#!/usr/bin/env python3
"""Test-only HTTP server (uses http.server; NOT part of the submission).

Usage: chunked_server.py PORT DOCROOT
  /chunked/<file>   body of DOCROOT/<file>, Transfer-Encoding: chunked, random
                    chunk sizes, chunk extensions and a trailer
  /chunkedlc/<file> same, header names in odd case ("tRANSFER-eNCODING")
  /close/<file>     body of DOCROOT/<file>, no Content-Length, ends at close
  /echo...          200 text/plain, body = the raw request head received
  /status/<code>    that status code with a small body
"""
import os
import random
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOC = sys.argv[2]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("%s\n" % (fmt % a))

    def _file(self, rel):
        p = os.path.realpath(os.path.join(DOC, rel))
        if not p.startswith(os.path.realpath(DOC)) or not os.path.isfile(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    def do_GET(self):
        path = self.path
        self.close_connection = True
        if path.startswith("/chunked/") or path.startswith("/chunkedlc/"):
            lc = path.startswith("/chunkedlc/")
            body = self._file(path.split("/", 2)[2])
            if body is None:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("cONTENT-tYPE" if lc else "Content-Type", "application/octet-stream")
            self.send_header("tRANSFER-eNCODING" if lc else "Transfer-Encoding", "chunked")
            self.send_header("Trailer", "X-Trailer")
            self.send_header("Connection", "close")
            self.end_headers()
            rnd = random.Random(1)
            i = 0
            while i < len(body):
                n = rnd.choice([1, 7, 100, 1000, 1460, 4096, 9000, 30000])
                c = body[i:i + n]
                ext = ";name=val" if rnd.random() < 0.3 else ""
                self.wfile.write(b"%x%s\r\n" % (len(c), ext.encode()) + c + b"\r\n")
                i += n
            self.wfile.write(b"0\r\nX-Trailer: done\r\n\r\n")
        elif path.startswith("/close/"):
            body = self._file(path.split("/", 2)[2])
            if body is None:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/echo"):
            head = (self.requestline + "\r\n" + "".join(
                "%s: %s\r\n" % kv for kv in self.headers.items()) + "\r\n").encode("latin-1")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(head)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(head)
        elif path.startswith("/status/"):
            code = int(path.split("/")[2])
            body = b"<html><body>status %d</body></html>\n" % code
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), H).serve_forever()
