#!/usr/bin/env python3
"""Print the mcp.json entry developers add to Kiro (or any MCP client).

Developers need Node.js and an Entra account in the allowed group. They never
need AWS credentials.

    python clients/kiro/make_mcp_config.py

Kiro's built-in OAuth does not send configured scopes to the identity provider
yet (kirodotdev/Kiro#9123), and Entra rejects a sign-in without one. mcp-remote
bridges that: it runs the Entra sign-in with the right scope and forwards MCP
traffic to the gateway.

With "gatewayAuth": "iam" in deployment.json, the entry runs mcp-proxy-for-aws
instead: it signs each request (SigV4) with the developer's own AWS login. Those
developers need uv (for uvx) and an AWS profile allowed to call
bedrock-agentcore:InvokeGateway; replace the profile placeholder with it.

No login in the stack's account? Set "gatewayUserTrustAccount" in
deployment.json to the account you do sign in to. The stack then makes the role
<prefix>-gateway-user (output GatewayUserRoleArn), which may only call this
gateway. Add a role profile to ~/.aws/config and use its name here:

    [profile kb-user]
    role_arn = <GatewayUserRoleArn>
    source_profile = <your existing profile in the trusted account>
    region = <region>

With the stack's own Cognito pool (identity.cognito.create), the entry is a
plain remote server: Kiro's built-in OAuth finds Cognito through the gateway's
protected-resource metadata and signs in with the shared public client (PKCE).
Nothing to install besides Kiro. The same client id works in any MCP client:
the gateway checks the token's scope, not which app asked. The script also
prints a Claude Code command and a VS Code entry with that id.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "infra"))
sys.path.insert(0, str(REPO))
from identity.config import migrate, provider  # noqa: E402
from settings import deployment, outputs  # noqa: E402

CALLBACK_PORT = 3334  # fixed: Entra accepts http://localhost/oauth/callback on any port
MCP_REMOTE = "mcp-remote@0.16.0"
MCP_PROXY_FOR_AWS = "mcp-proxy-for-aws-cli@latest"
# Must match a callback of the shared public client (infra/stacks/cognito_identity.py).
CLAUDE_CALLBACK_PORT = 7779
PROFILE_PLACEHOLDER = "<your AWS profile for the solution account>"


def main() -> int:
    cfg, out = migrate(deployment()), outputs()
    name = cfg.get("serverName") or cfg.get("prefix", "kb-mcp")
    if cfg.get("gatewayAuth") == "iam":
        entry = {
            name: {
                "command": "uvx",
                "args": [
                    MCP_PROXY_FOR_AWS,
                    out["GatewayUrl"],
                    "--service", "bedrock-agentcore",
                    "--region", cfg.get("region", "ap-south-1"),
                    "--profile", PROFILE_PLACEHOLDER,
                ],
                "disabled": False,
            }
        }
        return _print(entry)
    if (cfg.get("identity") or {}).get("cognito", {}).get("create") and out.get(
        "CognitoKiroClientId"
    ):
        _print(kiro_entry(name, out))
        print("\nClaude Code (one command):\n")
        print(claude_command(name, out))
        print("\nVS Code, inside \"servers\" in .vscode/mcp.json (paste the client id")
        print(f"{out['CognitoKiroClientId']} when VS Code asks for one):\n")
        print(json.dumps(vscode_entry(name, out), indent=2)[1:-1].strip())
        return 0
    if provider(cfg) != "entra":
        # shortcut: only Entra has a browser sign-in entry; other OIDC providers
        # need their own mcp-remote flags (docs/identity.md, follow-up issue).
        # Not an error: the install is done, only the printed entry is missing.
        print("No Kiro entry yet for this OIDC provider; see docs/identity.md.")
        return 0
    client_id = cfg["identity"]["entra"]["clientId"]
    entry = {
        name: {
            "command": "npx",
            "args": [
                "-y", MCP_REMOTE,
                out["GatewayUrl"],
                str(CALLBACK_PORT),
                "--static-oauth-client-info",
                json.dumps({"client_id": client_id,
                            "redirect_uris": [f"http://localhost:{CALLBACK_PORT}/oauth/callback"]},
                           separators=(",", ":")),
                "--static-oauth-client-metadata",
                json.dumps({"scope": f"api://{client_id}/kb.read offline_access",
                            "token_endpoint_auth_method": "none"}, separators=(",", ":")),
                # Entra v2 rejects the RFC 8707 resource parameter (AADSTS9010010).
                "--disable-resource-parameter",
            ],
            "disabled": False,
        }
    }
    return _print(entry)


def kiro_entry(name: str, out: dict) -> dict:
    """Remote MCP entry for Kiro's built-in OAuth with the stack's Cognito pool."""
    scopes = out["CognitoKiroScopes"].split()
    return {
        name: {
            "url": out["GatewayUrl"],
            # Same scopes in both places: Kiro versions differ in which one wins.
            "oauthScopes": scopes,
            "oauth": {
                "clientId": out["CognitoKiroClientId"],
                "redirectUri": out["CognitoKiroRedirectUri"],
                "oauthScopes": scopes,
            },
            "disabled": False,
        }
    }


def claude_command(name: str, out: dict) -> str:
    """claude mcp add with the shared public client and its fixed callback port."""
    return (
        f"claude mcp add --transport http --client-id {out['CognitoKiroClientId']} "
        f"--callback-port {CLAUDE_CALLBACK_PORT} {name} {out['GatewayUrl']}"
    )


def vscode_entry(name: str, out: dict) -> dict:
    return {name: {"type": "http", "url": out["GatewayUrl"]}}


def _print(entry: dict) -> int:
    print("Add this inside \"mcpServers\" in ~/.kiro/settings/mcp.json:\n")
    print(json.dumps(entry, indent=2)[1:-1].strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
