"""Unit tests for server.py using a fake connection (no raw sockets needed).

Run: python3 -m unittest test_server -v   (from src/)
"""

import os
import shutil
import tempfile
import time
import unittest

import server


class FakeTimeout(Exception):
    pass


class FakeConn:
    """Feeds scripted recv() results and records everything sent.

    Each item in `pieces` is bytes to return, b"" for EOF, or "timeout"
    to raise like tcp.RecvTimeout would.
    """

    def __init__(self, pieces):
        self.pieces = list(pieces)
        self.sent = []
        self.recv_timeouts = []

    def recv(self, max_bytes=65536, timeout=None):
        self.recv_timeouts.append(timeout)
        if not self.pieces:
            raise FakeTimeout()
        p = self.pieces.pop(0)
        if p == "timeout":
            raise FakeTimeout()
        assert len(p) <= max_bytes
        return p

    def send(self, data):
        self.sent.append(bytes(data))

    def close(self):
        pass

    @property
    def output(self):
        return b"".join(self.sent)


def split_response(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("ascii").split("\r\n")
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return lines[0], headers, body


class ServerTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.files = {
            "index.html": b"<h1>home</h1>",
            "a.txt": b"hello text\n",
            "style.css": b"body{}",
            "app.js": b"x=1",
            "data.json": b"{}",
            "pic.png": b"\x89PNG....",
            "pic.JPG": b"\xff\xd8jpeg",
            "photo.jpeg": b"\xff\xd8jpeg2",
            "anim.gif": b"GIF89a",
            "doc.pdf": b"%PDF-1.4",
            "page.HTM": b"<p>htm</p>",
            "blob.bin": bytes(range(256)),
            "noext": b"plain",
            "sub/index.html": b"<h1>sub</h1>",
            "sub/x.txt": b"sub x",
            "empty/.keep": b"",
            "nested/deeper/file.txt": b"deep",
        }
        for name, data in self.files.items():
            p = os.path.join(self.root, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def request(self, head, pieces=None):
        """Run handle_connection on a raw head; return (status, line, headers, body)."""
        if pieces is None:
            pieces = [head]
        conn = FakeConn(pieces)
        status = server.handle_connection(conn, self.root, time.monotonic() + 5)
        if status is None:
            return None, None, None, conn.output
        line, headers, body = split_response(conn.output)
        return status, line, headers, body

    def get(self, target, method="GET"):
        return self.request(("%s %s HTTP/1.1\r\nHost: x\r\n\r\n" % (method, target)).encode())


class TestPrecedence(ServerTestBase):
    def assertStatus(self, raw, code):
        status, line, headers, body = self.request(raw)
        self.assertEqual(status, code, raw)
        self.assertTrue(line.startswith("HTTP/1.1 %d " % code), line)
        return headers, body

    def test_400_cases(self):
        bad = [
            b"GET x HTTP/1.1\r\n\r\n",                # target without leading /
            b"GET / HTTP/1.1 extra\r\n\r\n",          # four parts
            b"GET /\r\n\r\n",                         # two parts
            b"GET  / HTTP/1.1\r\n\r\n",               # double space
            b"GET / HTTP/1.1 \r\n\r\n",               # trailing space
            b" GET / HTTP/1.1\r\n\r\n",               # leading space
            b"GET / HTTP/11\r\n\r\n",
            b"GET / HTTP/1.10\r\n\r\n",
            b"GET / http/1.1\r\n\r\n",
            b"GET / FTP/1.1\r\n\r\n",
            b"GET\t/\tHTTP/1.1\r\n\r\n",
            b"\r\n\r\n",                              # empty request line
            b"POST x HTTP/1.1\r\n\r\n",               # 400 beats 501
            b"FOO ../x HTTP/1.1\r\n\r\n",             # 400 beats 501 and 403
            b"GET http://h/a.txt HTTP/1.1\r\n\r\n",   # absolute form isn't "/..."
            b"GET \xff\xfe HTTP/1.1\r\n\r\n",         # non-UTF-8 bytes must not crash
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                headers, body = self.assertStatus(raw, 400)
                self.assertEqual(headers["content-type"], "text/html")
                self.assertIn(b"400", body)

    def test_501_cases(self):
        for raw in [b"POST / HTTP/1.1\r\n\r\n",
                    b"get / HTTP/1.1\r\n\r\n",          # methods are case-sensitive
                    b"POST /../x HTTP/1.1\r\n\r\n",     # 501 beats 403
                    b"DELETE /nope HTTP/1.1\r\n\r\n",   # 501 beats 404
                    b"PUT /a.txt HTTP/1.0\r\n\r\n"]:
            with self.subTest(raw=raw):
                self.assertStatus(raw, 501)

    def test_403_cases(self):
        for target in ["/..", "/../a.txt", "/sub/../a.txt", "/sub/..",
                       "/../nonexistent", "/a/b/../../c", "/..?x=1", "/sub/../?q"]:
            with self.subTest(target=target):
                for method in ("GET", "HEAD"):
                    status, line, headers, body = self.get(target, method)
                    self.assertEqual(status, 403)

    def test_dotdot_lookalikes_not_forbidden(self):
        # Only a whole ".." segment counts; these map to (missing) files.
        for target in ["/...", "/..a", "/a..", "/%2e%2e/a.txt", "/sub/.../x"]:
            with self.subTest(target=target):
                self.assertEqual(self.get(target)[0], 404)

    def test_dotdot_in_query_is_ignored(self):
        self.assertEqual(self.get("/a.txt?p=../../etc/passwd")[0], 200)
        self.assertEqual(self.get("/a.txt?/../")[0], 200)

    def test_single_dot_segment(self):
        self.assertEqual(self.get("/./a.txt")[0], 200)

    def test_404_cases(self):
        for target in ["/missing", "/missing.html", "/sub/missing", "/a.txt/",
                       "/empty/", "/empty", "/nested/", "/a.txt/x"]:
            with self.subTest(target=target):
                status, line, headers, body = self.get(target)
                self.assertEqual(status, 404)
                self.assertEqual(line, "HTTP/1.1 404 Not Found")
                self.assertEqual(headers["content-type"], "text/html")
                self.assertEqual(int(headers["content-length"]), len(body))

    def test_200(self):
        status, line, headers, body = self.get("/a.txt")
        self.assertEqual(status, 200)
        self.assertEqual(line, "HTTP/1.1 200 OK")
        self.assertEqual(body, self.files["a.txt"])

    def test_http10_accepted(self):
        status, line, _, body = self.request(b"GET /a.txt HTTP/1.0\r\n\r\n")
        self.assertEqual(status, 200)
        self.assertEqual(line, "HTTP/1.1 200 OK")


class TestMapping(ServerTestBase):
    def test_root_maps_to_index(self):
        status, _, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.files["index.html"])
        self.assertEqual(headers["content-type"], "text/html")

    def test_directory_with_and_without_slash(self):
        for target in ["/sub", "/sub/", "/sub?x=1", "/sub/?x=1"]:
            with self.subTest(target=target):
                status, _, headers, body = self.get(target)
                self.assertEqual(status, 200)
                self.assertEqual(body, self.files["sub/index.html"])

    def test_query_string_stripped(self):
        status, _, _, body = self.get("/sub/x.txt?a=1&b=/2")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"sub x")
        self.assertEqual(self.get("/?")[0], 200)

    def test_nested(self):
        self.assertEqual(self.get("/nested/deeper/file.txt")[3], b"deep")

    def test_double_slash(self):
        self.assertEqual(self.get("//a.txt")[0], 200)

    def test_index_html_is_directory(self):
        os.makedirs(os.path.join(self.root, "weird", "index.html"))
        self.assertEqual(self.get("/weird/")[0], 404)

    def test_broken_symlink(self):
        os.symlink(os.path.join(self.root, "gone"), os.path.join(self.root, "dangling"))
        self.assertEqual(self.get("/dangling")[0], 404)

    def test_symlink_to_file(self):
        os.symlink(os.path.join(self.root, "a.txt"), os.path.join(self.root, "link.txt"))
        status, _, _, body = self.get("/link.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.files["a.txt"])

    def test_nul_byte_in_target(self):
        status = self.request(b"GET /a\x00.txt HTTP/1.1\r\n\r\n")[0]
        self.assertEqual(status, 404)

    def test_very_long_name(self):
        self.assertEqual(self.get("/" + "a" * 5000)[0], 404)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads anything")
    def test_unreadable_file(self):
        p = os.path.join(self.root, "secret.txt")
        with open(p, "wb") as f:
            f.write(b"no")
        os.chmod(p, 0)
        self.assertEqual(self.get("/secret.txt")[0], 404)

    def test_missing_docroot(self):
        conn = FakeConn([b"GET / HTTP/1.1\r\n\r\n"])
        status = server.handle_connection(conn, os.path.join(self.root, "nope"),
                                          time.monotonic() + 5)
        self.assertEqual(status, 404)

    def test_utf8_filename(self):
        name = "café.txt"
        with open(os.path.join(self.root, name), "wb") as f:
            f.write(b"utf8")
        raw = b"GET /" + name.encode("utf-8") + b" HTTP/1.1\r\n\r\n"
        self.assertEqual(self.request(raw)[3], b"utf8")


class TestHeaders(ServerTestBase):
    def test_content_types(self):
        expect = {
            "/a.txt": "text/plain",
            "/index.html": "text/html",
            "/page.HTM": "text/html",
            "/style.css": "text/css",
            "/app.js": "application/javascript",
            "/data.json": "application/json",
            "/pic.png": "image/png",
            "/pic.JPG": "image/jpeg",
            "/photo.jpeg": "image/jpeg",
            "/anim.gif": "image/gif",
            "/doc.pdf": "application/pdf",
            "/blob.bin": "application/octet-stream",
            "/noext": "application/octet-stream",
        }
        for target, ctype in expect.items():
            with self.subTest(target=target):
                status, _, headers, body = self.get(target)
                self.assertEqual(status, 200)
                self.assertEqual(headers["content-type"], ctype)
                self.assertEqual(body, self.files[target[1:]])

    def test_content_type_helper(self):
        self.assertEqual(server.content_type("X.HTML"), "text/html")
        self.assertEqual(server.content_type("x.Jpeg"), "image/jpeg")
        self.assertEqual(server.content_type("x.tar.gz"), "application/octet-stream")
        self.assertEqual(server.content_type(".html"), "application/octet-stream")
        self.assertEqual(server.content_type("dir.txt/file"), "application/octet-stream")

    def test_required_headers_on_every_status(self):
        for raw in [b"GET /a.txt HTTP/1.1\r\n\r\n", b"BAD\r\n\r\n",
                    b"POST / HTTP/1.1\r\n\r\n", b"GET /.. HTTP/1.1\r\n\r\n",
                    b"GET /zz HTTP/1.1\r\n\r\n"]:
            with self.subTest(raw=raw):
                status, line, headers, body = self.request(raw)
                self.assertTrue(line.startswith("HTTP/1.1 "))
                self.assertEqual(headers["connection"], "close")
                self.assertIn("content-type", headers)
                self.assertEqual(int(headers["content-length"]), len(body))

    def test_head_matches_get(self):
        for target in ["/a.txt", "/", "/missing", "/../x", "/pic.png"]:
            with self.subTest(target=target):
                g = self.get(target, "GET")
                h = self.get(target, "HEAD")
                self.assertEqual(g[0], h[0])
                self.assertEqual(g[1], h[1])
                gh = dict(g[2]); hh = dict(h[2])
                gh.pop("date", None); hh.pop("date", None)
                self.assertEqual(gh, hh)
                self.assertEqual(h[3], b"")
                self.assertEqual(int(hh["content-length"]), len(g[3]))

    def test_large_file_streamed_in_chunks(self):
        data = os.urandom(5 * server.CHUNK + 123)
        with open(os.path.join(self.root, "big.bin"), "wb") as f:
            f.write(data)
        conn = FakeConn([b"GET /big.bin HTTP/1.1\r\n\r\n"])
        server.handle_connection(conn, self.root, time.monotonic() + 5)
        self.assertGreater(len(conn.sent), 5)
        self.assertTrue(all(len(s) <= server.CHUNK for s in conn.sent[1:]))
        _, headers, body = split_response(conn.output)
        self.assertEqual(int(headers["content-length"]), len(data))
        self.assertEqual(body, data)

    def test_empty_file(self):
        status, _, headers, body = self.get("/empty/.keep")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-length"], "0")
        self.assertEqual(body, b"")


class TestReading(ServerTestBase):
    def test_head_split_byte_by_byte(self):
        raw = b"GET /a.txt HTTP/1.1\r\nHost: x\r\nUser-Agent: t\r\n\r\n"
        status, _, _, body = self.request(raw, [raw[i:i + 1] for i in range(len(raw))])
        self.assertEqual(status, 200)
        self.assertEqual(body, self.files["a.txt"])

    def test_terminator_split_across_pieces(self):
        pieces = [b"GET /a.txt HTTP/1.1\r", b"\nHost: x\r\n\r", b"\n"]
        self.assertEqual(self.request(None, pieces)[0], 200)

    def test_bare_lf(self):
        self.assertEqual(self.request(b"GET /a.txt HTTP/1.1\nHost: x\n\n")[0], 200)

    def test_extra_bytes_after_head_ignored(self):
        raw = b"GET /a.txt HTTP/1.1\r\n\r\nGARBAGE BODY"
        self.assertEqual(self.request(raw)[0], 200)

    def test_timeout_no_response(self):
        status, _, _, out = self.request(None, [b"GET / HTTP/1.1\r\n", "timeout"])
        self.assertIsNone(status)
        self.assertEqual(out, b"")

    def test_eof_no_response(self):
        status, _, _, out = self.request(None, [b"GET / HTTP/1.1\r\nHost: x\r\n", b""])
        self.assertIsNone(status)
        self.assertEqual(out, b"")

    def test_immediate_eof(self):
        self.assertIsNone(self.request(None, [b""])[0])

    def test_deadline_passed(self):
        conn = FakeConn([b"GET / HTTP/1.1\r\n\r\n"])
        status = server.handle_connection(conn, self.root, time.monotonic() - 1)
        self.assertIsNone(status)
        self.assertEqual(conn.sent, [])

    def test_recv_timeout_shrinks(self):
        conn = FakeConn([b"GET / HTTP/1.1\r\n", b"Host: x\r\n", b"\r\n"])
        server.handle_connection(conn, self.root, time.monotonic() + 10)
        t = conn.recv_timeouts
        self.assertEqual(len(t), 3)
        self.assertTrue(all(0 < x <= 10 for x in t))
        self.assertTrue(t[0] >= t[1] >= t[2])

    def test_slow_drip_hits_deadline(self):
        # A client that keeps sending header lines but never finishes.
        class Dripper(FakeConn):
            def recv(self, max_bytes=65536, timeout=None):
                if timeout is None or timeout <= 0:
                    raise AssertionError("bad timeout")
                time.sleep(0.02)
                return b"X-A: b\r\n"
        conn = Dripper([])
        conn.sent = []
        start = time.monotonic()
        status = server.handle_connection(conn, self.root, start + 0.2)
        self.assertIsNone(status)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_huge_head(self):
        pieces = [b"GET / HTTP/1.1\r\n"] + [b"X: " + b"a" * 8000 + b"\r\n"] * 20
        status, line, headers, body = self.request(None, pieces)
        self.assertEqual(status, 400)
        self.assertEqual(int(headers["content-length"]), len(body))

    def test_huge_head_single_piece(self):
        status = self.request(None, [b"A" * 65536, b"A" * 65536])[0]
        self.assertEqual(status, 400)


class TestFindHeadEnd(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(server.find_head_end(b"a\r\n\r\nb"), 5)
        self.assertEqual(server.find_head_end(b"a\n\nb"), 3)
        self.assertEqual(server.find_head_end(b"a\r\nb\r\n"), -1)
        self.assertEqual(server.find_head_end(b"a\n\r\n"), -1)
        # whichever terminator comes first wins
        self.assertEqual(server.find_head_end(b"a\n\nb\r\n\r\n"), 3)


if __name__ == "__main__":
    unittest.main()
