#!/usr/bin/env python3
"""The EKS console's team build, between the console's request and add-team.sh.

Runs inside the platform's CodeBuild project (platform/runner.sh), from the
release bundle:

    python3 platform/team.py args REQUEST.json   # prints add-team.sh flags, one per line
    python3 platform/team.py result REQUEST.json OUT.json
    python3 platform/team.py files REQUEST.json DIR   # DIR/repo-files/: what the team commits

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

import base64
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import eks_add_team  # noqa: E402
import eks_hosts  # noqa: E402

GUID = eks_hosts.GUID
# A repo team's first content: a copy of templates/team-content (runner.sh).
STARTER = eks_add_team.CLONES
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
    if host == "local":
        out = ["--team", team, "--content", content]
    else:
        # Never cloned here: the runner has no git login, and EKS calls no Git
        # host. The team starts from the template (runner.sh copies it to
        # STARTER); the repo's own pipeline publishes the real documents.
        out = ["--team", team, "--content", str(STARTER / team), "--repo-url", content]
    out += ["--prefix", f"{prefix}{team}"]
    # The platform's own region (CodeBuild sets AWS_REGION): the team file must
    # not win (the template's deployment.json says us-east-1).
    if region := os.environ.get("AWS_REGION", ""):
        out += ["--region", region]
        # IAM names are account-wide: the same team in another region needs its own.
        out += ["--ci-role-name", f"{prefix}{team}-{region}-ci-content"]
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
        "signIn": str(req.get("signIn") or "iam"),
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
    team_file = STARTER / req["team"] / "deployment.json"
    if team_file.is_file():
        out["teamFile"] = team_file.read_text()
    out["nextStep"] = (
        f"The server answers from the template's example document for now. EKS did not "
        f"read {url}. To publish your documents: download repo-files.zip below, do the "
        f"steps in your repo (AWS role {role or 'below'}), then run its pipeline."
    )
    out["hostSteps"] = eks_add_team.host_steps(host, parts, team, cfg, role, "repo-files")
    if host == "ado":
        out["adoSetup"] = ado_setup_note(req["team"])
    return out


def repo_files(req: dict[str, Any], dest: pathlib.Path) -> list[str]:
    """The files the team commits to its repo, in dest/repo-files/ (zipped by runner.sh).

    All hosts: the team deployment.json. Azure DevOps: azure-pipelines.yml and
    kb-pipelines/ (eks_hosts.ado_files). GitHub: .github/workflows/eks-deploy.yml.
    """
    _, host = content_of(req)
    out = dest / "repo-files"
    if host == "local":
        return []
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    team = STARTER / req["team"]
    shutil.copy2(team / "deployment.json", out / "deployment.json")
    if host == "ado":
        ado = STARTER / f"{req['team']}-ado"
        shutil.copy2(ado / "azure-pipelines.yml", out / "azure-pipelines.yml")
        shutil.copytree(ado / "kb-pipelines", out / "kb-pipelines")
        # The one script for the Azure DevOps side, filled in (runner.sh: key first).
        cfg, stack = team_stack()
        _, parts = eks_hosts.host_of(content_of(req)[0])
        name = f"ado-setup-{req['team']}.sh"
        (out / name).write_text(
            ado_script(req["team"], parts, cfg, stack, os.environ.get("EKS_KEY_ID", ""))
        )
    if host == "github":
        wf = out / ".github" / "workflows"
        wf.mkdir(parents=True)
        shutil.copy2(team / ".github" / "workflows" / "eks-deploy.yml", wf / "eks-deploy.yml")
    return sorted(str(p.relative_to(dest)) for p in out.rglob("*") if p.is_file())


# What a name clash at change-set validation means, by logical id prefix.
CLASH_HINTS = {
    "AzureDevOpsOidcProvider": "the IAM OIDC provider for this Azure DevOps organization "
    "already exists in this account (another team made it; one per account). Keep the "
    "default access-key mode, or set azureDevOps.oidcProviderArn to the existing one",
    "CiContentRole": "a role with this name exists in this account (the same team in another "
    "region, or a team not fully deleted). Delete that team, or use another team name",
    "GatewayUserRole": "a role with this name exists in this account (the same team elsewhere)",
}


def clash_message(
    stack: str, change_sets: list[dict[str, Any]], events: list[dict[str, Any]]
) -> str:
    """Plain words for a change set CloudFormation refused before deploying anything."""
    lines = []
    for e in events:
        logical = e.get("LogicalResourceId") or ""
        why = next((str(v) for k, v in e.items() if k.endswith("StatusReason") and v), "")
        if not logical or "FAIL" not in json.dumps(e, default=str).upper():
            continue
        hint = next((h for k, h in CLASH_HINTS.items() if logical.startswith(k)), "")
        name = e.get("PhysicalResourceId") or ""
        lines.append(f"- {logical}{f' ({name})' if name else ''}: {hint or why}")
    if not lines:
        lines = [
            f"- {c['StatusReason']}"
            for c in change_sets
            if c.get("Status") == "FAILED" and c.get("StatusReason")
        ]
    if not lines:
        return ""
    return f"CloudFormation refused the team stack {stack} before changing anything:\n" + "\n".join(
        lines
    )


def diagnose(region: str, sess=None) -> str:
    """After a failed install: the refused change set's clashing resources, or ''.
    Reads with the CDK lookup role (read-only), which the runner may assume."""
    cfg = json.loads((REPO / "deployment.json").read_text())
    stack = f"{cfg['prefix']}-stack"
    if sess is None:
        import boto3

        sts = boto3.client("sts", region_name=region)
        acct = sts.get_caller_identity()["Account"]
        q = os.environ.get("CDK_QUALIFIER", "kbmcp")
        arn = f"arn:aws:iam::{acct}:role/cdk-{q}-lookup-role-{acct}-{region}"
        c = sts.assume_role(RoleArn=arn, RoleSessionName="eks-diagnose")["Credentials"]
        sess = boto3.Session(
            aws_access_key_id=c["AccessKeyId"],
            aws_secret_access_key=c["SecretAccessKey"],
            aws_session_token=c["SessionToken"],
            region_name=region,
        )
    cfn = sess.client("cloudformation")
    sets = cfn.list_change_sets(StackName=stack).get("Summaries", [])
    events: list[dict[str, Any]] = []
    failed = [c for c in sets if c.get("Status") == "FAILED"]
    if failed and hasattr(cfn, "describe_events"):
        events = cfn.describe_events(ChangeSetName=failed[-1]["ChangeSetId"]).get(
            "OperationEvents", []
        )
    return clash_message(stack, sets, events)


def team_stack() -> tuple[dict[str, Any], dict[str, Any]]:
    """The team's deployment.json and its stack outputs, as add-team.sh left them."""
    cfg = json.loads((REPO / "deployment.json").read_text())
    return cfg, json.loads((REPO / "infra" / "outputs.json").read_text())[f"{cfg['prefix']}-stack"]


