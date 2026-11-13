"""Run the gateway: the agent-facing proxy and the admin UI, on separate ports.

Configuration (environment):

  HGW_UPSTREAM            Hindsight's API, e.g. http://hindsight:8888 (required)
  HGW_LISTEN              proxy address, default 0.0.0.0:8889
  HGW_ADMIN_LISTEN        admin UI address, default 0.0.0.0:8890 ("" to turn the UI off)
  HGW_ADMIN_PASSWORD      admin password; the UI refuses logins without one
  HGW_DB                  SQLite file, default /data/gateway.db
  HGW_COOKIE_SECURE       "false" to allow the admin session over plain HTTP (local testing)
  HGW_MAX_BODY            largest request body in bytes, default 64 MiB
  HGW_UPSTREAM_TIMEOUT    seconds, default 600
  HGW_SEED_<n>_NAME / HGW_SEED_<n>_BANK / HGW_SEED_<n>_KEY
                          keys to import on start (see store.Store.seed)
"""

import os
import re
import signal
import sys
import threading
from http.server import ThreadingHTTPServer

from . import __version__, admin, proxy
from .store import Store


def addr(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host or "0.0.0.0", int(port)


def seeds_from_env(env) -> list[tuple[str, str, str]]:
    out = []
    for k in sorted(env):
        m = re.fullmatch(r"HGW_SEED_(\d+)_NAME", k)
        if m:
            n = m.group(1)
            out.append((env[k], env.get(f"HGW_SEED_{n}_BANK", ""), env.get(f"HGW_SEED_{n}_KEY", "")))
    return out


def serve(server):
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


def main() -> int:
    env = os.environ
    upstream = env.get("HGW_UPSTREAM", "")
    if not upstream:
        print("hindsight-gateway: HGW_UPSTREAM is required (e.g. http://hindsight:8888)", file=sys.stderr)
        return 2
    store = Store(env.get("HGW_DB", "/data/gateway.db"))
    for note in store.seed(seeds_from_env(env)):
        print(f"hindsight-gateway: {note}", flush=True)

    max_body = int(env.get("HGW_MAX_BODY", str(64 * 1024 * 1024)))
    timeout = float(env.get("HGW_UPSTREAM_TIMEOUT", "600"))
    p = ThreadingHTTPServer(addr(env.get("HGW_LISTEN", "0.0.0.0:8889")),
                            proxy.make_handler(store, upstream, max_body, timeout))
    p.daemon_threads = True
    serve(p)
    keys = store.keys()
    print(f"hindsight-gateway {__version__}: {len(keys)} key(s), proxy :{p.server_port} -> {upstream}", flush=True)

    admin_listen = env.get("HGW_ADMIN_LISTEN", "0.0.0.0:8890")
    if admin_listen:
        password = env.get("HGW_ADMIN_PASSWORD", "")
        a = ThreadingHTTPServer(addr(admin_listen), admin.make_handler(
            store, password, upstream, secure_cookies=env.get("HGW_COOKIE_SECURE", "true").lower() != "false"))
        a.daemon_threads = True
        serve(a)
        print(f"hindsight-gateway: admin UI :{a.server_port}" + ("" if password else " (no HGW_ADMIN_PASSWORD: logins refused)"),
              flush=True)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())  # Kubernetes stops pods with SIGTERM
    try:
        while not stop.wait(30):
            store.flush_usage()
    except KeyboardInterrupt:
        pass
    store.flush_usage()  # keep "last used" across restarts
    return 0


if __name__ == "__main__":
    sys.exit(main())
