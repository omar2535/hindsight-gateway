"""Smoke test for a patched control-plane image: each person sees only their own bank.

Runs a fake Hindsight and the gateway in this process, the image in Docker (host
network), then signs in as alice and checks what the UI's own /api routes return.

    PYTHONPATH=.:tests python3 control-plane/smoke.py <image>
"""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import time

from helpers import FakeHindsight, serve
from hindsight_gateway import proxy
from hindsight_gateway.store import Store

KEY_A, KEY_B = "hgw_smoke-alice-0123456789abcdef", "hgw_smoke-bob-0123456789abcdef"
UI_PORT = 19999


def ui(method, path, cookie=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", UI_PORT, timeout=30)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if cookie:
        headers["Cookie"] = cookie
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data, r


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        raise SystemExit(1)


def main(image):
    tmp = tempfile.TemporaryDirectory()
    store = Store(os.path.join(tmp.name, "gw.db"))
    store.seed([("alice", "hermes-alice", KEY_A), ("bob", "hermes-bob", KEY_B)])
    upstream = serve(FakeHindsight)
    gw = serve(proxy.make_handler(store, f"http://127.0.0.1:{upstream.server_port}"))
    name = f"cp-smoke-{os.getpid()}"
    subprocess.run([
        "docker", "run", "-d", "--rm", "--name", name, "--network", "host",
        # As Hermify runs it: non-root, read-only root filesystem. start-all.sh checks that
        # $HOME/.pg0 is writable even with the API off.
        "--user", "1000:1000", "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/app/control-plane/.next/cache",
        "-e", "HOME=/tmp",
        "-e", f"HINDSIGHT_CP_PORT={UI_PORT}",
        "-e", "HINDSIGHT_CP_AUTH_MODE=api-key",
        "-e", f"HINDSIGHT_CP_DATAPLANE_API_URL=http://127.0.0.1:{gw.server_port}",
        "-e", "HINDSIGHT_CP_ACCESS_KEY=smoke-session-secret",
        # Must never be sent in API-key mode, even though it's set.
        "-e", f"HINDSIGHT_CP_DATAPLANE_API_KEY={KEY_B}",
        image,
    ], check=True, stdout=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                if ui("GET", "/login")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise SystemExit("the UI never came up")

        check(ui("GET", "/api/banks")[0] == 401, "no session: 401")
        check(ui("POST", "/api/auth/login", body={"key": "wrong"})[0] == 401, "unknown key: 401")
        status, _, r = ui("POST", "/api/auth/login", body={"key": KEY_A})
        check(status == 200, "alice signs in")
        cookie = r.getheader("Set-Cookie").split(";")[0]
        check(KEY_A not in cookie, "the cookie doesn't hold the key in clear")

        FakeHindsight.seen.clear()
        status, data, _ = ui("GET", "/api/banks", cookie)
        banks = [b["bank_id"] for b in json.loads(data).get("banks", [])]
        check(status == 200 and banks == ["hermes-alice"], f"bank list is alice's only: {banks}")
        check(ui("GET", "/api/stats/hermes-alice", cookie)[0] == 200, "alice's stats")
        check(ui("GET", "/api/stats/hermes-bob", cookie)[0] != 200, "bob's stats refused")
        check(not any("hermes-bob" in s["path"] for s in FakeHindsight.seen if "q=" not in s["path"]),
              "nothing about bob reached Hindsight")

        tampered = cookie[:-4] + ("AAAA" if not cookie.endswith("AAAA") else "BBBB")
        check(ui("GET", "/api/banks", tampered)[0] == 401, "altered cookie: 401")
        print("smoke test passed")
    finally:
        subprocess.run(["docker", "logs", name], check=False)
        subprocess.run(["docker", "rm", "-f", name], check=False, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main(sys.argv[1])
