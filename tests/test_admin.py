"""The admin UI: login, CSRF, show-once keys, and that changes take effect on the proxy."""

import os
import re
import tempfile
import unittest
import urllib.parse

from helpers import FakeHindsight, request, serve, stop
from hindsight_gateway import admin, proxy
from hindsight_gateway.store import Store

PASSWORD = "correct horse battery staple"


class Browser:
    """Just enough of a browser: keeps the session cookie, submits forms with their CSRF token."""

    def __init__(self, port):
        self.port, self.cookie = port, ""

    def get(self, path):
        status, body, resp = request(self.port, "GET", path, {"Cookie": self.cookie} if self.cookie else {})
        return status, body.decode(), resp

    def post(self, path, fields, csrf=True):
        fields = dict(fields)
        if csrf:
            fields.setdefault("csrf", self.csrf_token())
        body = urllib.parse.urlencode(fields).encode()
        h = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.cookie:
            h["Cookie"] = self.cookie
        status, data, resp = request(self.port, "POST", path, h, body)
        sc = resp.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0]
        return status, data.decode(), resp

    def csrf_token(self):
        _, page, _ = self.get("/")
        m = re.search(r'name="csrf" value="([^"]+)"', page)
        return m.group(1) if m else ""

    def login(self, password=PASSWORD):
        return self.post("/login", {"password": password}, csrf=False)


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "gw.db"))
        self.store.seed([("alice", "hermes-alice", "seed-alice-key-123456")])
        self.upstream = serve(FakeHindsight)
        up = f"http://127.0.0.1:{self.upstream.server_port}"
        h = admin.make_handler(self.store, PASSWORD, up, secure_cookies=False)
        h.log_message = lambda *a: None
        self.ui = serve(h)
        p = proxy.make_handler(self.store, up)
        p.log_message = lambda *a: None
        self.gw = serve(p)
        self.b = Browser(self.ui.server_port)

    def tearDown(self):
        for s in (self.ui, self.gw, self.upstream):
            stop(s)
        self.store._db.close()
        self.tmp.cleanup()

    def proxy_status(self, key, bank="hermes-alice"):
        return request(self.gw.server_port, "GET", f"/v1/default/banks/{bank}/stats",
                       {"Authorization": f"Bearer {key}"})[0]

    # --- login

    def test_everything_needs_login(self):
        for path in ("/", "/keys/1", "/keys/1/rotate"):
            status, _, resp = self.b.get(path)
            self.assertEqual((status, resp.getheader("Location")), (303, "/login"), path)
        status, _, _ = self.b.post("/keys", {"name": "x", "bank": "hermes-x", "csrf": "x"}, csrf=False)
        self.assertEqual(status, 303)
        self.assertEqual([k.name for k in self.store.keys()], ["alice"])

    def test_wrong_password_and_throttle(self):
        for _ in range(5):
            self.assertEqual(self.b.login("nope")[0], 401)
        self.assertEqual(self.b.login(PASSWORD)[0], 429)  # blocked even with the right one
        self.assertEqual(self.b.cookie, "")

    def test_login_logout_and_cookie_flags(self):
        status, _, resp = self.b.login()
        self.assertEqual(status, 303)
        flags = resp.getheader("Set-Cookie")
        self.assertIn("HttpOnly", flags)
        self.assertIn("SameSite=Strict", flags)
        self.assertEqual(self.b.get("/")[0], 200)
        self.b.post("/logout", {})
        self.assertEqual(self.b.get("/")[0], 303)

    def test_secure_flag_on_by_default(self):
        h = admin.make_handler(self.store, PASSWORD, "http://127.0.0.1:1")
        h.log_message = lambda *a: None
        ui = serve(h)
        try:
            _, _, resp = Browser(ui.server_port).login()
            self.assertIn("Secure", resp.getheader("Set-Cookie"))
        finally:
            stop(ui)

    def test_forged_cookie_rejected(self):
        import base64
        import hashlib
        import hmac
        import json
        payload = base64.urlsafe_b64encode(json.dumps({"exp": 9999999999, "csrf": "x"}).encode()).decode()
        for mac in ("deadbeef", hmac.new(b"wrong-key", payload.encode(), hashlib.sha256).hexdigest(), ""):
            self.b.cookie = f"hgw_session={payload}.{mac}"
            self.assertEqual(self.b.get("/")[0], 303, mac)
        # The real signing key does work, so the test above isn't passing by accident.
        good = hmac.new(self.store.secret("session-signing-key"), payload.encode(), hashlib.sha256).hexdigest()
        self.b.cookie = f"hgw_session={payload}.{good}"
        self.assertEqual(self.b.get("/")[0], 200)

    def test_no_password_means_no_login(self):
        h = admin.make_handler(self.store, "", "http://127.0.0.1:1")
        h.log_message = lambda *a: None
        ui = serve(h)
        try:
            br = Browser(ui.server_port)
            self.assertEqual(br.login("")[0], 503)
            self.assertEqual(br.cookie, "")
        finally:
            stop(ui)

    # --- CSRF and headers

    def test_csrf_required(self):
        self.b.login()
        status, _, _ = self.b.post("/keys", {"name": "x", "bank": "hermes-x", "csrf": "wrong"})
        self.assertEqual(status, 403)
        status, _, _ = self.b.post("/keys", {"name": "x", "bank": "hermes-x"}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual([k.name for k in self.store.keys()], ["alice"])

    def test_security_headers(self):
        self.b.login()
        _, _, resp = self.b.get("/")
        self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    # --- keys

    def test_create_shows_key_once_and_it_works(self):
        self.b.login()
        status, page, _ = self.b.post("/keys", {"name": "alice-laptop", "bank": "hermes-alice"})
        self.assertEqual(status, 200)
        key = re.search(r'<pre class="secret">([^<]+)</pre>', page).group(1)
        self.assertEqual(self.proxy_status(key), 200)
        self.assertEqual(self.proxy_status(key, "hermes-bob"), 403)
        _, listing, _ = self.b.get("/")
        self.assertNotIn(key, listing)
        self.assertIn("alice-laptop", listing)

    def test_bad_input_is_refused(self):
        self.b.login()
        for name, bank in [("", "hermes-a"), ("<script>", "hermes-a"), ("ok", "x/../y"), ("ok", "")]:
            self.b.post("/keys", {"name": name, "bank": bank})
        self.b.post("/keys", {"name": "alice", "bank": "hermes-a"})  # name taken
        self.assertEqual([k.name for k in self.store.keys()], ["alice"])

    def test_html_is_escaped(self):
        self.store.create("x\" onmouseover=\"1", "hermes-a")  # can't come from the form, but be safe
        self.b.login()
        _, page, _ = self.b.get("/")
        self.assertNotIn('onmouseover="1', page)

    def test_rotate_disable_delete_take_effect(self):
        self.b.login()
        alice = self.store.keys()[0]
        self.assertEqual(self.proxy_status("seed-alice-key-123456"), 200)
        _, page, _ = self.b.post(f"/keys/{alice.id}/rotate", {})
        new = re.search(r'<pre class="secret">([^<]+)</pre>', page).group(1)
        self.assertIn("Secret", page)  # warns: this key came from an agent's Secret
        self.assertEqual(self.proxy_status("seed-alice-key-123456"), 401)
        self.assertEqual(self.proxy_status(new), 200)
        self.b.post(f"/keys/{alice.id}/disable", {})
        self.assertEqual(self.proxy_status(new), 401)
        self.b.post(f"/keys/{alice.id}/enable", {})
        self.assertEqual(self.proxy_status(new), 200)
        self.b.post(f"/keys/{alice.id}/delete", {})
        self.assertEqual(self.proxy_status(new), 401)
        self.assertEqual(self.store.keys(), [])

    def test_edit_moves_key_to_another_bank(self):
        self.b.login()
        alice = self.store.keys()[0]
        self.b.post(f"/keys/{alice.id}", {"name": "alice", "bank": "hermes-shared"})
        self.assertEqual(self.proxy_status("seed-alice-key-123456", "hermes-shared"), 200)
        self.assertEqual(self.proxy_status("seed-alice-key-123456", "hermes-alice"), 403)

    def test_bank_suggestions_come_from_hindsight(self):
        self.b.login()
        _, page, _ = self.b.get("/")
        self.assertIn('<option value="hermes-bob">', page)

    def test_activity_log(self):
        self.b.login("nope")
        self.b.login()
        self.b.post("/keys", {"name": "laptop", "bank": "hermes-alice"})
        _, page, _ = self.b.get("/")
        for text in ("login failed", "logged in", "created", "laptop → hermes-alice", "imported"):
            self.assertIn(text, page)


if __name__ == "__main__":
    unittest.main()