def ado_setup_note(team: str) -> str:
    """The team page's Azure DevOps text: the downloaded script does the rest."""
    name = f"ado-setup-{team}.sh"
    return (
        f"# On your laptop, with your own Azure DevOps PAT (no Azure CLI, no extension):\n"
        f"bash {name} check     # your rights; makes and deletes ci-check-* things\n"
        f"bash {name}           # repo, pipeline, variables, first run\n"
        f"# It asks for the pipeline key's secret: 'Show pipeline key (once)' on this page.\n"
    )


def pipeline_key(stack: dict[str, Any], region: str, iam=None, sm=None) -> str:
    """A new key for the team's Azure DevOps sync user: old keys deleted, the secret
    into the stack's secret (never printed). Returns the key id ('' without a user)."""
    user, arn = stack.get("AzureDevOpsSyncUserName"), stack.get("AzureDevOpsSyncKeySecretArn")
    if not user or not arn:
        return ""
    if iam is None or sm is None:
        import boto3

        iam, sm = (
            boto3.client("iam", region_name=region),
            boto3.client("secretsmanager", region_name=region),
        )
    for k in iam.list_access_keys(UserName=user)["AccessKeyMetadata"]:
        iam.delete_access_key(UserName=user, AccessKeyId=k["AccessKeyId"])
    key = iam.create_access_key(UserName=user)["AccessKey"]
    sm.put_secret_value(SecretId=arn, SecretString=key["SecretAccessKey"])
    return key["AccessKeyId"]


