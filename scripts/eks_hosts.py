"""The team repo's git host, for scripts/add-team.sh (docs/add-a-team.md).

EKS never calls a git host API (Azure DevOps, GitHub, GitLab): it does not
create, clone with a host login, grant or look anything up there. It does the
AWS side only, from plain inputs, and prints the steps the team does on the
host itself (templates/host-setup/<host>.md, the same text as the setup page):

    host    AWS side (this file / the stack)              steps on the host
    ado     sync role trusting the service connection     templates/host-setup/azure-devops.md
            (azureDevOps.* in deployment.json), plus
            the pipeline files written to .eks/teams/<team>-ado
    github  cicd/github/deploy-role.yaml (deploy role)     templates/host-setup/github.md
    gitlab  none yet: pushes do not deploy (#95)          templates/host-setup/gitlab.md
    local   none                                          templates/host-setup/local.md
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import urllib.parse
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
STEPS = REPO / "templates" / "host-setup"
# Host -> (template, source provider). Same table in ui/team-setup/src/getStarted.js.
HOSTS = {
    "ado": ("azure-devops.md", "azure-devops"),
    "github": ("github.md", "github"),
    "gitlab": ("gitlab.md", "gitlab"),
    "local": ("local.md", ""),
}
ADO_URL = re.compile(r"^https://(?:[^@/]+@)?dev\.azure\.com/([^/]+)/([^/]+)/_git/([^/?#]+)/?$")
GITHUB_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
GITLAB_URL = re.compile(r"^https://([^/@]*gitlab[^/@]*)/([\w.-]+(?:/[\w.-]+)+?)(?:\.git)?/?$")
GITHUB_IDS = re.compile(r"^[\w.-]+@\d+/[\w.-]+@\d+$")
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
FIELD = re.compile(r"\{\{(\w+)\}\}")


def host_of(content: str) -> tuple[str, dict[str, str]]:
    """Host and repo parts of --content, read from the URL alone (no host call).
    A folder, or a URL of another host, is "local"."""
    if m := ADO_URL.match(content):
        org, project, repo = (urllib.parse.unquote(x) for x in m.groups())
        return "ado", {"org": org, "project": project, "repo": repo}
    if m := GITHUB_URL.match(content):
        return "github", {"repo": m.group(1)}
    if m := GITLAB_URL.match(content):
        return "gitlab", {"repo": m.group(2)}
    return "local", {}


def render(template: str, values: dict[str, str]) -> str:
    """Fill {{field}}s; a field with no value is a bug, not an empty string."""

    def one(m: re.Match) -> str:
        if m.group(1) not in values:
            raise KeyError(f"templates/host-setup: field {{{{{m.group(1)}}}}} has no value")
        return values[m.group(1)]

    return FIELD.sub(one, template)


def steps(host: str, values: dict[str, str]) -> str:
    """The steps on the host, filled in: templates/host-setup/<host>.md."""
    return render((STEPS / HOSTS[host][0]).read_text(), values)


def configure(host: str, parts: dict[str, str], cfg: dict[str, Any], org_id: str) -> None:
    """The host's keys in the team's deployment.json, from plain inputs."""
    provider = HOSTS[host][1]
    if provider:
        first = cfg["sources"][0] if cfg.get("sources") else cfg.setdefault("source", {})
        first["provider"] = provider
    if host != "ado":
        return
    ado = cfg.setdefault("azureDevOps", {})
    ado.update(
        {
            "orgUrl": f"https://dev.azure.com/{parts['org']}",
            "project": parts["project"],
            "repo": parts["repo"],
        }
    )
    ado.setdefault("serviceConnection", "aws-kb-sync")
    if org_id:
        ado["orgId"] = org_id
    if not GUID.match(ado.get("orgId") or ""):
        raise SystemExit(
            f"--ado-org-id: open https://dev.azure.com/{parts['org']}/_apis/connectionData "
            "in a signed-in browser, copy instanceId, and pass it (or set azureDevOps.orgId "
            "in the team's deployment.json)"
        )


def run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if r.returncode:
        raise SystemExit(f"{' '.join(cmd[:3])}: {r.stderr.strip()[:500]}")
    return r.stdout


# ------------------------------------------------------------------ GitHub


def github_trust(repo: str, repo_ids: str, prefix: str, region: str) -> str:
    """The deploy role eks-deploy.yml assumes (cicd/github/deploy-role.yaml). AWS only;
    repo_ids (owner@ID/name@ID) come from the admin, never from GitHub. Re-runnable."""
    import boto3
    from botocore.exceptions import ClientError

    if not repo_ids:
        print(
            "WARN --github-repo-ids not given: the role trusts the repo name only. "
            "Newer repos need the ids (templates/host-setup/github.md step 2)."
        )
    iam = boto3.client("iam", region_name=region)
    provider = next(
        (
            p["Arn"]
            for p in iam.list_open_id_connect_providers()["OpenIDConnectProviderList"]
            if p["Arn"].endswith("/token.actions.githubusercontent.com")
        ),
        "",
    )
    stack = f"{prefix}-github-deploy"
    cfn = boto3.client("cloudformation", region_name=region)
    try:  # an existing stack keeps the provider it made, or the one it was given
        old = cfn.describe_stacks(StackName=stack)["Stacks"][0]
        given = {p["ParameterKey"]: p.get("ParameterValue", "") for p in old.get("Parameters", [])}
        provider = given.get("ExistingProviderArn", "")
    except ClientError as exc:
        if "does not exist" not in str(exc):
            raise
    run(
        [
            "aws",
            "cloudformation",
            "deploy",
            "--region",
            region,
            "--stack-name",
            stack,
            "--template-file",
            str(REPO / "cicd" / "github" / "deploy-role.yaml"),
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "--no-fail-on-empty-changeset",
            "--parameter-overrides",
            f"Prefix={prefix}",
            f"Repo={repo}",
            f"RepoIds={repo_ids or repo}",
            f"ExistingProviderArn={provider}",
        ]
    )
    out = cfn.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    arn = next(o["OutputValue"] for o in out if o["OutputKey"] == "DeployRoleArn")
    print(f"[github] deploy role {arn}")
    return arn


def eks_source() -> tuple[str, str]:
    """The EKS GitHub repo (owner/name) and ref that eks-deploy.yml checks out.

    A release bundle (no git) names its public repo and tag in RELEASE.json
    (factory/release/bundle.sh). A checkout: its origin and commit. Local only."""
    release = REPO / "RELEASE.json"
    if release.is_file():
        r = json.loads(release.read_text())
        return r["repo"], r["version"]

    def git(*args: str) -> str:
        cmd = ["git", "-C", str(REPO), *args]
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()  # noqa: S603

    remote = git("remote", "get-url", "origin")
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", remote)
    ref = git("rev-parse", "HEAD")
    # No github.com origin: a placeholder the host steps show, for the admin to fill.
    return (m.group(1) if m else "OWNER/eks"), ref or "main"


# ------------------------------------------------------------ Azure DevOps


def ado_files(folder: pathlib.Path, dest: pathlib.Path, cfg: dict[str, Any]) -> pathlib.Path:
    """kb-pipelines/ and azure-pipelines.yml, filled from the stack outputs, written to
    dest for the admin to push (templates/host-setup/azure-devops.md steps 5 and 6).
    Local files only: cicd/azure-devops/setup_azure_devops.py stage_repos, no API call."""
    import sys

    sys.path.insert(0, str(REPO / "cicd" / "azure-devops"))
    import setup_azure_devops as ado

    ado_cfg = cfg["azureDevOps"]
    first = cfg["sources"][0] if cfg.get("sources") else cfg["source"]
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    pipes, content = ado.stage_repos(
        dest / "stage",
        ado_cfg["project"],
        first["id"],
        cfg["region"],
        ado_cfg["serviceConnection"],
        folder / "knowledge-base",
    )
    shutil.move(str(pipes), dest / "kb-pipelines")
    shutil.move(str(content / "azure-pipelines.yml"), dest / "azure-pipelines.yml")
    shutil.rmtree(dest / "stage")
    return dest
