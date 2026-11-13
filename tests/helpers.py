"""Shared test bits: run handlers on free ports, and a fake Hindsight that records requests."""

import http.client
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve(handler) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def stop(srv):
    srv.shutdown()
    srv.server_close()


def request(port, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data, r


def raw(port, text: str) -> int:
    """Send a hand-written request (for paths http.client would refuse); return the status."""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(text.encode())
    buf = b""
    while b"\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    return int(buf.split(b"\r\n", 1)[0].split()[1])


class FakeHindsight(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list = []
    lock = threading.Lock()
    banks = ["hermes-alice", "hermes-bob"]

    def _any(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        with self.lock:
            FakeHindsight.seen.append({"method": self.command, "path": self.path,
                                       "auth": self.headers.get("Authorization"), "body": body})
        if "stream=1" in self.path:
            # No Content-Length: the gateway has to stream it through re-chunked.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"data: one\n\ndata: two\n\n")
            self.close_connection = True
            return
        if self.path == "/v1/default/banks":
            out = json.dumps({"banks": [{"bank_id": b} for b in self.banks]}).encode()
        else:
            out = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any

    def log_message(self, *a):
        pass
