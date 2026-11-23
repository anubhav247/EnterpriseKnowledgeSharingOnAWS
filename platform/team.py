#!/usr/bin/env python3
"""The EKS console's team build, between the console's request and add-team.sh.

Runs inside the platform's CodeBuild project (platform/runner.sh), from the
release bundle:

    python3 platform/team.py args REQUEST.json   # prints add-team.sh flags, one per line
    python3 platform/team.py result REQUEST.json OUT.json

args: the console request is untrusted input (any signed-in admin can send
any JSON), so each field is checked here against a strict pattern before it
becomes a flag. Nothing is passed through a shell.

result: what the console shows after the install: gateway URL, the Kiro
mcp.json entry, the team deployment.json to commit, and the steps left on the
Git host (templates/host-setup/<host>.md, the same text add-team.sh prints).
EKS calls no Git host API here either.

Env: TEAM_PREFIX (stack prefix before the team name, default eks-),
RUNNER_ROLE_ARN (the CodeBuild role: the team's content roles trust it).
Guide: docs/platform.md.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import eks_add_team  # noqa: E402
import eks_hosts  # noqa: E402

GUID = eks_hosts.GUID
CLIENT = re.compile(r"^[\w.:/@-]{1,200}$")
ISSUER = re.compile(r"^https://[^\s/?#]+(/[^\s?#]*)?$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,}$", re.I)
SIGN_IN = ("iam", "entra", "oidc", "cognito")


class BadRequest(ValueError):
    pass


def _field(req: dict[str, Any], key: str, pattern: re.Pattern[str], what: str) -> str:
    value = str(req.get(key) or "").strip()
    if value and not pattern.match(value):
        raise BadRequest(f"{key}: {what}")
    return value


def content_of(req: dict[str, Any]) -> tuple[str, str]:
    """(--content value, host). Empty URL = the bundled sample documents."""
    url = str(req.get("content") or "").strip()
    if not url:
        return str(REPO / "sample-content"), "local"
    if len(url) > 300 or not url.startswith("https://"):
        raise BadRequest("content: the https URL of the team's docs repo, or empty for sample docs")
    host, _ = eks_hosts.host_of(url)
    if host == "local":
        raise BadRequest("content: a GitHub, GitLab or Azure DevOps repo URL")
    return url, host


def flags(req: dict[str, Any], prefix: str, role_arn: str) -> list[str]:
    """add-team.sh flags for one console request. Raises BadRequest."""
    team = str(req.get("team") or "")
    if not eks_add_team.TEAM_RE.match(team):
        raise BadRequest("team: 2-20 lowercase letters, digits and -, starts with a letter")
    content, host = content_of(req)
    out = ["--team", team, "--content", content, "--prefix", f"{prefix}{team}"]
    out += ["--artifact", "code", "--ci-trust-role-arn", role_arn]
    sign_in = str(req.get("signIn") or "iam")
    if sign_in not in SIGN_IN:
        raise BadRequest(f"signIn: one of {', '.join(SIGN_IN)}")
    out += ["--auth", sign_in]
    if sign_in == "entra":
        tenant = _field(req, "tenantId", GUID, "the directory (tenant) id, a GUID")
        client = _field(req, "clientId", GUID, "the application (client) id, a GUID")
        if not (tenant and client):
            raise BadRequest("entra: tenantId and clientId")
        out += ["--entra-tenant-id", tenant, "--entra-client-id", client]
    if sign_in == "oidc":
        issuer = _field(req, "issuer", ISSUER, "the issuer URL, https://...").rstrip("/")
        client = _field(req, "clientId", CLIENT, "the client id")
        audience = _field(req, "audience", CLIENT, "the token audience") or client
        if not (issuer and audience):
            raise BadRequest("oidc: issuer and clientId")
        out += ["--oidc-discovery-url", f"{issuer}/.well-known/openid-configuration"]
        out += ["--oidc-audience", audience]
        if client:
            out += ["--oidc-client", client]
    if host == "github":
        ids = _field(req, "githubRepoIds", eks_hosts.GITHUB_IDS, "owner@OWNER_ID/name@REPO_ID")
        if ids:
            out += ["--github-repo-ids", ids]
    if host == "ado":
        org = _field(req, "adoOrgId", GUID, "the organization id, a GUID")
        if org:
            out += ["--ado-org-id", org]
    email = _field(req, "alertEmail", EMAIL, "an e-mail address")
    if email:
        out += ["--alert-email", email]
    if req.get("monitoring") is False:
        out += ["--monitoring", "off"]
    return out


def _stack_output(stack: str, key: str, region: str) -> str:
    import boto3

    try:
        outs = (
            boto3.client("cloudformation", region_name=region)
            .describe_stacks(StackName=stack)["Stacks"][0]
            .get("Outputs", [])
        )
    except Exception:  # noqa: BLE001 - no such stack: no role to show
        return ""
    return next((o["OutputValue"] for o in outs if o["OutputKey"] == key), "")


def result(req: dict[str, Any]) -> dict[str, Any]:
    """What the console shows once add-team.sh has finished."""
    cfg = json.loads((REPO / "deployment.json").read_text())
    stack = json.loads((REPO / "infra" / "outputs.json").read_text())[f"{cfg['prefix']}-stack"]
    team = cfg.get("serverName", "").removesuffix("-kb") or req["team"]
    url, host = content_of(req)
    _, parts = eks_hosts.host_of(url) if host != "local" else ("local", {})
    mcp = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(REPO / "clients" / "kiro" / "make_mcp_config.py")],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    out: dict[str, Any] = {
        "team": req["team"],
        "prefix": cfg["prefix"],
        "serverName": cfg.get("serverName", ""),
        "gatewayUrl": stack.get("GatewayUrl", ""),
        "host": host,
        "kiro": mcp.stdout.strip() or mcp.stderr.strip(),
        "roles": {k: v for k, v in stack.items() if k.endswith("RoleArn")},
    }
    if host == "local":
        return out
    role = ""
    if host == "github":
        role = _stack_output(f"{cfg['prefix']}-github-deploy", "DeployRoleArn", cfg["region"])
    if host == "ado":
        role = stack.get("AzureDevOpsSyncRoleArn", "")
    team_file = eks_add_team.CLONES / req["team"] / "deployment.json"
    if team_file.is_file():
        out["teamFile"] = team_file.read_text()
    out["hostSteps"] = eks_add_team.host_steps(host, parts, team, cfg, role)
    return out


def main(argv: list[str]) -> int:
    cmd, path = argv[0], pathlib.Path(argv[1])
    req = json.loads(path.read_text())
    try:
        if cmd == "args":
            prefix = os.environ.get("TEAM_PREFIX", "eks-")
            print("\n".join(flags(req, prefix, os.environ["RUNNER_ROLE_ARN"])))
            return 0
        if cmd == "result":
            pathlib.Path(argv[2]).write_text(json.dumps(result(req), indent=2) + "\n")
            return 0
    except BadRequest as exc:
        print(f"bad request: {exc}", file=sys.stderr)
        return 2
    raise SystemExit(f"usage: team.py args|result REQUEST.json [OUT.json]: {cmd}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
