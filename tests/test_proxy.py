"""The proxy must let each key use its own bank and nothing else.

A fake Hindsight records every request that reaches it, so the tests also check that
refused requests never get through, not just that the gateway answered 403.
"""

import json
import os
import socket
import tempfile
import unittest

from helpers import FakeHindsight, raw, request, serve, stop
from hindsight_gateway import proxy
from hindsight_gateway.store import Store

KEY_A, KEY_B = "key-alice-0123456789", "key-bob-9876543210"
KEY_LAPTOP = "key-alice-laptop-5555555555"
A, B = "/v1/default/banks/hermes-alice", "/v1/default/banks/hermes-bob"


class ProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(os.path.join(cls.tmp.name, "gw.db"))
        cls.store.seed([("alice", "hermes-alice", KEY_A), ("bob", "hermes-bob", KEY_B),
                        ("alice-laptop", "hermes-alice", KEY_LAPTOP)])
        cls.upstream = serve(FakeHindsight)
        handler = proxy.make_handler(cls.store, f"http://127.0.0.1:{cls.upstream.server_port}")
        cls.denials = []
        handler.log_message = lambda self, fmt, *a: cls.denials.append(fmt % a) if fmt.startswith("denied") else None
        cls.gw = serve(handler)
        cls.port = cls.gw.server_port

    @classmethod
    def tearDownClass(cls):
        stop(cls.gw)
        stop(cls.upstream)
        cls.store._db.close()
        cls.tmp.cleanup()

    def setUp(self):
        FakeHindsight.seen.clear()

    def call(self, method, path, key=KEY_A, body=None):
        h = {"Authorization": f"Bearer {key}"} if key else {}
        if body is not None:
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        return request(self.port, method, path, h, body)[0]

    def assertForwarded(self, method, path, key=KEY_A, body=None):
        self.assertEqual(self.call(method, path, key, body), 200, f"{method} {path}")
        self.assertEqual(FakeHindsight.seen[-1]["path"], path)

    def assertRefused(self, method, path, key=KEY_A, want=403, body=None):
        before = len(FakeHindsight.seen)
        self.assertEqual(self.call(method, path, key, body), want, f"{method} {path}")
        self.assertEqual(FakeHindsight.seen[before:], [], f"{method} {path} reached Hindsight")

    # --- what a key may do

    def test_own_bank_works(self):
        self.assertForwarded("PUT", A, body={})
        self.assertForwarded("PATCH", A, body={})
        self.assertForwarded("POST", A + "/memories", body={"items": []})
        self.assertForwarded("POST", A + "/memories/recall", body={"query": "q"})
        self.assertForwarded("POST", A + "/reflect", body={"query": "q"})
        self.assertForwarded("GET", A + "/operations/abc")
        self.assertForwarded("GET", A + "/stats")
        self.assertForwarded("GET", A + "/config")
        self.assertForwarded("POST", A + "/import", body={})
        self.assertForwarded("GET", A + "/documents/doc%20one")
        self.assertForwarded("GET", A + "/memories/list?limit=5&q=x")

    def test_each_key_maps_to_its_own_bank(self):
        self.assertForwarded("GET", B + "/stats", key=KEY_B)
        self.assertRefused("GET", A + "/stats", key=KEY_B)

    def test_two_keys_one_bank(self):
        self.assertForwarded("POST", A + "/memories/recall", key=KEY_LAPTOP, body={"query": "q"})
        self.assertRefused("GET", B + "/stats", key=KEY_LAPTOP)
        self.assertForwarded("GET", A + "/stats", key=KEY_A)

    def test_harmless_globals(self):
        self.assertForwarded("GET", "/version")
        self.assertForwarded("GET", "/v1/version")
        self.assertForwarded("GET", "/v1/bank-template-schema")
        self.assertEqual(self.call("GET", "/health", key=None), 200)  # probes, no key

    def test_authorization_is_not_forwarded(self):
        self.call("GET", A + "/stats")
        self.assertIsNone(FakeHindsight.seen[-1]["auth"])

    def test_body_and_query_pass_through(self):
        self.call("POST", A + "/memories/recall?budget=low", body={"query": "hello"})
        last = FakeHindsight.seen[-1]
        self.assertEqual(last["path"], A + "/memories/recall?budget=low")
        self.assertEqual(json.loads(last["body"]), {"query": "hello"})

    def test_chunked_request_body(self):
        body = b'{"query": "chunked"}'
        status = raw(self.port, f"POST {A}/memories/recall HTTP/1.1\r\nHost: h\r\n"
                                f"Authorization: Bearer {KEY_A}\r\nTransfer-Encoding: chunked\r\n"
                                f"Connection: close\r\n\r\n{len(body):x}\r\n{body.decode()}\r\n0\r\n\r\n")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(FakeHindsight.seen[-1]["body"]), {"query": "chunked"})

    # --- other banks, however it's spelled

    def test_other_bank_refused(self):
        for m, sub in [("GET", "/stats"), ("POST", "/memories/recall"), ("POST", "/memories"),
                       ("DELETE", ""), ("PUT", "")]:
            self.assertRefused(m, B + sub, body={} if m in ("POST", "PUT") else None)

    def test_path_tricks_refused(self):
        for path in [
            A + "/%2e%2e/hermes-bob/stats", A + "/%2E%2E/hermes-bob/stats",
            "/v1/default/banks/hermes-alice%2F..%2Fhermes-bob/stats",
            "/v1/default/banks/hermes-alice%2fx/stats", "/v1/default/banks//hermes-bob/stats",
            "/v1/default/banks/HERMES-BOB/stats", "/v1/default/banks/hermes-alice%20/stats",
            "/v1/other/banks/hermes-bob/stats",
            "/v1/default/banks/hermes-alice/memories/%2e%2e/%2e%2e/hermes-bob/stats",
            A + "/./stats", A + "//stats", A + "/",
        ]:
            self.assertRefused("GET", path)
        for line in [f"GET {A}/../hermes-bob/stats", f"GET http://127.0.0.1{B}/stats",
                     f"GET {A}\\..\\hermes-bob/stats", "GET /v1/default/banks/hermes-bob\t/stats",
                     f"GET {A}/stats/../../hermes-bob/stats"]:
            status = raw(self.port, f"{line} HTTP/1.1\r\nHost: h\r\nAuthorization: Bearer {KEY_A}\r\n"
                                    "Connection: close\r\n\r\n")
            self.assertIn(status, (400, 403), line)
        self.assertEqual(FakeHindsight.seen, [])

    # --- things that cross banks or leave the bank

    def test_cross_bank_and_global_endpoints_refused(self):
        for m, path in [
            ("DELETE", A),
            ("POST", A + "/aliases"), ("GET", A + "/aliases"), ("PATCH", A + "/aliases/x"),
            ("POST", A + "/clone?target_bank_id=hermes-bob"),
            ("POST", A + "/transfer/export"), ("POST", A + "/transfer/import?target_bank_id=hermes-bob"),
            ("POST", A + "/document-transfer"), ("POST", A + "/document-transfer/export"),
            ("POST", A + "/webhooks"), ("GET", A + "/webhooks"),
            ("PATCH", A + "/config"), ("DELETE", A + "/config"),
            ("GET", A + "/something-new-in-a-future-hindsight"),
            ("GET", "/v1/default/files/download/abc"),
            ("GET", "/v1/default/files/download/banks/hermes-bob/exports/u/transfer.zip"),
            ("GET", "/v1/default/files/download/tenants/public/banks/hermes-bob/exports/u/t.zip"),
            ("GET", "/v1/default/files/download/banks/hermes-alice%2F..%2Fhermes-bob/x/t.zip"),
            ("GET", "/v1/default/files/download/banks/HERMES-ALICE/x/t.zip"),
            ("GET", "/v1/default/chunks/"), ("POST", "/v1/default/banks"),
            ("GET", "/metrics"), ("GET", "/openapi.json"), ("GET", "/docs"),
            ("POST", "/mcp"), ("GET", "/mcp/"),
        ]:
            self.assertRefused(m, path, body={} if m in ("POST", "PATCH") else None)

    # --- global reads the gateway scopes to the key's bank

    def test_bank_list_shows_only_own_bank(self):
        status, data, _ = request(self.port, "GET", "/v1/default/banks?limit=50",
                                  {"Authorization": f"Bearer {KEY_A}"})
        self.assertEqual(status, 200)
        doc = json.loads(data)
        self.assertEqual([b["bank_id"] for b in doc["banks"]], ["hermes-alice"])
        self.assertEqual((doc["total"], doc["limit"], doc["offset"]), (1, 50, 0))
        # Hindsight was asked for the key's bank, not for whatever the client searched.
        self.assertIn("q=hermes-alice", FakeHindsight.seen[-1]["path"])
        _, data, _ = request(self.port, "GET", "/v1/default/banks?q=bob", {"Authorization": f"Bearer {KEY_A}"})
        self.assertEqual(json.loads(data)["banks"], [])
        _, data, _ = request(self.port, "GET", "/v1/default/banks?offset=1", {"Authorization": f"Bearer {KEY_A}"})
        self.assertEqual(json.loads(data)["banks"], [])

    def test_chunk_only_from_own_bank(self):
        status, data, _ = request(self.port, "GET", "/v1/default/chunks/hermes-alice__0",
                                  {"Authorization": f"Bearer {KEY_A}"})
        self.assertEqual((status, json.loads(data)["bank_id"]), (200, "hermes-alice"))
        status, data, _ = request(self.port, "GET", "/v1/default/chunks/hermes-bob__0",
                                  {"Authorization": f"Bearer {KEY_A}"})
        self.assertEqual(status, 404)
        self.assertNotIn(b"hermes-bob", data)
        self.assertEqual(self.call("GET", "/v1/default/chunks/missing"), 404)

    def test_own_export_download(self):
        self.assertForwarded("GET", "/v1/default/files/download/banks/hermes-alice/exports/u/transfer.zip")
        self.assertForwarded("GET", "/v1/default/files/download/tenants/public/banks/hermes-alice/exports/u/t.zip")

    # --- keys

    def test_bad_keys(self):
        self.assertRefused("GET", A + "/stats", key=None, want=401)
        self.assertRefused("GET", A + "/stats", key="wrong", want=401)
        self.assertRefused("GET", A + "/stats", key="", want=401)
        self.assertRefused("GET", "/version", key=None, want=401)
        status = raw(self.port, f"GET {A}/stats HTTP/1.1\r\nHost: h\r\nAuthorization: Basic {KEY_A}\r\n"
                                "Connection: close\r\n\r\n")
        self.assertEqual(status, 401)
        self.assertEqual(FakeHindsight.seen, [])

    def test_disabled_key_stops_at_once(self):
        k = next(k for k in self.store.keys() if k.name == "alice-laptop")
        self.store.set_enabled(k.id, False)
        try:
            self.assertRefused("GET", A + "/stats", key=KEY_LAPTOP, want=401)
        finally:
            self.store.set_enabled(k.id, True)
        self.assertForwarded("GET", A + "/stats", key=KEY_LAPTOP)

    def test_denials_name_the_key(self):
        self.denials.clear()
        self.call("GET", B + "/stats", key=KEY_LAPTOP)
        self.call("GET", B + "/stats", key=KEY_A)
        self.assertEqual(len(self.denials), 2)
        self.assertIn("key alice-laptop, bank hermes-alice", self.denials[0])
        self.assertIn("key alice, bank hermes-alice", self.denials[1])
        self.assertNotIn(KEY_LAPTOP, " ".join(self.denials))

    def test_usage_is_recorded(self):
        self.call("GET", A + "/stats", key=KEY_B)  # refused, but a valid key: counts as use
        self.call("GET", B + "/stats", key=KEY_B)
        bob = next(k for k in self.store.keys() if k.name == "bob")
        self.assertIsNotNone(bob.last_used_at)

    # --- responses

    def test_streamed_response_passes_through(self):
        status, data, resp = request(self.port, "POST", A + "/reflect?stream=1",
                                     {"Authorization": f"Bearer {KEY_A}"}, b"{}")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"data: one\n\ndata: two\n\n")
        self.assertEqual(resp.getheader("Transfer-Encoding"), "chunked")

    def test_keep_alive_serves_several_requests(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        for _ in range(3):
            c.request("GET", A + "/stats", headers={"Authorization": f"Bearer {KEY_A}"})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 200)
        c.close()

    def test_upstream_down_is_a_502(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()
        gw = serve(proxy.make_handler(self.store, f"http://127.0.0.1:{dead}", timeout=3))
        try:
            status, _, _ = request(gw.server_port, "GET", A + "/stats", {"Authorization": f"Bearer {KEY_A}"})
            self.assertEqual(status, 502)
        finally:
            stop(gw)


if __name__ == "__main__":
    unittest.main()
