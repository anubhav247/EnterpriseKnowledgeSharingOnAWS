#!/usr/bin/env python3
"""Add a team: deploy one EKS stack for one team's content repository.

Run it through the wrapper, which finds a Python with the deps first:

    scripts/add-team.sh --team legal --content ../legal-content
    scripts/add-team.sh --team legal --content https://github.com/acme/legal-content.git \\
        --account 123456789012 --region eu-west-1 --auth entra \\
        --entra-tenant-id ... --entra-client-id ...

The content repository is a copy of templates/team-content/: knowledge-base/,
mcp/instructions.md, mcp/prompts/, deployment.json and smoke.json. Its
deployment.json holds the team's values; any install flag overrides them. Not
set there, they come from the team name: prefix eks-<team>, source id <team>,
server name <team>-kb (also the Kiro mcp.json entry name).

Then it runs scripts/eks_install.py install with the same flags: deploy, load
knowledge-base/, publish mcp/, smoke test with smoke.json, print the Kiro entry.

EKS never calls a git host API. The team copies the template into a repo it
already has; a git URL is cloned with plain git and the admin's own git login.
From the URL alone (dev.azure.com, github.com, a gitlab host) the command does
the AWS side of that host's CI (scripts/eks_hosts.py), writes the team
deployment.json to commit, and prints the steps left on the host
(templates/host-setup/<host>.md, the same text as the setup page).

--draft deploys the team's draft server instead (access.draftServer true):
prefix <prefix>-draft, server <team>-draft-kb, each source read from its
draftBranch (default "draft"), sign-in from access.draftIdentity. The config
stays the one on the main branch; only documents, mcp/ and smoke.json come from
the draft branch. Use its own checkout (one deployment.json per checkout).
access.proposals true also turns on the propose_change tool there, and only
there: PRs/MRs into each source's draftBranch (docs/propose-change.md).
Guide: docs/add-a-team.md.
"""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import eks_oidc_check  # noqa: E402
import eks_request_pack  # noqa: E402

import eks_hosts  # noqa: E402
import eks_install  # noqa: E402

sys.path.insert(0, str(HERE.parent / "sync"))
import source_links  # noqa: E402

TEAM_RE = re.compile(r"^[a-z][a-z0-9-]{0,18}[a-z0-9]$")
CLONES = eks_install.REPO / ".eks" / "teams"


