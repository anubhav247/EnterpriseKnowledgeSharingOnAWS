#!/usr/bin/env python3
"""Prove Entra -> AgentCore Gateway -> MCP runtime without any MCP client.

  login   browser sign-in (authorization code + PKCE) as a developer; caches the token in
          ~/.cache/tfc-kb/token.json (outside the repo, mode 600)
  test    positive calls through the gateway with the cached token, and
          negative checks (no token, tampered token, wrong-audience token)

    python infra/entra/gateway_test.py login
    python infra/entra/gateway_test.py test
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import time

import requests

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "infra"))
from settings import deployment, outputs  # noqa: E402

OUT = outputs()
_CFG = deployment()
ENTRA = (_CFG.get("identity") or {}).get("entra") or _CFG.get("entra") or {}
TENANT = os.environ.get("ENTRA_TENANT_ID") or ENTRA.get("tenantId")
CLIENT = os.environ.get("ENTRA_CLIENT_ID") or ENTRA.get("clientId")
SCOPE = f"api://{CLIENT}/kb.read offline_access"
GATEWAY = OUT["GatewayUrl"]
CACHE = pathlib.Path.home() / ".cache" / "tfc-kb" / "token.json"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"


def claims(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def login() -> None:
    """Authorization code + PKCE through the browser, as Kiro does.

    Device code is blocked by Entra security defaults (AADSTS530035), and it is
    not the flow an MCP client uses, so it is not the thing to prove.
    """
    import hashlib
    import http.server
    import secrets
    import urllib.parse
    import webbrowser

    port = int(os.environ.get("CALLBACK_PORT", "8765"))
    redirect = f"http://localhost:{port}/oauth/callback"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    got: dict = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab.")

        def log_message(self, *a):
            pass

    url = f"{AUTHORITY}/authorize?" + urllib.parse.urlencode({
        "client_id": CLIENT, "response_type": "code", "redirect_uri": redirect,
        "scope": SCOPE, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "prompt": "select_account"})
    print(f"Opening the browser to sign in. If it does not open, visit:\n{url}\n", flush=True)
    webbrowser.open(url)

    server = http.server.HTTPServer(("127.0.0.1", port), Callback)
    server.timeout = 300
    while not got:
        server.handle_request()
    server.server_close()
    if "code" not in got:
        raise SystemExit(f"sign-in failed: {got.get('error')}: {got.get('error_description', '')[:300]}")

    t = requests.post(f"{AUTHORITY}/token", timeout=30, data={
        "grant_type": "authorization_code", "client_id": CLIENT, "code": got["code"],
        "redirect_uri": redirect, "code_verifier": verifier, "scope": SCOPE}).json()
    if "access_token" not in t:
        raise SystemExit(f"token exchange failed: {t.get('error')}: {t.get('error_description', '')[:300]}")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(t))
    CACHE.chmod(0o600)
    c = claims(t["access_token"])
    print(f"signed in: {c.get('preferred_username') or c.get('upn')} "
          f"aud={c['aud']} azp={c.get('azp')} scp={c.get('scp')} ver={c.get('ver')}")


def login_device_code() -> None:
    r = requests.post(f"{AUTHORITY}/devicecode",
                      data={"client_id": CLIENT, "scope": SCOPE}, timeout=30).json()
    if "device_code" not in r:
        raise SystemExit(f"device code request failed: {r}")
    print(r["message"], flush=True)
    deadline = time.time() + r["expires_in"]
    while time.time() < deadline:
        time.sleep(r.get("interval", 5))
        t = requests.post(f"{AUTHORITY}/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": CLIENT, "device_code": r["device_code"]}, timeout=30).json()
        if "access_token" in t:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(t))
            CACHE.chmod(0o600)
            c = claims(t["access_token"])
            print(f"signed in: {c.get('preferred_username') or c.get('upn')} "
                  f"aud={c['aud']} azp={c.get('azp')} scp={c.get('scp')} ver={c.get('ver')}")
            return
        if t.get("error") not in ("authorization_pending", "slow_down"):
            raise SystemExit(f"sign-in failed: {t.get('error')}: {t.get('error_description', '')[:300]}")
    raise SystemExit("device code expired")


def rpc(token: str | None, method: str, params: dict | None = None, session: str | None = None):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["Mcp-Session-Id"] = session
    r = requests.post(GATEWAY, headers=headers, timeout=60, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    body = r.text
    if body.startswith("event:") or body.startswith("data:"):
        body = next(line[5:] for line in body.splitlines() if line.startswith("data:"))
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {"raw": body[:300]}
    return r.status_code, payload, r.headers.get("Mcp-Session-Id")


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    return ok


def test() -> int:
    token = json.loads(CACHE.read_text())["access_token"]
    c = claims(token)
    if c["exp"] < time.time():
        raise SystemExit("cached token expired; run login again")
    results = []

    init = {"protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "gateway-test", "version": "1"}}
    status, body, session = rpc(token, "initialize", init)
    results.append(check("initialize with Entra token", status == 200 and "result" in body,
                         f"HTTP {status} {json.dumps(body)[:160]}"))
    rpc(token, "notifications/initialized", session=session)

    status, body, _ = rpc(token, "tools/list", session=session)
    names = [t["name"] for t in body.get("result", {}).get("tools", [])]
    results.append(check("tools/list", status == 200 and len(names) >= 4, ", ".join(names)))

    search = next((n for n in names if n.endswith("search_knowledge_base")), None)
    if search:
        prefix = search[: -len("search_knowledge_base")]  # e.g. "kb___"
        status, body, _ = rpc(token, "tools/call", {"name": search, "arguments": {
            "query": "how is net revenue defined"}}, session=session)
        text = body.get("result", {}).get("content", [{}])[0].get("text", "")
        hits = json.loads(text).get("results", []) if text.startswith("{") else []
        results.append(check("search through gateway", bool(hits),
                             f"top: {hits[0]['document_id']} ({hits[0]['relevance_score']})" if hits else text[:200]))
        if hits:
            status, body, _ = rpc(token, "tools/call", {"name": f"{prefix}retrieve_document",
                "arguments": {"document_id": hits[0]["document_id"]}}, session=session)
            text = body.get("result", {}).get("content", [{}])[0].get("text", "")
            results.append(check("retrieve_document through gateway", '"content"' in text,
                                 f"{len(text)} bytes"))

    # Negative checks: every one must be refused before reaching a runtime.
    status, _, _ = rpc(None, "tools/list")
    results.append(check("no token is refused", status == 401, f"HTTP {status}"))

    head, payload, sig = token.split(".")
    forged = dict(c, aud="00000000-0000-0000-0000-000000000000")
    fake = ".".join([head, base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("="), sig])
    status, _, _ = rpc(fake, "tools/list")
    results.append(check("tampered token is refused", status in (401, 403), f"HTTP {status}"))

    status, _, _ = rpc(token[:-8] + "AAAAAAAA", "tools/list")
    results.append(check("bad signature is refused", status in (401, 403), f"HTTP {status}"))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    sys.exit(login() if cmd == "login" else test())
