"""The proxy agents talk to: checks the key, then forwards to Hindsight."""

import http.client
import json
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

from .policy import BANK_LIST, HEALTH, allowed, special

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailer", "transfer-encoding", "upgrade", "host", "authorization", "content-length",
              "expect"}


def strip_prefix(path: str, prefix: str) -> str:
    """'/api/v1/x' -> '/v1/x' for prefix '/api'. Paths without the prefix are left alone,
    so in-cluster clients can keep calling the bare paths."""
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        return path[len(prefix):] or "/"
    return path


def _int(args: dict, name: str, default: int) -> int:
    try:
        return max(0, int((args.get(name) or [default])[0]))
    except ValueError:
        return default


def make_handler(store, upstream: str, max_body: int = 64 * 1024 * 1024, timeout: float = 600, prefix: str = ""):
    up = urllib.parse.urlsplit(upstream)
    prefix = prefix.rstrip("/")

    class ProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 900  # idle keep-alive connections don't hold a thread forever
        server_version = "hindsight-gateway"
        sys_version = ""

        def _reply(self, code: int, detail: str):
            body = json.dumps({"detail": detail}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes | None:
            if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
                out = bytearray()
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        return bytes(out)
                    out += self.rfile.read(size)
                    self.rfile.readline()
                    if len(out) > max_body:
                        return None
            length = int(self.headers.get("Content-Length") or 0)
            if length > max_body:
                return None
            return self.rfile.read(length) if length else b""

        def _scoped(self, kind: str, path: str, query: str, name: str, bank: str):
            """Global reads answered for one bank: the bank list (just the key's bank) and
            chunk lookups (only chunks in the key's bank). Buffered: both are small JSON."""
            if self._read_body() is None:
                self.close_connection = True
                return self._reply(413, "Hindsight gateway: request body too large")
            args = urllib.parse.parse_qs(query)
            if kind == "banks":
                # q matches bank ids and names as substrings; the exact id is filtered below.
                target = BANK_LIST + "?" + urllib.parse.urlencode({"q": bank, "limit": 1000})
            else:
                target = path + ("?" + query if query else "")
            conn = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=timeout)
            try:
                conn.request("GET", target, headers={"Host": up.netloc, "Accept": "application/json"})
                resp = conn.getresponse()
                data = resp.read()
            except (OSError, http.client.HTTPException) as e:
                self.close_connection = True
                return self._reply(502, f"Hindsight gateway: upstream error ({type(e).__name__})")
            finally:
                conn.close()
            if resp.status != 200:
                return self._send(resp.status, resp.getheader("Content-Type") or "application/json", data)
            try:
                doc = json.loads(data)
            except ValueError:
                return self._reply(502, "Hindsight gateway: unexpected answer from Hindsight")
            if kind == "banks":
                search = (args.get("q") or [""])[0].lower()
                mine = [b for b in (doc.get("banks") or []) if isinstance(b, dict) and b.get("bank_id") == bank
                        and (search in bank.lower() or search in str(b.get("name") or "").lower())]
                limit, offset = _int(args, "limit", 100), _int(args, "offset", 0)
                doc = {"banks": mine[offset:offset + limit], "total": len(mine), "limit": limit, "offset": offset}
            elif not isinstance(doc, dict) or doc.get("bank_id") != bank:
                self.log_message("denied %s %s (key %s, bank %s)", self.command, path, name, bank)
                return self._reply(404, "Chunk not found")
            return self._send(200, "application/json", json.dumps(doc).encode())

        def _send(self, code: int, content_type: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self):
            raw_path, sep, query = self.path.partition("?")
            path = strip_prefix(raw_path, prefix)
            target = path + sep + query  # what Hindsight sees: never the prefix
            if path in HEALTH and self.command == "GET" and not self.headers.get("Authorization"):
                pass  # probes and plain health checks, no key needed
            else:
                auth = self.headers.get("Authorization") or ""
                key = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
                found = store.lookup(key)
                if not found:
                    self.close_connection = True
                    return self._reply(401, "Hindsight gateway: missing, unknown or disabled API key")
                name, bank = found
                if not allowed(self.command, path, bank):
                    self.log_message("denied %s %s (key %s, bank %s)", self.command, path, name, bank)
                    self.close_connection = True
                    return self._reply(403, "Hindsight gateway: not allowed for this key")
                kind = special(self.command, path)
                if kind:
                    return self._scoped(kind, path, query, name, bank)

            body = self._read_body()
            if body is None:
                self.close_connection = True
                return self._reply(413, "Hindsight gateway: request body too large")

            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
            headers["Host"] = up.netloc
            if body or self.command in ("POST", "PUT", "PATCH"):
                headers["Content-Length"] = str(len(body))
            conn = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=timeout)
            try:
                conn.request(self.command, target, body=body or None, headers=headers)
                resp = conn.getresponse()
                self.send_response(resp.status, resp.reason)
                length = resp.getheader("Content-Length")
                for k, v in resp.getheaders():
                    if k.lower() not in HOP_BY_HOP:
                        self.send_header(k, v)
                no_body = self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200
                if no_body:
                    self.send_header("Content-Length", length or "0")
                    self.end_headers()
                    return
                if length is not None:
                    self.send_header("Content-Length", length)
                    self.end_headers()
                    while chunk := resp.read(65536):
                        self.wfile.write(chunk)
                else:
                    # Stream through (e.g. server-sent events) re-chunked.
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    while chunk := resp.read1(65536):
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
            except (OSError, http.client.HTTPException) as e:
                self.close_connection = True
                try:
                    self._reply(502, f"Hindsight gateway: upstream error ({type(e).__name__})")
                except OSError:
                    pass
            finally:
                conn.close()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle

        def do_OPTIONS(self):
            self._reply(405, "Hindsight gateway: method not allowed")

        def log_message(self, fmt, *args):
            # Method, path (no query string: it can hold memory text) and status; never headers.
            msg = re.sub(r'"(\w+) ([^ ?"]*)\?[^ "]*', r'"\1 \2?…', fmt % args)
            sys.stderr.write(f"proxy {self.address_string()} {msg}\n")

    return ProxyHandler