GONE = ("--host", "--repo", "--new-repo", "--org", "--project", "--creator-can-approve")


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    p = argparse.ArgumentParser(
        prog="eks_add_team.py", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    p.add_argument("--team", required=True, help="team name, lowercase (legal, procurement)")
    p.add_argument(
        "--content",
        help="team content repo, a copy of templates/team-content: a folder or a git URL "
        "(cloned with plain git and your own git login)",
    )
    p.add_argument("--prefix", help="resource name prefix (default: team file, else eks-<team>)")
    p.add_argument("--draft", action="store_true", help="deploy the draft server (see above)")
    h = p.add_argument_group("the team repo's CI: AWS side only, EKS never calls the git host")
    h.add_argument(
        "--github-repo-ids",
        help="GitHub: owner@OWNER_ID/name@REPO_ID for the deploy role "
        "(templates/host-setup/github.md step 2)",
    )
    h.add_argument(
        "--ado-org-id",
        help="Azure DevOps: the organization id (instanceId of <org>/_apis/connectionData)",
    )
    r = p.add_argument_group(
        "IT request pack: write the tickets for IT and stop (docs/it-request-packs.md)"
    )
    r.add_argument("--request-pack", metavar="DIR", help="write the pack here; changes nothing")
    r.add_argument("--it", default="", help="what IT does, comma list: sign-in,aws,ado")
    r.add_argument("--owner", default="", help="the team admin's e-mail / user name")
    r.add_argument("--reader-members", default="", help="readers group members (default owner)")
    r.add_argument("--publisher-members", default="", help="publishers members (default owner)")
    r.add_argument("--installer-principal-arn", default="", help="aws: who may assume the role")
    r.add_argument("--ado-org", default="", help="ado: the organization IT works in")
    r.add_argument("--ado-project", default="", help="ado: the project IT makes")
    r.add_argument("--ado-repo", default="", help="ado: the repo IT makes")
    for flag in GONE:  # removed: EKS no longer creates or sets up repos on the host
        p.add_argument(flag, nargs="?", const=True, help=argparse.SUPPRESS)
    a, rest = p.parse_known_args(argv)
    given = [f for f in GONE if getattr(a, f[2:].replace("-", "_")) is not None]
    if given:
        p.error(
            f"{given[0]} is gone: EKS does not create or set up repos. Copy "
            "templates/team-content into your repo (its README), then use --content <repo URL>"
        )
    if not a.content and not a.request_pack:
        p.error("give --content: a folder or the URL of your copy of templates/team-content")
    if not TEAM_RE.match(a.team):
        p.error("--team: 2-20 chars, lowercase letters, digits and '-', start with a letter")
    if a.github_repo_ids and not eks_hosts.GITHUB_IDS.match(a.github_repo_ids):
        p.error("--github-repo-ids: owner@OWNER_ID/name@REPO_ID")
    return a, rest


def content_folder(team: str, content: str) -> pathlib.Path:
    """The content repo as a local folder; a git URL is cloned into .eks/teams/<team>."""
    if not re.match(r"^(https://|git@|ssh://)", content):
        return pathlib.Path(content).resolve()
    return eks_install.clone(content, CLONES / team)


def team_config(team: str, folder: pathlib.Path) -> dict[str, Any]:
    """deployment.json from the team repo, with the team-name defaults filled in."""
    path = folder / "deployment.json"
    cfg: dict[str, Any] = json.loads(path.read_text()) if path.is_file() else {}
    cfg.setdefault("_comment", f"Team {team}, written by scripts/add-team.sh from {path}.")
    cfg.setdefault("prefix", f"eks-{team}")
    if not cfg.get("sources"):  # one repo; "sources" lists several (docs/add-a-team.md)
        cfg.setdefault("source", {}).setdefault("id", team)
    cfg.setdefault("serverName", f"{team}-kb")
    return cfg


def draft_config(team: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """The draft server's config: its own names, roles and sign-in, draft branches."""
    access = cfg.get("access") or {}
    if not access.get("draftServer"):
        raise SystemExit('--draft: set "access": {"draftServer": true} in deployment.json')
    if cfg.get("gatewayAuth") == "oidc" and not access.get("draftIdentity"):
        # Same sign-in app = every reader's token opens the drafts too.
        raise SystemExit("--draft: set access.draftIdentity, the publishers-only sign-in app")
    d = copy.deepcopy(cfg)
    d["_comment"] = f"Team {team} draft server, written by scripts/add-team.sh --draft."
    d["prefix"] = f"{cfg['prefix']}-draft"
    d["serverName"] = f"{cfg['serverName'].removesuffix('-kb')}-draft-kb"
    if access.get("draftIdentity"):
        d["identity"] = access["draftIdentity"]
    # The Azure DevOps pipeline syncs main only, and its OIDC provider URL may
    # exist once per account: a second one fails the draft stack.
    d.pop("azureDevOps", None)
    for s in d.get("sources") or [d.setdefault("source", {"id": team})]:
        s["branch"] = s.get("draftBranch", "draft")
    if (d.get("ciContentRole") or {}).get("roleName"):
        d["ciContentRole"]["roleName"] += "-draft"  # role names are per account
    return d


def proposal_targets(cfg: dict[str, Any], folder: pathlib.Path) -> dict[str, dict[str, str]]:
    """Where propose_change opens PRs: each source's repo, folder and draft branch.

    cfg is the draft config (branch = draftBranch). A git URL names the repo;
    a local folder is read from its git checkout (origin), as page links are.
    """
    out = {}
    for src in cfg.get("sources") or [cfg["source"]]:
        repo = src.get("repo", "")
        link: dict[str, Any] | None
        if re.match(r"^(https://|git@|ssh://)", repo):
            found = source_links.web_base(repo, src.get("provider", ""))
            path = src.get("path", "")
            link = {"provider": found[0], "base": found[1], "path": path} if found else None
        else:
            link = source_links.detect(str(eks_install.source_folder(folder, src)), src)
        if not link:
            raise SystemExit(
                f"access.proposals: source {src['id']} is not a GitHub, GitLab or Azure DevOps repo"
            )
        out[src["id"]] = {k: link[k] for k in ("provider", "base", "path")}
        out[src["id"]]["branch"] = src["branch"]
    return out


# Repo host role per group: (readers, publishers). Same table in
# ui/team-setup/src/config.js and docs/access-model.md.
REPO_ROLES = {
    "github": ("GitHub", "team role Read", "team role Write"),
    "gitlab": ("GitLab", "Reporter", "Developer"),
    "azure-devops": ("Azure DevOps", "project Readers, Basic access", "Contributors"),
}


def access_checklist(cfg: dict[str, Any]) -> list[str]:
    """Groups to make and where each goes (docs/access-model.md). A suggestion only:
    the customer's admins create the groups; Entra (or IAM) and the repo host enforce it."""
    access = cfg.get("access") or {}
    server = cfg["serverName"]
    base = server.removesuffix("-kb")
    readers = access.get("readers") or f"{base}-kb-readers"
    publishers = access.get("publishers") or f"{base}-kb-publishers"
    draft = f"{base}-draft-kb" if access.get("draftServer") else ""
    both = f"{readers!r} and {publishers!r}"
    steps = [f"create groups {both}, same names in Entra and the repo host"]
    if cfg.get("gatewayAuth") == "iam":
        role = f"{cfg['prefix']}-gateway-user"
        steps.append(f"Identity Center: {both} may assume {role}")
        if draft:
            role = f"{cfg['prefix']}-draft-gateway-user"
            steps.append(f"Identity Center: only {publishers!r} may assume {role}")
    else:
        steps.append(f"sign-in app {server}: assign {both}, assignment required")
        if draft:
            steps.append(f"sign-in app {draft}: assign only {publishers!r}")
    sources = cfg.get("sources") or [cfg.get("source") or {}]
    for p in dict.fromkeys(s.get("provider", "local") for s in sources):  # each host once
        if p in REPO_ROLES:
            label, r, w = REPO_ROLES[p]
            sync = "group sync, or mirror by hand"
            steps.append(f"{label} repos: {readers!r} {r}, {publishers!r} {w} ({sync})")
    head = "access checklist (a suggestion, nothing is created; docs/access-model.md):"
    return [head, *(f"  {i}. {s}" for i, s in enumerate(steps, 1))]


def print_access(cfg: dict[str, Any]) -> None:
    print("\n".join(access_checklist(cfg)))


def git(folder: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, check=False)  # noqa: S603, S607


def draft_folder(team: str, folder: pathlib.Path, branch: str) -> pathlib.Path:
    """The team folder as it is on the draft branch: a git worktree in .eks/teams/<team>-draft.

    A local folder: the git repo it sits in must have <branch> or origin/<branch>
    (CI: checkout with fetch-depth 0). A whole worktree, so sources with a
    relative repo folder resolve on the draft branch too, and page links
    (sync/source_links.py) name the draft branch and the real path.
    """
    found = git(folder, "rev-parse", "--show-toplevel")
    if found.returncode:
        raise SystemExit(f"{folder}: not in a git repo, so no draft branch {branch!r}")
    top = pathlib.Path(found.stdout.decode().strip())
    has = lambda ref: git(top, "rev-parse", "--verify", "-q", ref).returncode == 0  # noqa: E731
    if has(f"refs/heads/{branch}"):  # a local branch is used as it is, never reset
        add = ["--force", str(CLONES / f"{team}-draft"), branch]
    elif has(f"origin/{branch}"):
        add = ["-b", branch, str(CLONES / f"{team}-draft"), f"origin/{branch}"]
    else:
        raise SystemExit(f"{folder}: no draft branch {branch!r} (git fetch origin {branch})")
    dest = CLONES / f"{team}-draft"
    if dest.exists():
        git(top, "worktree", "remove", "--force", str(dest))
        shutil.rmtree(dest, ignore_errors=True)  # an older unpacked copy
    git(top, "worktree", "prune")
    dest.parent.mkdir(parents=True, exist_ok=True)
    made = git(top, "worktree", "add", "-q", *add)
    if made.returncode:
        raise SystemExit(made.stderr.decode())
    # Draft content is not trusted: no symlinks, so nothing outside the tree is read.
    links = [
        x
        for x in git(dest, "ls-files", "-s").stdout.decode().splitlines()
        if x.startswith("120000")
    ]
    if links:
        raise SystemExit(f"draft branch {branch!r} has symlinks, refused: {links[:3]}")
    return dest / folder.resolve().relative_to(top.resolve())


def team_file(team: str, cfg: dict[str, Any], rest: list[str]) -> dict[str, Any]:
    """The team repo's deployment.json: the host's keys, the groups, and the sign-in and
    region flags, so a later deploy from the repo alone keeps them."""
    cfg = copy.deepcopy(cfg)
    cfg["_comment"] = f"Team {team}, written by scripts/add-team.sh. Not secret."
    access = cfg.setdefault("access", {})
    access.setdefault("readers", f"{team}-kb-readers")
    access.setdefault("publishers", f"{team}-kb-publishers")
    out = eks_install.build_config(eks_install.parse_args(["install", *rest]), cfg)
    out.pop("ciContentRole", None)  # per deploy, from --ci-trust-role-arn
    return out


def ado_role(prefix: str) -> str:
    """The stack's Azure DevOps sync role (infra/outputs.json), for the service connection."""
    out = json.loads((eks_install.REPO / "infra" / "outputs.json").read_text())
    return out[f"{prefix}-stack"].get("AzureDevOpsSyncRoleArn", "")


def host_steps(host: str, parts: dict[str, str], team: str, cfg: dict[str, Any], role: str) -> str:
    """templates/host-setup/<host>.md, filled with this deploy's values."""
    readers, publishers = (
        (cfg.get("access") or {}).get(k) or f"{team}-kb-{k}" for k in ("readers", "publishers")
    )
    ado = cfg.get("azureDevOps") or {}
    eks_repo, eks_ref = eks_hosts.eks_source() if host == "github" else ("", "")
    return eks_hosts.steps(
        host,
        {
            "team": team,
            "readers": readers,
            "publishers": publishers,
            "region": cfg.get("region", ""),
            "roleArn": role,
            "repo": parts.get("repo", ""),
            "eksRepo": eks_repo,
            "eksRef": eks_ref,
            "orgUrl": ado.get("orgUrl", ""),
            "project": ado.get("project", ""),
            "serviceConnection": ado.get("serviceConnection", ""),
            "adoFiles": f".eks/teams/{team}-ado",
            "folder": "",
        },
    )


def permissions_config(cfg: dict[str, Any], rest: list[str]) -> dict[str, Any]:
    """The team config plus the install flags the permissions check reads."""
    p = argparse.ArgumentParser(add_help=False)
    for flag in ("--region", "--account", "--ci-trust-role-arn", "--ci-role-name"):
        p.add_argument(flag)
    f = p.parse_known_args(rest)[0]
    out = {**cfg, "region": f.region or cfg.get("region") or eks_install._default_region()}
    if f.account:
        out["account"] = f.account
    if f.ci_trust_role_arn:
        out["ciContentRole"] = {"trustRoleArn": f.ci_trust_role_arn, "roleName": f.ci_role_name}
    return out


def request_pack(a: argparse.Namespace, rest: list[str]) -> int:
    """--request-pack DIR: the IT request pack from the same flags as the install."""
    f = argparse.ArgumentParser(add_help=False)
    f.add_argument("--region")
    f.add_argument("--account", default="")
    flags = f.parse_known_args(rest)[0]
    ado = {}
    if a.ado_org or a.ado_project or a.ado_repo:  # names only: IT makes them
        ado = {"org": a.ado_org, "project": a.ado_project, "repo": a.ado_repo}
    inputs = {
        "team": a.team,
        "areas": eks_request_pack.area_list(a.it),
        "owner": a.owner,
        "readerMembers": a.reader_members,
        "publisherMembers": a.publisher_members,
        "account": flags.account,
        "region": flags.region or eks_install._default_region(),
        "installerPrincipalArn": a.installer_principal_arn,
        **ado,
    }
    for path in eks_request_pack.write(pathlib.Path(a.request_pack), inputs):
        print(f"wrote {path}")
    print("Send it to IT. Then paste the values back on the setup page, or run")
    print("  scripts/add-team.sh --check-oidc --issuer ... --client-id ...  (sign-in)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--check-oidc" in argv:
        return eks_oidc_check.main(argv)
    a, rest = parse_args(argv)
    if a.request_pack:
        return request_pack(a, rest)
    host, parts = eks_hosts.host_of(a.content)
    if a.draft:
        host = "local"  # the draft server: no host side (docs/add-a-team.md)
    folder = content_folder(a.team, a.content)
    if not (folder / "knowledge-base").is_dir():
        raise SystemExit(
            f"{folder}: no knowledge-base/ folder. Copy templates/team-content into the repo "
            "first (templates/team-content/README.md)"
        )
    cfg = team_config(a.team, folder)
    if a.prefix:
        cfg["prefix"] = a.prefix
    if host != "local":
        eks_hosts.configure(host, parts, cfg, a.ado_org_id or "")
        cfg = team_file(a.team, cfg, rest)
        (folder / "deployment.json").write_text(json.dumps(cfg, indent=2) + "\n")
    auth = argparse.ArgumentParser(add_help=False)
    auth.add_argument("--auth")
    flag = auth.parse_known_args(rest)[0].auth
    if flag:  # the install applies --auth later; the checklist must already follow it
        cfg["gatewayAuth"] = "iam" if flag == "iam" else "oidc"
    print_access(cfg)
    if a.draft:
        cfg = draft_config(a.team, cfg)
    if not eks_install.PREFIX_RE.match(cfg["prefix"]):
        raise SystemExit(f"prefix {cfg['prefix']!r}: 3-24 chars, lowercase letters, digits, '-'")
    target = eks_install.DEPLOYMENT_FILE
    if target.is_file():
        old = json.loads(target.read_text()).get("prefix")
        if old not in (None, cfg["prefix"]):
            raise SystemExit(
                f"deployment.json is for prefix {old!r}, not {cfg['prefix']!r}. "
                "Use one checkout per team, or move the file away."
            )
    if a.draft:  # documents, mcp/ and smoke.json from the draft branch
        first = (cfg.get("sources") or [cfg["source"]])[0]
        if re.match(r"^(https://|git@|ssh://)", a.content):
            folder = eks_install.clone(a.content, CLONES / f"{a.team}-draft", first["branch"])
        else:
            folder = draft_folder(a.team, folder, first["branch"])
        if (cfg.get("access") or {}).get("proposals"):
            cfg["proposals"] = proposal_targets(cfg, folder)
    else:
        cfg.pop("proposals", None)  # the main server never takes proposals
    # AWS permissions first: nothing in AWS has changed yet.
    if "--skip-permissions" not in rest:
        eks_install.check_permissions(permissions_config(cfg, rest), github_trust=host == "github")
        rest += ["--skip-permissions"]
    role = ""
    if host == "github":  # before the deploy: the content role trusts it
        role = eks_hosts.github_trust(
            parts["repo"], a.github_repo_ids or "", cfg["prefix"], cfg["region"]
        )
        rest += ["--ci-trust-role-arn", role]
    target.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"team {a.team}: prefix {cfg['prefix']}, server {cfg['serverName']}, content {folder}")
    code = eks_install.main(["install", "--content", str(folder), *rest])
    if code or host == "local":
        return code
    if host == "ado":  # after the deploy: the pipeline's AWS role is a stack output
        role = ado_role(cfg["prefix"])
        eks_hosts.ado_files(folder, CLONES / f"{a.team}-ado", cfg)
    print(f"\nwrote {folder / 'deployment.json'}: copy it into {a.content} and commit it\n")
    print(host_steps(host, parts, a.team, cfg, role))
    return 0


if __name__ == "__main__":
    sys.exit(main())
