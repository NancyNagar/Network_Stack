"""Unit tests for client.py, using a fake connection instead of real TCP.

Run from src/:  python3 -m unittest test_client -v
"""

import io
import os
import random
import tempfile
import unittest

import client
from client import ResponseError, URLError, parse_url


class FakeTCPError(Exception):
    pass


class FakeConn:
    """Replays a canned response, cut into random small pieces.

    end="fin" returns b"" after the data, end="reset" raises like a RST would,
    end="hang" behaves like a silent server (recv times out).
    """

    def __init__(self, response, seed=0, max_piece=7, end="fin"):
        rng = random.Random(seed)
        self.pieces = []
        i = 0
        while i < len(response):
            n = rng.randint(1, max_piece)
            self.pieces.append(response[i:i + n])
            i += n
        self.end = end
        self.sent = b""
        self.closed = False
        self.aborted = False
        self.recv_calls = 0
        self.eof_reads = 0

    def send(self, data):
        assert not self.closed and not self.aborted
        self.sent += data

    def recv(self, max_bytes=65536, timeout=None):
        assert not self.closed and not self.aborted
        self.recv_calls += 1
        if self.pieces:
            piece = self.pieces.pop(0)
            if len(piece) > max_bytes:
                self.pieces.insert(0, piece[max_bytes:])
                piece = piece[:max_bytes]
            return piece
        if self.end == "reset":
            raise FakeTCPError("connection reset")
        if self.end == "hang":
            raise FakeTCPError("recv timed out")
        self.eof_reads += 1
        return b""

    def close(self):
        self.closed = True

    def abort(self):
        self.aborted = True


