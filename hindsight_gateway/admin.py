"""The admin UI: list, create, edit, rotate, disable and delete keys.

Server-rendered HTML, no JavaScript. One admin password (HGW_ADMIN_PASSWORD). Sessions are
signed cookies (HttpOnly, SameSite=Strict); every form carries a CSRF token. Runs on its own
port, so agents, which only reach the proxy port, never see it.
"""

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from importlib import resources

from . import __version__
from .policy import BANK_ID

SESSION_COOKIE = "hgw_session"
SESSION_TTL = 12 * 3600
KEY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
CSP = ("default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")


def esc(v) -> str:
    return html.escape(str(v), quote=True)


def ago(ts: int | None) -> str:
    if not ts:
        return "never"
    s = max(0, int(time.time()) - ts)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if s >= size:
            return f"{s // size} {unit} ago"
    return "just now"


def iso(ts: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts)) if ts else ""


class Throttle:
    """Failed logins: 5 per client per 10 min, and 30 overall, then wait."""

    def __init__(self, per_client=5, overall=30, window=600):
        self.per_client, self.overall, self.window = per_client, overall, window
        self.fails: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _recent(self, client: str, now: float) -> tuple[int, int]:
        for c in list(self.fails):
            self.fails[c] = [t for t in self.fails[c] if now - t < self.window]
            if not self.fails[c]:
                del self.fails[c]
        return len(self.fails.get(client, [])), sum(len(v) for v in self.fails.values())

    def blocked(self, client: str) -> bool:
        with self.lock:
            mine, total = self._recent(client, time.time())
            return mine >= self.per_client or total >= self.overall

    def fail(self, client: str):
        with self.lock:
            self.fails.setdefault(client, []).append(time.time())

    def reset(self, client: str):
        with self.lock:
            self.fails.pop(client, None)


def list_upstream_banks(upstream: str) -> list[str]:
    """Bank ids Hindsight knows about, for the bank field's suggestions. Best effort."""
    try:
        with urllib.request.urlopen(upstream.rstrip("/") + "/v1/default/banks", timeout=3) as r:
            data = json.load(r)
        banks = data.get("banks", data) if isinstance(data, dict) else data
        return sorted({b.get("bank_id") or b.get("id") for b in banks if isinstance(b, dict)} - {None})
    except Exception:
        return []


