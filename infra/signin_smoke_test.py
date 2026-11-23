#!/usr/bin/env python3
"""Real OIDC sign-in through the gateway, with the stack's Cognito test pool.

1. The test machine client gets a token with this server's scope (client
   credentials) and calls search_knowledge_base: results expected.
2. The other client gets a token from the same pool, for another app's scope:
   the gateway must refuse it (HTTP 401 or 403). No client list is involved.

Needs a stack with identity.cognito.create (deploy/ekb-oidc-test.json) and AWS
credentials that may call cognito-idp:DescribeUserPoolClient on the pool (the
stack's CI content role). Results print as GitHub Actions notices or errors.
Tokens and secrets are never printed.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

import boto3

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from settings import deployment, outputs  # noqa: E402

QUESTION = "What is the freshness target for the Curated layer, and how is it measured?"
TIMEOUT = 60


def annotate(level: str, title: str, message: str) -> None:
    print(f"{title}: {message}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        flat = message.replace("%", "%25").replace("\r", " ").replace("\n", " ")
        print(f"::{level} title={title}::{flat}")


def token(out: dict, client_id: str, scope: str) -> str:
    cognito = boto3.client("cognito-idp", region_name=deployment().get("region"))
    secret = cognito.describe_user_pool_client(
        UserPoolId=out["CognitoUserPoolId"], ClientId=client_id
    )["UserPoolClient"]["ClientSecret"]
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": scope}).encode()
    req = urllib.request.Request(  # noqa: S310  https URL from the stack outputs
        out["CognitoTokenUrl"],
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310
        return json.load(r)["access_token"]


class Mcp:
    """Minimal MCP over streamable HTTP: JSON-RPC posts with a bearer token."""

    def __init__(self, url: str, bearer: str) -> None:
        self.url, self.bearer, self.session = url, bearer, ""

    def call(self, method: str, params: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.bearer}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        req = urllib.request.Request(  # noqa: S310  https gateway URL from the outputs
            self.url, data=json.dumps(body).encode(), headers=headers
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310
            self.session = r.headers.get("Mcp-Session-Id") or self.session
            raw = r.read().decode()
        if "data:" in raw[:200]:
            raw = next(line[5:] for line in raw.splitlines() if line.startswith("data:"))
        msg = json.loads(raw)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg["result"]


def allowed(out: dict) -> bool:
    mcp = Mcp(out["GatewayUrl"], token(out, out["CognitoTestClientId"], out["CognitoScope"]))
    mcp.call(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "signin-smoke", "version": "1"},
        },
    )
    names = [t["name"] for t in mcp.call("tools/list")["tools"]]
    # The gateway prefixes tool names with the target: kb___search_knowledge_base.
    search = next(n for n in names if n.endswith("search_knowledge_base"))
    result = mcp.call("tools/call", {"name": search, "arguments": {"query": QUESTION}})
    if result.get("isError"):
        raise RuntimeError(result["content"][0]["text"][:300])
    hits = json.loads(result["content"][0]["text"])
    ids = [r["document_id"] for r in hits.get("results", [])]
    ok = bool(ids)
    annotate(
        "notice" if ok else "error",
        f"Sign-in allowed client {'PASS' if ok else 'FAIL'}",
        f"Cognito test client signed in; tools {names}; '{QUESTION}' -> top {ids[:3]}"
        f"; content {[c.get('type') for c in result['content']]}"
        f"; link {(hits.get('results') or [{}])[0].get('source_url', 'none')}",
    )
    return ok


def refused(out: dict) -> bool:
    bearer = token(out, out["CognitoOtherClientId"], out["CognitoOtherScope"])
    return refused_token(
        out, bearer, "Sign-in other client", "Cognito other client (another app's scope)"
    )


def refused_token(out: dict, bearer: str, title: str, who: str) -> bool:
    """The gateway must answer 401 or 403 to this token."""
    mcp = Mcp(out["GatewayUrl"], bearer)
    try:
        mcp.call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "signin-smoke", "version": "1"},
            },
        )
        mcp.call("tools/list")
    except urllib.error.HTTPError as exc:
        ok = exc.code in (401, 403)
        annotate(
            "notice" if ok else "error",
            f"{title} {'PASS' if ok else 'FAIL'}",
            f"{who} got HTTP {exc.code}",
        )
        return ok
    annotate("error", f"{title} FAIL", f"the gateway accepted a token it must refuse: {who}")
    return False


def main() -> int:
    out = outputs()
    failures = 0
    for check in (allowed, refused):
        try:
            failures += 0 if check(out) else 1
        except Exception as exc:  # noqa: BLE001
            failures += 1
            annotate(
                "error", f"Sign-in {check.__name__} FAIL", f"{type(exc).__name__}: {exc}"[:500]
            )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
