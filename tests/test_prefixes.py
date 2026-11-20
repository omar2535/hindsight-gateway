"""One hostname for both: the API under /api, the admin UI under /ui."""

import os
import re
import tempfile
import unittest
import urllib.parse

from helpers import FakeHindsight, raw, request, serve, stop
from hindsight_gateway import admin, proxy
from hindsight_gateway.proxy import strip_prefix
from hindsight_gateway.store import Store

KEY = "seed-alice-key-123456"
A = "/v1/default/banks/hermes-alice"


class StripPrefixTest(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(strip_prefix("/api/v1/x", "/api"), "/v1/x")
        self.assertEqual(strip_prefix("/api", "/api"), "/")
        self.assertEqual(strip_prefix("/v1/x", "/api"), "/v1/x")  # bare paths still work
        self.assertEqual(strip_prefix("/apiv1/x", "/api"), "/apiv1/x")  # not the prefix
        self.assertEqual(strip_prefix("/api/v1/x", ""), "/api/v1/x")


class PrefixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "gw.db"))
        self.store.seed([("alice", "hermes-alice", KEY)])
        self.upstream = serve(FakeHindsight)
        up = f"http://127.0.0.1:{self.upstream.server_port}"
        p = proxy.make_handler(self.store, up, prefix="/api")
        p.log_message = lambda *a: None
        self.gw = serve(p)
        h = admin.make_handler(self.store, "pw", up, secure_cookies=False, prefix="/ui/")
        h.log_message = lambda *a: None
        self.ui = serve(h)
        FakeHindsight.seen.clear()

    def tearDown(self):
        for s in (self.ui, self.gw, self.upstream):
            stop(s)
        self.store._db.close()
        self.tmp.cleanup()

    def api(self, path, key=KEY):
        return request(self.gw.server_port, "GET", path, {"Authorization": f"Bearer {key}"} if key else {})[0]

    def test_api_under_prefix_reaches_hindsight_without_it(self):
        self.assertEqual(self.api("/api" + A + "/stats?x=1"), 200)
        self.assertEqual(FakeHindsight.seen[-1]["path"], A + "/stats?x=1")
        self.assertEqual(self.api("/api/version"), 200)
        self.assertEqual(FakeHindsight.seen[-1]["path"], "/version")
        # The scoped bank list works under the prefix too.
        self.assertEqual(self.api("/api/v1/default/banks"), 200)
        self.assertTrue(FakeHindsight.seen[-1]["path"].startswith("/v1/default/banks?q="))

    def test_bare_paths_still_work(self):
        self.assertEqual(self.api(A + "/stats"), 200)
        self.assertEqual(FakeHindsight.seen[-1]["path"], A + "/stats")

    def test_prefix_does_not_open_anything(self):
        for path in ["/api/v1/default/banks/hermes-bob/stats",
                     "/api/../v1/default/banks/hermes-bob/stats", "/api//v1/default/banks/hermes-alice/stats",
                     "/api/api" + A + "/stats", "/apiv1/default/banks/hermes-alice/stats"]:
            self.assertIn(self.api(path), (403, 404), path)
        status = raw(self.gw.server_port, f"GET /api/{A}/../hermes-bob/stats HTTP/1.1\r\nHost: h\r\n"
                                          f"Authorization: Bearer {KEY}\r\nConnection: close\r\n\r\n")
        self.assertEqual(status, 403)
        self.assertEqual(self.api("/api" + A + "/stats", key=None), 401)
        self.assertEqual(FakeHindsight.seen, [])

    def test_ui_under_prefix(self):
        port = self.ui.server_port
        status, _, resp = request(port, "GET", "/ui/")
        self.assertEqual((status, resp.getheader("Location")), (303, "/ui/login"))
        status, page, _ = request(port, "GET", "/ui/login")
        page = page.decode()
        self.assertIn('action="/ui/login"', page)
        self.assertIn('href="/ui/static/style.css"', page)
        self.assertEqual(request(port, "GET", "/ui/static/style.css")[0], 200)
        body = urllib.parse.urlencode({"password": "pw"}).encode()
        status, _, resp = request(port, "POST", "/ui/login",
                                  {"Content-Type": "application/x-www-form-urlencoded"}, body)
        self.assertEqual((status, resp.getheader("Location")), (303, "/ui/"))
        cookie = resp.getheader("Set-Cookie")
        self.assertIn("Path=/ui/", cookie)
        status, page, _ = request(port, "GET", "/ui/", {"Cookie": cookie.split(";")[0]})
        page = page.decode()
        self.assertEqual(status, 200)
        links = re.findall(r'(?:href|action)="([^"]+)"', page)
        self.assertTrue(links)
        self.assertTrue(all(l.startswith("/ui/") for l in links), [l for l in links if not l.startswith("/ui/")])


if __name__ == "__main__":
    unittest.main()