def do_run(response, url="http://10.10.3.10:8000/f.bin", seed=0, end="fin",
           out_path=None, max_piece=7):
    """Run the client against a canned response.

    Returns (exit code, body bytes, stderr text, conn).
    """
    conn = FakeConn(response, seed=seed, end=end, max_piece=max_piece)
    stdout = io.BytesIO()
    stderr = io.StringIO()
    resolved = []

    def resolve(host):
        resolved.append(host)
        return "10.10.3.10"

    code = client.run(url, out_path=out_path, connect=lambda ip, port, log: conn,
                      resolve=resolve, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue(), conn


def chunked(body, sizes, ext=b"", trailers=b""):
    out = b""
    i = 0
    for n in sizes:
        piece = body[i:i + n]
        i += n
        out += b"%x%s\r\n%s\r\n" % (len(piece), ext, piece)
    assert i >= len(body)
    return out + b"0" + ext + b"\r\n" + trailers + b"\r\n"


BODY = bytes(range(256)) * 20 + b"\r\n0\r\n\r\nHTTP/1.1 200 OK\r\n"


class URLParsingTests(unittest.TestCase):

    def test_valid_urls(self):
        table = [
            # url, host, port, target, Host header
            ("http://10.10.3.10", "10.10.3.10", 80, "/", "10.10.3.10"),
            ("http://10.10.3.10/", "10.10.3.10", 80, "/", "10.10.3.10"),
            ("http://10.10.3.10:8000/a/b.html", "10.10.3.10", 8000, "/a/b.html", "10.10.3.10:8000"),
            ("http://server:80/x", "server", 80, "/x", "server:80"),
            ("http://Example.COM/Path", "Example.COM", 80, "/Path", "Example.COM"),
            ("HTTP://host/", "host", 80, "/", "host"),
            ("http://host/search?q=tcp&x=1", "host", 80, "/search?q=tcp&x=1", "host"),
            ("http://host:8080?q=1", "host", 8080, "/?q=1", "host:8080"),
            ("http://host/a%20b/?x=%2F", "host", 80, "/a%20b/?x=%2F", "host"),
            ("http://host/page#section", "host", 80, "/page", "host"),
            ("http://host#top", "host", 80, "/", "host"),
            ("http://host:/x", "host", 80, "/x", "host:"),
            ("http://my-host.local:65535//double", "my-host.local", 65535, "//double", "my-host.local:65535"),
            ("http://h:1/", "h", 1, "/", "h:1"),
            ("http://h:08080/", "h", 8080, "/", "h:08080"),
            ("  http://h/x  ", "h", 80, "/x", "h"),
        ]
        for url, host, port, target, host_header in table:
            with self.subTest(url=url):
                u = parse_url(url)
                self.assertEqual((u.host, u.port, u.target, u.host_header),
                                 (host, port, target, host_header))

    def test_invalid_urls(self):
        bad = [
            "", "10.10.3.10/", "host:80/x", "https://host/", "ftp://host/",
            "http:/host/", "http//host/", "http://", "http:///path", "http://:80/",
            "http://host:abc/", "http://host:0/", "http://host:65536/",
            "http://host:-1/", "http://host:99999999999999999999/",
            "http://host:８０/",
            "http://user:pw@host/", "http://[::1]/", "http://ho st/",
            "http://host/a b", "http://host/a\tb", "http://host/\xe9",
            "http://300.1.1.1/", "http://1.2.3/", "http://1.2.3.4.5/",
            "http://ho$t/", "http://host:80:80/", "http://h\n/x",
            "http://h/x\r\nX-Injected: 1",
        ]
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(URLError):
                    parse_url(url)

    def test_invalid_url_exit_code(self):
        for url in ["https://host/", "http://host:99999/", "garbage", "http:///x"]:
            with self.subTest(url=url):
                stderr = io.StringIO()

                def connect(*a):
                    raise AssertionError("should not connect")

                code = client.run(url, connect=connect, resolve=lambda h: "1.2.3.4",
                                  stdout=io.BytesIO(), stderr=stderr)
                self.assertEqual(code, 2)


class RequestTests(unittest.TestCase):

    def test_request_bytes(self):
        resp = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        _, _, _, conn = do_run(resp, url="http://10.10.3.10:8000/search?q=tcp")
        self.assertEqual(conn.sent,
                         b"GET /search?q=tcp HTTP/1.1\r\n"
                         b"Host: 10.10.3.10:8000\r\n"
                         b"User-Agent: " + client.USER_AGENT.encode() + b"\r\n"
                         b"Accept: */*\r\n"
                         b"Connection: close\r\n"
                         b"\r\n")

    def test_request_shape(self):
        resp = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        cases = [
            ("http://Server.Local/", b"/", b"Server.Local"),
            ("http://10.10.3.10", b"/", b"10.10.3.10"),
            ("http://10.10.3.10:80", b"/", b"10.10.3.10:80"),
            ("http://h:8080?a=b", b"/?a=b", b"h:8080"),
        ]
        for url, target, host in cases:
            with self.subTest(url=url):
                _, _, _, conn = do_run(resp, url=url)
                lines = conn.sent.split(b"\r\n")
                self.assertEqual(lines[0], b"GET " + target + b" HTTP/1.1")
                self.assertEqual(lines[1], b"Host: " + host)
                self.assertTrue(conn.sent.endswith(b"\r\n\r\n"))
                self.assertEqual(conn.sent.count(b"\r\n\r\n"), 1)
                self.assertIn(b"\r\nConnection: close\r\n", conn.sent)
                self.assertNotIn(b"accept-encoding", conn.sent.lower())
                self.assertEqual(conn.sent.count(b"GET "), 1)

    def test_dns_lookup_uses_bare_host(self):
        resolved = []
        conn = FakeConn(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        seen = []

        def connect(ip, port, log):
            seen.append((ip, port))
            return conn

        code = client.run("http://www.example.org:8000/x",
                          connect=connect,
                          resolve=lambda h: resolved.append(h) or "1.2.3.4",
                          stdout=io.BytesIO(), stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(resolved, ["www.example.org"])
        self.assertEqual(seen, [("1.2.3.4", 8000)])


class FramingTests(unittest.TestCase):

    def check(self, response, body, code=0, seeds=range(15), **kw):
        for seed in seeds:
            with self.subTest(seed=seed):
                rc, out, err, conn = do_run(response, seed=seed, **kw)
                self.assertEqual(rc, code, err)
                self.assertEqual(out, body)
                self.assertTrue(conn.closed)
                self.assertFalse(conn.aborted)
        return err, conn

    def test_content_length(self):
        resp = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(BODY) + BODY
        err, conn = self.check(resp, BODY)
        self.assertEqual(err, "HTTP/1.1 200 OK\n")

    def test_content_length_does_not_wait_for_fin(self):
        # The server keeps the connection open: we must not read past the body.
        resp = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
        rc, out, _, conn = do_run(resp, end="hang", max_piece=1)
        self.assertEqual((rc, out), (0, b"hello"))
        self.assertTrue(conn.closed)
        self.assertEqual(conn.eof_reads, 0)

    def test_content_length_extra_bytes_ignored(self):
        resp = b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabcdef"
        self.check(resp, b"abc")

    def test_content_length_zero(self):
        self.check(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", b"")

    def test_duplicate_equal_content_length(self):
        resp = b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Length: 3\r\n\r\nabc"
        self.check(resp, b"abc")
        resp = b"HTTP/1.1 200 OK\r\nContent-Length: 3, 3\r\n\r\nabc"
        self.check(resp, b"abc")

    def test_header_names_case_insensitive(self):
        for name in [b"content-length", b"CONTENT-LENGTH", b"Content-length", b"cOnTeNt-LeNgTh"]:
            with self.subTest(name=name):
                resp = b"HTTP/1.1 200 OK\r\n" + name + b":   4  \r\n\r\nabcd"
                self.check(resp, b"abcd", seeds=range(3))
        for name in [b"transfer-encoding", b"TRANSFER-ENCODING", b"Transfer-encoding"]:
            with self.subTest(name=name):
                resp = b"HTTP/1.1 200 OK\r\n" + name + b": Chunked\r\n\r\n" + chunked(b"xyz", [3])
                self.check(resp, b"xyz", seeds=range(3))

    def test_chunked(self):
        resp = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + \
            chunked(BODY, [1, 100, 1000, 4000, len(BODY)])
        self.check(resp, BODY)

    def test_chunked_with_extensions_and_trailers(self):
        resp = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" +
                chunked(BODY, [17, 300, 5000, 2000], ext=b";name=value;foo",
                        trailers=b"X-Checksum: abc\r\nExpires: never\r\n"))
        self.check(resp, BODY)

    def test_chunked_upper_and_padded_sizes(self):
        resp = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"0000A ; ext\r\n0123456789\r\na\r\nABCDEFGHIJ\r\n000\r\n\r\n")
        self.check(resp, b"0123456789ABCDEFGHIJ")

    def test_chunked_beats_content_length(self):
        resp = (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n"
                + chunked(b"hello world", [5, 6]))
        self.check(resp, b"hello world")

    def test_chunked_does_not_wait_for_fin(self):
        resp = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunked(b"abc", [3])
        rc, out, _, conn = do_run(resp, end="hang")
        self.assertEqual((rc, out), (0, b"abc"))
        self.assertTrue(conn.closed)

    def test_chunked_missing_final_crlf_tolerated(self):
        resp = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n"
        self.check(resp, b"abc")

    def test_close_delimited(self):
        resp = b"HTTP/1.0 200 OK\r\nServer: test\r\n\r\n" + BODY
        self.check(resp, BODY)

    def test_close_delimited_empty(self):
        self.check(b"HTTP/1.1 200 OK\r\n\r\n", b"")

    def test_other_transfer_coding_reads_to_close(self):
        resp = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\nContent-Length: 2\r\n\r\nabcdef"
        self.check(resp, b"abcdef")

    def test_bare_lf_line_endings(self):
        resp = b"HTTP/1.1 200 OK\nContent-Length: 3\n\nabc"
        self.check(resp, b"abc")

    def test_no_body_statuses(self):
        for status, code in [(b"204 No Content", 0), (b"304 Not Modified", 1)]:
            with self.subTest(status=status):
                resp = b"HTTP/1.1 " + status + b"\r\nContent-Length: 10\r\n\r\n"
                rc, out, _, conn = do_run(resp, end="hang")
                self.assertEqual((rc, out), (code, b""))
                self.assertTrue(conn.closed)

    def test_100_continue_skipped(self):
        resp = (b"HTTP/1.1 100 Continue\r\n\r\n"
                b"HTTP/1.1 103 Early Hints\r\nLink: </a.css>\r\n\r\n"
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        err, _ = self.check(resp, b"ok")
        self.assertEqual(err, "HTTP/1.1 200 OK\n")

    def test_status_line_without_reason(self):
        err, _ = self.check(b"HTTP/1.1 200\r\nContent-Length: 1\r\n\r\nx", b"x")
        self.assertEqual(err, "HTTP/1.1 200\n")

    def test_folded_header(self):
        resp = b"HTTP/1.1 200 OK\r\nX-Long: a\r\n  b\r\nContent-Length: 1\r\n\r\nx"
        self.check(resp, b"x")

    def test_large_body(self):
        rng = random.Random(1)
        body = bytes(rng.getrandbits(8) for _ in range(300000))
        resp = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body
        self.check(resp, body, seeds=[0], max_piece=1460)
        resp = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunked(body, [8192] * 37)
        self.check(resp, body, seeds=[0], max_piece=1460)


class StatusCodeTests(unittest.TestCase):

    def test_exit_codes(self):
        cases = [(b"200 OK", 0), (b"201 Created", 0), (b"206 Partial Content", 0),
                 (b"299 Whatever", 0), (b"301 Moved Permanently", 1), (b"302 Found", 1),
                 (b"404 Not Found", 1), (b"403 Forbidden", 1), (b"500 Internal Server Error", 1),
                 (b"501 Not Implemented", 1), (b"199 Odd", 1)]
        for status, code in cases:
            if status.startswith(b"1"):
                continue  # interim, tested separately
            with self.subTest(status=status):
                body = b"<html>" + status + b"</html>"
                resp = (b"HTTP/1.1 " + status + b"\r\nContent-Length: %d\r\n"
                        b"Location: http://elsewhere/\r\n\r\n" % len(body) + body)
                rc, out, err, conn = do_run(resp)
                self.assertEqual(rc, code)
                self.assertEqual(out, body)          # body saved even for errors
                self.assertEqual(err, "HTTP/1.1 %s\n" % status.decode())
                self.assertEqual(conn.sent.count(b"GET "), 1)  # no redirect following

    def test_404_to_file(self):
        body = b"<h1>Not Found</h1>"
        resp = b"HTTP/1.1 404 Not Found\r\nContent-Length: %d\r\n\r\n" % len(body) + body
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.html")
            rc, out, _, _ = do_run(resp, out_path=path)
            self.assertEqual(rc, 1)
            self.assertEqual(out, b"")
            with open(path, "rb") as f:
                self.assertEqual(f.read(), body)


class FailureTests(unittest.TestCase):

    def expect_fail(self, response, seeds=range(5), **kw):
        for seed in seeds:
            with self.subTest(seed=seed):
                rc, _, err, conn = do_run(response, seed=seed, **kw)
                self.assertEqual(rc, 2, err)
                self.assertTrue(conn.closed or conn.aborted)
                self.assertIn("error", err)

    def test_truncated_content_length(self):
        self.expect_fail(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort")

    def test_truncated_chunked(self):
        base = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        self.expect_fail(base + b"a\r\nabc")                   # mid chunk
        self.expect_fail(base + b"3\r\nabc\r\n")                # no last chunk
        self.expect_fail(base + b"3\r\nabc")                    # no CRLF after data
        self.expect_fail(base + b"3")                           # mid size line
        self.expect_fail(base)                                  # nothing at all

    def test_malformed_chunks(self):
        base = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        for bad in [b"zz\r\nabc\r\n0\r\n\r\n", b"\r\nabc\r\n0\r\n\r\n",
                    b"3\r\nabcX\r\n0\r\n\r\n", b"-3\r\nabc\r\n0\r\n\r\n",
                    b"0x3\r\nabc\r\n0\r\n\r\n", b"+3\r\nabc\r\n0\r\n\r\n",
                    b"3\r\nabcdefgh\r\n0\r\n\r\n"]:
            with self.subTest(bad=bad):
                self.expect_fail(base + bad, seeds=[0])

    def test_truncated_head(self):
        self.expect_fail(b"")
        self.expect_fail(b"HTTP/1.1 200 OK\r\nContent-Len")
        self.expect_fail(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n")
        self.expect_fail(b"HTTP/1.1 100 Continue\r\n\r\n")

    def test_malformed_status_line(self):
        for line in [b"HTTP/1.1 20 OK", b"HTTP/1.1 OK", b"http/1.1 200 OK", b"HTTP/1.1  200 OK",
                     b"ICY 200 OK", b"<html>", b"HTTP/1.1 2000 OK", b"HTTP/11 200 OK"]:
            with self.subTest(line=line):
                self.expect_fail(line + b"\r\nContent-Length: 0\r\n\r\n", seeds=[0])

    def test_malformed_headers(self):
        for head in [b"NoColonHere\r\n", b"Bad Name: x\r\n", b": empty\r\n",
                     b" folded-first: x\r\n",
                     b"Content-Length: abc\r\n", b"Content-Length: -1\r\n",
                     b"Content-Length: 3\r\nContent-Length: 4\r\n",
                     b"Content-Length: 3, 4\r\n"]:
            with self.subTest(head=head):
                self.expect_fail(b"HTTP/1.1 200 OK\r\n" + head + b"\r\nabcd", seeds=[0])

    def test_reset_mid_body(self):
        resp = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort"
        rc, _, _, conn = do_run(resp, end="reset")
        self.assertEqual(rc, 2)
        self.assertTrue(conn.aborted)
        resp = b"HTTP/1.0 200 OK\r\n\r\nclose-delimited"
        rc, _, _, conn = do_run(resp, end="reset")
        self.assertEqual(rc, 2)

    def test_timeout_waiting_for_response(self):
        rc, _, err, conn = do_run(b"", end="hang")
        self.assertEqual(rc, 2)
        self.assertTrue(conn.aborted)

    def test_dns_failure(self):
        def resolve(host):
            raise OSError(-2, "Name or service not known")
        stderr = io.StringIO()
        rc = client.run("http://no.such.host/", resolve=resolve,
                        connect=lambda *a: self.fail("connected"),
                        stdout=io.BytesIO(), stderr=stderr)
        self.assertEqual(rc, 2)

    def test_connect_failure(self):
        class ConnectionRefused(Exception):
            pass

        def connect(ip, port, log):
            raise ConnectionRefused("RST in handshake")
        rc = client.run("http://10.10.3.10:9/", resolve=lambda h: h, connect=connect,
                        stdout=io.BytesIO(), stderr=io.StringIO())
        self.assertEqual(rc, 2)

    def test_send_failure(self):
        conn = FakeConn(b"")

        def send(data):
            raise FakeTCPError("nothing acked for 10 s")
        conn.send = send
        rc = client.run("http://h/", resolve=lambda h: "1.1.1.1",
                        connect=lambda *a: conn, stdout=io.BytesIO(), stderr=io.StringIO())
        self.assertEqual(rc, 2)
        self.assertTrue(conn.aborted)

    def test_no_file_when_connection_fails(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out")
            rc, _, _, _ = do_run(b"", out_path=path, end="reset")
            self.assertEqual(rc, 2)
            self.assertFalse(os.path.exists(path))

    def test_head_line_limit(self):
        resp = b"HTTP/1.1 200 OK\r\nX: " + b"a" * (client.MAX_LINE + 10)
        self.expect_fail(resp, seeds=[0], max_piece=4096)


class MainTests(unittest.TestCase):

    def test_usage_error_exit_code(self):
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(client.main([]), 2)
            self.assertEqual(client.main(["http://h/", "-x"]), 2)

    def test_invalid_url_via_main(self):
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(client.main(["https://h/"]), 2)


if __name__ == "__main__":
    unittest.main()