def tools_release() -> tuple[str, str]:
    """The public release the team's pipeline downloads its sync tools from, and its sha256."""
    url = os.environ.get("RELEASE_URL", "")
    if not url:
        rel = (
            json.loads((REPO / "RELEASE.json").read_text())
            if (REPO / "RELEASE.json").is_file()
            else {}
        )
        v, repo = rel.get("version", "dev"), rel.get("repo", "")
        url = f"https://github.com/{repo}/releases/download/{v}/eks-{v}.tar.gz"
    return url, os.environ.get("EKS_TOOLS_SHA256", "")


def ado_script(
    team: str, parts: dict[str, str], cfg: dict[str, Any], stack: dict[str, Any], key_id: str
) -> str:
    """platform/ado-setup.sh with the team's values filled in and the sample repo embedded."""
    url, sha = tools_release()
    values = {
        "ORG_URL": f"https://dev.azure.com/{parts['org']}",
        "PROJECT": parts["project"],
        "REPO": parts["repo"],
        "TEAM": team,
        "REGION": cfg["region"],
        "DOCS_BUCKET": stack.get("DocsBucketName", ""),
        "SOURCE_ID": stack.get("AzureDevOpsSourceId", ""),
        "KB_ID": stack.get("KnowledgeBaseId", ""),
        "DS_ID": stack.get("DataSourceId", ""),
        "MCP_PUBLISHER": stack.get("McpPublisherFunctionName", ""),
        "AWS_ACCESS_KEY_ID": key_id,
        "TOOLS_URL": url,
        "TOOLS_SHA256": sha,
    }
    text = (REPO / "platform" / "ado-setup.sh").read_text()
    for k, v in values.items():
        if not re.match(r"^[\w .:/@+=-]{0,200}$", v):
            raise BadRequest(f"{k}: unexpected value for the script")
        text = text.replace(f'EKS_{k}="${{EKS_{k}:-}}"', f'EKS_{k}="${{EKS_{k}:-{v}}}"')
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        folder = REPO / "templates" / "team-content"
        for f in sorted(folder.rglob("*")):
            if f.is_file() and ".github" not in f.parts and f.name != "deployment.json":
                t.add(f, arcname=f.relative_to(folder).as_posix())
    blob = base64.b64encode(buf.getvalue()).decode()
    lines = [f"# {blob[i : i + 76]}" for i in range(0, len(blob), 76)]
    return text.rstrip("\n") + "\n# ---- sample ----\n" + "\n".join(lines) + "\n"


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
        if cmd == "files":
            print("\n".join(repo_files(req, pathlib.Path(argv[2]))))
            return 0
        if cmd == "diagnose":  # runner.sh, after a failed add-team.sh
            try:
                print(diagnose(os.environ.get("AWS_REGION", "us-east-1")))
            except Exception as exc:  # noqa: BLE001 - best effort; the log has the rest
                print(f"(no diagnosis: {type(exc).__name__}: {str(exc)[:200]})", file=sys.stderr)
            return 0
        if cmd == "key":  # runner.sh, after add-team.sh: prints the new key id
            cfg = json.loads((REPO / "deployment.json").read_text())
            stack = json.loads((REPO / "infra" / "outputs.json").read_text())[
                f"{cfg['prefix']}-stack"
            ]
            print(pipeline_key(stack, cfg["region"]))
            return 0
    except BadRequest as exc:
        print(f"bad request: {exc}", file=sys.stderr)
        return 2
    raise SystemExit(f"usage: team.py args|result|files|key REQUEST.json [OUT.json|DIR]: {cmd}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
