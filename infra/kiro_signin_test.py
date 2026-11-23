#!/usr/bin/env python3
"""Kiro browser sign-in against the stack's Cognito pool, tested without a person.

Kiro's built-in remote-MCP OAuth does: POST the gateway without a token ->
401 with resource_metadata -> protected-resource metadata -> Cognito's OIDC
metadata -> browser to /oauth2/authorize (PKCE, Kiro's fixed redirect URI) ->
code -> token -> MCP calls with the token. This script does the same steps:

1. demo user   create demo@example.com if missing, set the password from the
               secret ekb/demo/<prefix>/demo-user as permanent (no change at
               first sign-in), and sign it in with that password
2. discovery   the 401, the metadata chain, issuer match
3. browser     authorization code + PKCE through the Cognito login page, with
               a CI-only user, then tools/list and a search with that token
4. any client  the same browser flow with a 2nd public client (Claude Code's
               callback): the gateway takes it, no client list
5. no scope    AdminInitiateAuth on the Kiro client: a valid user token, but
               without this server's scope: the gateway must refuse it

Annotations also print the Kiro mcp.json entry and the demo secret ARN.
Passwords, tokens and codes are never printed. Needs the stack's CI content
role (Cognito admin calls on the pool, read on the demo secret).
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import pathlib
import re
import secrets
import string
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

import boto3

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "clients" / "kiro"))
from make_mcp_config import claude_command, kiro_entry  # noqa: E402
from signin_smoke_test import QUESTION, TIMEOUT, Mcp, annotate, refused_token  # noqa: E402

from settings import deployment, outputs  # noqa: E402

CI_USERNAME = "ci-kiro-signin"


def random_password() -> str:
    # Meets Cognito's default policy: upper, lower, digit, symbol.
    body = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
    return f"Aa1!{body}"


def ensure_user(cognito, pool: str, username: str) -> bool:
    """Create the user if missing; True when created."""
    try:
        cognito.admin_get_user(UserPoolId=pool, Username=username)
        return False
    except cognito.exceptions.UserNotFoundException:
        pass
    attrs = []
    if "@" in username:
        attrs = [{"Name": "email", "Value": username}, {"Name": "email_verified", "Value": "true"}]
    cognito.admin_create_user(
        UserPoolId=pool, Username=username, UserAttributes=attrs, MessageAction="SUPPRESS"
    )
    return True


def search(url: str, access_token: str, who: str) -> list[str]:
    mcp = Mcp(url, access_token)
    mcp.call(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": who, "version": "1"},
        },
    )
    names = [t["name"] for t in mcp.call("tools/list")["tools"]]
    tool = next(n for n in names if n.endswith("search_knowledge_base"))
    result = mcp.call("tools/call", {"name": tool, "arguments": {"query": QUESTION}})
    if result.get("isError"):
        raise RuntimeError(result["content"][0]["text"][:300])
    return [r["document_id"] for r in json.loads(result["content"][0]["text"]).get("results", [])]


def get_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:  # noqa: S310
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def discovery(out: dict) -> bool:
    """The metadata chain Kiro follows from an unauthenticated call."""
    req = urllib.request.Request(  # noqa: S310
        out["GatewayUrl"],
        data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT)  # noqa: S310
        raise RuntimeError("the gateway answered without a token")
    except urllib.error.HTTPError as exc:
        status, challenge = exc.code, exc.headers.get("WWW-Authenticate", "")
    m = re.search(r'resource_metadata="([^"]+)"', challenge)
    if status != 401 or not m:
        raise RuntimeError(f"expected 401 with resource_metadata, got {status} {challenge!r}")
    code, prm = get_json(m.group(1))
    servers = prm.get("authorization_servers") or []
    if code != 200 or not servers:
        raise RuntimeError(f"protected-resource metadata {code}: {json.dumps(prm)[:300]}")
    issuer = servers[0].removesuffix("/.well-known/openid-configuration")
    # The URLs Kiro tries for a path issuer (rmcp), in order.
    parts = urllib.parse.urlsplit(issuer)
    base, path = f"{parts.scheme}://{parts.netloc}", parts.path.rstrip("/")
    tried = [
        f"{base}/.well-known/oauth-authorization-server{path}",
        f"{base}/.well-known/openid-configuration{path}",
        f"{base}{path}/.well-known/openid-configuration",
    ]
    found = None
    for url in tried:
        code, meta = get_json(url)
        if code == 200 and meta.get("authorization_endpoint"):
            found = (url, meta)
            break
    if not found:
        raise RuntimeError(f"no authorization-server metadata under {issuer}")
    url, meta = found
    ok = meta.get("issuer") == servers[0] and prm.get("resource") == out["GatewayUrl"]
    annotate(
        "notice" if ok else "error",
        f"Kiro discovery {'PASS' if ok else 'FAIL'}",
        f"401 -> {m.group(1)} -> authorization_servers {servers} (resource "
        f"{prm.get('resource')}) -> {url} (issuer {meta.get('issuer')}, "
        f"authorize {meta.get('authorization_endpoint')})",
    )
    return ok


class _StopAtCallback(urllib.request.HTTPRedirectHandler):
    """Follow Cognito's redirects, but keep the one to Kiro's loopback callback."""

    def __init__(self, callback: str) -> None:
        self.callback, self.landed = callback, ""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if newurl.startswith(self.callback):
            self.landed = newurl
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def browser_code(
    out: dict, client: tuple[str, str], username: str, password: str, resource: bool
) -> tuple[str, str]:
    """Authorization code + PKCE through the Cognito login page, as Kiro opens it.

    client is (client id, redirect URI).
    """
    base, callback = out["CognitoLoginBaseUrl"], client[1]
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": client[0],
        "redirect_uri": callback,
        "scope": out["CognitoKiroScopes"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if resource:
        params["resource"] = out["GatewayUrl"]
    stop = _StopAtCallback(callback)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()), stop)
    try:
        with opener.open(  # noqa: S310
            f"{base}/oauth2/authorize?{urllib.parse.urlencode(params)}", timeout=TIMEOUT
        ) as r:
            login_url, page = r.geturl(), r.read().decode()
    except urllib.error.HTTPError as exc:
        if not stop.landed:
            raise RuntimeError(f"authorize HTTP {exc.code}: {exc.read()[:200]!r}") from None
    if stop.landed:
        raise RuntimeError(f"no login page: {_error(stop.landed)}")
    csrf = re.search(r'name="_csrf"\s+value="([^"]+)"', page) or re.search(
        r'value="([^"]+)"\s+name="_csrf"', page
    )
    if not csrf:
        raise RuntimeError(f"no login form at {login_url.split('?')[0]}: {page[:200]!r}")
    form = urllib.parse.urlencode(
        {
            "_csrf": html.unescape(csrf.group(1)),
            "username": username,
            "password": password,
            "signInSubmitButton": "Sign in",
        }
    ).encode()
    try:
        opener.open(login_url, data=form, timeout=TIMEOUT).read()  # noqa: S310
    except urllib.error.HTTPError as exc:
        if not stop.landed:
            raise RuntimeError(f"login POST HTTP {exc.code}") from None
    if not stop.landed:
        raise RuntimeError("login did not redirect to the Kiro callback")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(stop.landed).query))
    if "code" not in query or query.get("state") != state:
        raise RuntimeError(f"callback without a code: {_error(stop.landed)}")
    return query["code"], verifier


def _error(url: str) -> str:
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    return f"{q.get('error', '?')} {q.get('error_description', '')}".strip()


def browser_token(
    out: dict, client: tuple[str, str], code: str, verifier: str, resource: bool
) -> str:
    body = {
        "grant_type": "authorization_code",
        "client_id": client[0],
        "code": code,
        "redirect_uri": client[1],
        "code_verifier": verifier,
    }
    if resource:
        body["resource"] = out["GatewayUrl"]
    req = urllib.request.Request(  # noqa: S310
        f"{out['CognitoLoginBaseUrl']}/oauth2/token",
        data=urllib.parse.urlencode(body).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"token HTTP {exc.code}: {exc.read()[:200]!r}") from None


def main() -> int:
    cfg, out = deployment(), outputs()
    pool = out["CognitoUserPoolId"]
    cognito = boto3.client("cognito-idp", region_name=cfg.get("region"))
    failures = 0

    def check(title: str, fn) -> None:  # noqa: ANN001
        nonlocal failures
        try:
            failures += 0 if fn() is not False else 1
        except Exception as exc:  # noqa: BLE001
            failures += 1
            annotate("error", f"{title} FAIL", f"{type(exc).__name__}: {exc}"[:500])

    def demo_user() -> bool:
        secret = json.loads(
            boto3.client("secretsmanager", region_name=cfg.get("region")).get_secret_value(
                SecretId=out["DemoUserSecretArn"]
            )["SecretString"]
        )
        created = ensure_user(cognito, pool, secret["username"])
        # Permanent: the secret is the password, no forced change at first
        # sign-in. Each deploy resets it, so a changed password reverts.
        cognito.admin_set_user_password(
            UserPoolId=pool,
            Username=secret["username"],
            Password=secret["password"],
            Permanent=True,
        )
        auth = cognito.admin_initiate_auth(
            UserPoolId=pool,
            ClientId=out["CognitoKiroClientId"],
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": secret["username"], "PASSWORD": secret["password"]},
        )
        ok = "AuthenticationResult" in auth
        annotate(
            "notice" if ok else "error",
            f"Kiro demo user sign-in {'PASS' if ok else 'FAIL'}",
            f"{secret['username']} {'created' if created else 'exists'}; signs in with the "
            f"password in Secrets Manager {out['DemoUserSecretArn']}"
            + ("" if ok else f" (got challenge {auth.get('ChallengeName')})"),
        )
        return ok

    check("Kiro demo user", demo_user)
    check("Kiro discovery", lambda: discovery(out))

    # CI-only user: a fresh permanent password each run, never printed.
    password = random_password()
    ensure_user(cognito, pool, CI_USERNAME)
    cognito.admin_set_user_password(
        UserPoolId=pool, Username=CI_USERNAME, Password=password, Permanent=True
    )

    kiro = (out["CognitoKiroClientId"], out["CognitoKiroRedirectUri"])
    second = (out["CognitoSecondClientId"], out["CognitoSecondRedirectUri"])

    def browser() -> bool:
        # Kiro sends the RFC 8707 resource parameter; if Cognito refuses it,
        # say so plainly and retry without it to tell the two causes apart.
        try:
            code, verifier = browser_code(out, kiro, CI_USERNAME, password, resource=True)
            access = browser_token(out, kiro, code, verifier, resource=True)
            how = "with resource parameter"
        except RuntimeError as exc:
            annotate("error", "Kiro browser sign-in with resource FAIL", str(exc)[:400])
            code, verifier = browser_code(out, kiro, CI_USERNAME, password, resource=False)
            access = browser_token(out, kiro, code, verifier, resource=False)
            how = "only WITHOUT resource parameter"
        ids = search(out["GatewayUrl"], access, "kiro-browser-test")
        ok = bool(ids) and how.startswith("with")
        annotate(
            "notice" if ok else "error",
            f"Kiro browser sign-in {'PASS' if ok else 'FAIL'}",
            f"PKCE code flow on the Cognito login page ({how}); gateway accepted the "
            f"Kiro client token; '{QUESTION}' -> top {ids[:3]}",
        )
        return ok

    def any_client() -> bool:
        code, verifier = browser_code(out, second, CI_USERNAME, password, resource=True)
        access = browser_token(out, second, code, verifier, resource=True)
        ids = search(out["GatewayUrl"], access, "second-public-client-test")
        annotate(
            "notice" if ids else "error",
            f"Second public client accepted {'PASS' if ids else 'FAIL'}",
            f"PKCE sign-in with another public client (not listed anywhere) holding "
            f"{out['CognitoScope']}; top {ids[:3]}",
        )
        return bool(ids)

    def no_scope() -> bool:
        # AdminInitiateAuth tokens hold only aws.cognito.signin.user.admin.
        access = cognito.admin_initiate_auth(
            UserPoolId=pool,
            ClientId=out["CognitoKiroClientId"],
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": CI_USERNAME, "PASSWORD": password},
        )["AuthenticationResult"]["AccessToken"]
        return refused_token(
            out,
            access,
            "Token without this server's scope refused",
            f"user token from the shared public client without {out['CognitoScope']}",
        )

    check("Kiro browser sign-in", browser)
    check("Second public client accepted", any_client)
    check("Token without this server's scope refused", no_scope)

    entry = json.dumps(kiro_entry(cfg.get("prefix", "kb-mcp"), out), separators=(",", ":"))
    annotate("notice", "Kiro mcp.json entry", entry)
    annotate("notice", "Claude Code command", claude_command(cfg.get("prefix", "kb-mcp"), out))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