def make_handler(store, password: str, upstream: str, secure_cookies: bool = True):
    pw_digest = hashlib.sha256(password.encode()).digest() if password else None
    signing_key = store.secret("session-signing-key")
    throttle = Throttle()
    css = resources.files(__package__).joinpath("static/style.css").read_bytes()

    def sign(payload: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        mac = hmac.new(signing_key, raw.encode(), hashlib.sha256).hexdigest()
        return f"{raw}.{mac}"

    def unsign(value: str) -> dict | None:
        raw, _, mac = value.partition(".")
        good = hmac.new(signing_key, raw.encode(), hashlib.sha256).hexdigest()
        if not raw or not hmac.compare_digest(mac, good):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(raw.encode()))
        except ValueError:
            return None
        return payload if payload.get("exp", 0) > time.time() else None

    class AdminHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 120
        server_version = "hindsight-gateway-admin"
        sys_version = ""

        # -- plumbing ---------------------------------------------------------------

        def client(self) -> str:
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            return fwd or self.client_address[0]

        def session(self) -> dict | None:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == SESSION_COOKIE:
                    return unsign(v)
            return None

        def send(self, code: int, body: bytes, ctype="text/html; charset=utf-8", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def redirect(self, to: str, cookie: str | None = None):
            headers = {"Location": to}
            if cookie is not None:
                headers["Set-Cookie"] = cookie
            self.send(303, b"", headers=headers)

        def cookie(self, value: str, max_age: int) -> str:
            flags = f"Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
            return f"{SESSION_COOKIE}={value}; {flags}" + ("; Secure" if secure_cookies else "")

        def form(self) -> dict[str, str]:
            length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

        def page(self, title: str, body: str, sess: dict | None = None, code=200):
            nav = ""
            if sess:
                nav = (f'<form method="post" action="/logout" class="logout">'
                       f'<input type="hidden" name="csrf" value="{esc(sess["csrf"])}">'
                       f'<button class="link">Log out</button></form>')
            doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Hindsight gateway</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body><header><a href="/" class="brand">Hindsight gateway</a>{nav}</header>
<main>{body}</main>
<footer>hindsight-gateway {esc(__version__)}</footer></body></html>"""
            self.send(code, doc.encode())

        def log_message(self, fmt, *args):
            sys.stderr.write(f"admin {self.client()} {fmt % args}\n")

        # -- routing ------------------------------------------------------------------

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz":
                return self.send(200, b"ok", "text/plain")
            if path == "/static/style.css":
                return self.send(200, css, "text/css; charset=utf-8", {"Cache-Control": "max-age=3600"})
            if path == "/login":
                return self.login_page()
            sess = self.session()
            if not sess:
                return self.redirect("/login")
            if path == "/":
                return self.index(sess)
            m = re.fullmatch(r"/keys/(\d+)", path)
            if m:
                return self.edit_page(sess, int(m.group(1)))
            m = re.fullmatch(r"/keys/(\d+)/(rotate|delete)", path)
            if m:
                return self.confirm_page(sess, int(m.group(1)), m.group(2))
            self.page("Not found", "<h1>Not found</h1>", sess, 404)

        do_HEAD = do_GET

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            data = self.form()
            if path == "/login":
                return self.login(data)
            sess = self.session()
            if not sess:
                return self.redirect("/login")
            if not hmac.compare_digest(data.get("csrf", ""), sess["csrf"]):
                return self.page("Expired", "<h1>That form expired</h1><p><a href='/'>Start over</a></p>", sess, 403)
            if path == "/logout":
                return self.redirect("/login", self.cookie("", 0))
            if path == "/keys":
                return self.create(sess, data)
            m = re.fullmatch(r"/keys/(\d+)(?:/(rotate|delete|enable|disable))?", path)
            if m:
                return self.change(sess, int(m.group(1)), m.group(2) or "update", data)
            self.page("Not found", "<h1>Not found</h1>", sess, 404)

        # -- login --------------------------------------------------------------------

        def login_page(self, error: str = "", code=200):
            if not pw_digest:
                return self.page("Disabled", "<h1>Admin UI disabled</h1>"
                                 "<p>Set <code>HGW_ADMIN_PASSWORD</code> to turn it on.</p>", code=503)
            err = f'<p class="error">{esc(error)}</p>' if error else ""
            self.page("Log in", f"""<section class="narrow"><h1>Log in</h1>{err}
<form method="post" action="/login"><label>Admin password
<input type="password" name="password" autocomplete="current-password" required autofocus></label>
<button>Log in</button></form></section>""", code=code)

        def login(self, data):
            if not pw_digest:
                return self.login_page()
            who = self.client()
            if throttle.blocked(who):
                return self.login_page("Too many tries. Wait a few minutes.", 429)
            given = hashlib.sha256(data.get("password", "").encode()).digest()
            if not hmac.compare_digest(given, pw_digest):
                throttle.fail(who)
                store.log(who, "login failed", "wrong password")
                time.sleep(0.5)
                return self.login_page("Wrong password.", 401)
            throttle.reset(who)
            store.log(who, "logged in", "admin")
            value = sign({"exp": int(time.time()) + SESSION_TTL, "csrf": secrets.token_urlsafe(24)})
            self.redirect("/", self.cookie(value, SESSION_TTL))

        # -- pages --------------------------------------------------------------------

        def csrf(self, sess) -> str:
            return f'<input type="hidden" name="csrf" value="{esc(sess["csrf"])}">'

        def bank_options(self) -> str:
            banks = sorted(set(store.banks_in_use()) | set(list_upstream_banks(upstream)))
            return "".join(f'<option value="{esc(b)}">' for b in banks)

        def index(self, sess, notice: str = ""):
            rows = []
            for k in store.keys():
                status = "on" if k.enabled else "off"
                source = ("from its Secret" if k.source == "seed" else "made here")
                rows.append(f"""<tr class="{'' if k.enabled else 'disabled'}">
<td><a href="/keys/{k.id}">{esc(k.name)}</a></td><td><code>{esc(k.bank)}</code></td>
<td><code>{esc(k.key_hint)}</code></td><td>{source}</td>
<td title="{esc(iso(k.last_used_at))}">{ago(k.last_used_at)}</td>
<td><span class="status {status}">{'active' if k.enabled else 'disabled'}</span></td></tr>""")
            table = ("<table><thead><tr><th>Name</th><th>Bank</th><th>Key</th><th>Source</th>"
                     "<th>Last used</th><th>Status</th></tr></thead><tbody>" + "".join(rows) +
                     "</tbody></table>") if rows else "<p class='muted'>No keys yet.</p>"
            events = "".join(
                f"<li><span title='{esc(iso(at))}'>{ago(at)}</span> · {esc(action)} · {esc(detail)}"
                f" <span class='muted'>({esc(actor)})</span></li>" for at, actor, action, detail in store.events(15))
            note = f'<p class="notice">{esc(notice)}</p>' if notice else ""
            self.page("Keys", f"""{note}<h1>Keys</h1>
<p class="muted">Each key opens one memory bank. Agents send it as <code>Authorization: Bearer &lt;key&gt;</code>.</p>
{table}
<h2>New key</h2>
<form method="post" action="/keys" class="inline">{self.csrf(sess)}
<label>Name <input name="name" required maxlength="64" placeholder="alice-laptop"></label>
<label>Bank <input name="bank" required maxlength="64" list="banks" placeholder="hermes-alice"></label>
<datalist id="banks">{self.bank_options()}</datalist>
<button>Create key</button></form>
<h2>Recent activity</h2><ul class="events">{events or "<li class='muted'>Nothing yet.</li>"}</ul>""", sess)

        def show_key(self, sess, title: str, k, key: str, extra: str = ""):
            self.page(title, f"""<h1>{esc(title)}</h1>
<p>This is the only time the full key is shown. Copy it now.</p>
<pre class="secret">{esc(key)}</pre>
<dl><dt>Name</dt><dd>{esc(k.name)}</dd><dt>Bank</dt><dd><code>{esc(k.bank)}</code></dd></dl>
{extra}<p><a href="/">Back to keys</a></p>""", sess)

        def edit_page(self, sess, key_id: int, error: str = ""):
            k = store.get(key_id)
            if not k:
                return self.page("Not found", "<h1>Key not found</h1>", sess, 404)
            err = f'<p class="error">{esc(error)}</p>' if error else ""
            seed_note = ("<p class='muted'>This key came from an agent's Secret. Rotating or deleting it here "
                         "takes it over: the Secret's value won't be imported again.</p>"
                         if k.source == "seed" else "")
            toggle = "disable" if k.enabled else "enable"
            self.page(k.name, f"""<p><a href="/">← Keys</a></p><h1>{esc(k.name)}</h1>{err}
<dl><dt>Key</dt><dd><code>{esc(k.key_hint)}</code></dd>
<dt>Created</dt><dd>{esc(iso(k.created_at))}</dd><dt>Last used</dt><dd>{ago(k.last_used_at)}</dd>
<dt>Status</dt><dd>{'active' if k.enabled else 'disabled'}</dd></dl>{seed_note}
<h2>Edit</h2><form method="post" action="/keys/{k.id}" class="inline">{self.csrf(sess)}
<label>Name <input name="name" value="{esc(k.name)}" required maxlength="64"></label>
<label>Bank <input name="bank" value="{esc(k.bank)}" required maxlength="64" list="banks"></label>
<datalist id="banks">{self.bank_options()}</datalist><button>Save</button></form>
<h2>Actions</h2><div class="actions">
<form method="post" action="/keys/{k.id}/{toggle}">{self.csrf(sess)}<button class="secondary">{toggle.title()}</button></form>
<a class="button secondary" href="/keys/{k.id}/rotate">Rotate…</a>
<a class="button danger" href="/keys/{k.id}/delete">Delete…</a></div>""", sess)

        def confirm_page(self, sess, key_id: int, what: str):
            k = store.get(key_id)
            if not k:
                return self.page("Not found", "<h1>Key not found</h1>", sess, 404)
            text = {"rotate": ("Rotate", f"The current key for <b>{esc(k.name)}</b> stops working right away, "
                               "and you get a new one. Anything using it needs the new value."),
                    "delete": ("Delete", f"<b>{esc(k.name)}</b> stops working right away and is removed. "
                               "This can't be undone.")}[what]
            self.page(f"{text[0]} {k.name}", f"""<h1>{text[0]} {esc(k.name)}?</h1><p>{text[1]}</p>
<div class="actions"><form method="post" action="/keys/{k.id}/{what}">{self.csrf(sess)}
<button class="{'danger' if what == 'delete' else ''}">{text[0]}</button></form>
<a class="button secondary" href="/keys/{k.id}">Cancel</a></div>""", sess)

        # -- changes ------------------------------------------------------------------

        def check(self, name: str, bank: str) -> str:
            if not KEY_NAME.fullmatch(name):
                return "Name: letters, digits, spaces, '.', '_' or '-', up to 64."
            if not BANK_ID.fullmatch(bank):
                return "Bank: letters, digits, '.', '_' or '-', up to 64."
            return ""

        def create(self, sess, data):
            name, bank = data.get("name", "").strip(), data.get("bank", "").strip()
            err = self.check(name, bank)
            if not err and any(k.name == name for k in store.keys()):
                err = f"There's already a key called {name}."
            if err:
                return self.index(sess, err)
            key = store.create(name, bank, actor=self.client())
            k = next(k for k in store.keys() if k.name == name)
            self.show_key(sess, "Key created", k, key)

        def change(self, sess, key_id: int, action: str, data):
            k = store.get(key_id)
            if not k:
                return self.page("Not found", "<h1>Key not found</h1>", sess, 404)
            who = self.client()
            if action == "update":
                name, bank = data.get("name", "").strip(), data.get("bank", "").strip()
                err = self.check(name, bank)
                if not err and name != k.name and any(x.name == name for x in store.keys()):
                    err = f"There's already a key called {name}."
                if err:
                    return self.edit_page(sess, key_id, err)
                store.update(key_id, name, bank, actor=who)
                return self.redirect(f"/keys/{key_id}")
            if action in ("enable", "disable"):
                store.set_enabled(key_id, action == "enable", actor=who)
                return self.redirect(f"/keys/{key_id}")
            if action == "rotate":
                key = store.rotate(key_id, actor=who)
                extra = ("<p class='notice'>This key came from an agent's Secret: put the new value in that "
                         "Secret (<code>HINDSIGHT_API_KEY</code>) and restart the agent, or it loses its "
                         "memory.</p>" if k.source == "seed" else "")
                return self.show_key(sess, "Key rotated", store.get(key_id), key, extra)
            if action == "delete":
                store.delete(key_id, actor=who)
                return self.index(sess, f"Deleted {k.name}.")

    return AdminHandler
