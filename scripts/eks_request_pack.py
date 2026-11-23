"""IT request packs: the tickets a team admin hands to IT when IT owns sign-in,
AWS or Azure DevOps. Guide: docs/it-request-packs.md.

The text lives in templates/it-request-packs/*.md with {{name}} fields. This
file and ui/team-setup/src/requestPack.js fill the same templates from the same
inputs; both tests compare against tests/fixtures/it-request-pack/.

    scripts/add-team.sh --request-pack <dir> --team legal --it sign-in,aws,ado ...
"""

from __future__ import annotations

import pathlib
import re
import urllib.parse
from typing import Any

REPO = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "templates" / "it-request-packs"
# Area -> (file, one-line title for the header). Order = order in the pack.
AREAS = {
    "sign-in": ("sign-in.md", "Sign-in: one OIDC app and two groups (any OIDC provider)"),
    "aws": ("aws.md", "AWS: the one-time installer stack (path B)"),
    "ado": ("azure-devops.md", "Azure DevOps: project, repo, permissions"),
}
PACK = "it-request-pack.md"
SCOPE = "kb.read"
UPN = re.compile(r"^[\w.+'-]+@[\w-]+(\.[\w-]+)+$", re.ASCII)
ACCOUNT = re.compile(r"^\d{12}$")
ARN = re.compile(r"^arn:aws[\w-]*:(iam|sts)::\d{12}:[\w+=,.@/-]+$", re.ASCII)
SAFE = re.compile(r"^[\w@%+=:,./-]+$", re.ASCII)  # same as sh() in getStarted.js


def sh(v: str) -> str:
    return v if SAFE.match(v) else "'" + v.replace("'", "'\\''") + "'"


def redirect_uris() -> list[str]:
    """The loopback redirect URIs MCP clients use: the default list in
    identity/entra/setup_entra_app.sh (the one place they are written down)."""
    text = (REPO / "identity" / "entra" / "setup_entra_app.sh").read_text()
    return re.search(r'REDIRECT_URIS:-([^}"]+)\}', text).group(1).split()


def cdk_version() -> str:
    text = (REPO / "factory" / "toolchain" / "versions.env").read_text()
    return re.search(r"^AWS_CDK_CLI_VERSION=(\S+)", text, re.M).group(1)


def split_members(text: str) -> list[str]:
    return [m for m in re.split(r"[\s,;]+", text or "") if m]


def validate(p: dict[str, Any]) -> list[str]:
    """Errors for the inputs; empty = fine. Same rules as requestPack.js."""
    errs = []
    if not p["areas"]:
        errs.append("no area for IT: give --it sign-in,aws,ado (one or more)")
    if not UPN.match(p.get("owner", "")):
        errs.append("--owner: the team admin's e-mail / user name, e.g. ana@example.com")
    for key in ("readerMembers", "publisherMembers"):
        bad = [m for m in split_members(p.get(key, "")) if not UPN.match(m)]
        if bad:
            errs.append(f"members: not an e-mail / user name: {', '.join(bad)}")
    if "aws" in p["areas"]:
        if not ACCOUNT.match(p.get("account", "")):
            errs.append("--account: the 12-digit AWS account id")
        if p.get("installerPrincipalArn") and not ARN.match(p["installerPrincipalArn"]):
            errs.append("--installer-principal-arn: an IAM role or user ARN, or empty")
    if "ado" in p["areas"] and not (p.get("org") and p.get("project") and p.get("repo")):
        errs.append("ado: give --ado-org <org> --ado-project <project> --ado-repo <repo>")
    return errs


def values(p: dict[str, Any]) -> dict[str, str]:
    team = p["team"]
    owner = p["owner"]
    readers = split_members(p.get("readerMembers", "")) or [owner]
    publishers = split_members(p.get("publisherMembers", "")) or [owner]
    arn = p.get("installerPrincipalArn", "")
    org, project, repo = p.get("org", ""), p.get("project", ""), p.get("repo", "")
    q = urllib.parse.quote
    return {
        "team": team,
        "owner": owner,
        "appName": f"{team}-kb",
        "readers": f"{team}-kb-readers",
        "publishers": f"{team}-kb-publishers",
        "readerMembers": ", ".join(readers),
        "publisherMembers": ", ".join(publishers),
        "scope": SCOPE,
        "redirectUris": "\n".join(f"  - `{u}`" for u in redirect_uris()),
        "areaList": "\n".join(f"- {AREAS[a][1]} ({AREAS[a][0]})" for a in AREAS if a in p["areas"]),
        "account": p.get("account", ""),
        "region": p.get("region", ""),
        "installerPrincipal": f"`{arn}` (the team admin)"
        if arn
        else "empty (any principal of the account whose own policy allows sts:AssumeRole on it)",
        "installerOverride": f" \\\n  --parameter-overrides InstallerPrincipalArn={arn}"
        if arn
        else "",
        "cdkVersion": cdk_version(),
        "orgUrl": f"https://dev.azure.com/{org}",
        "project": project,
        "projectQ": sh(project),
        "repo": repo,
        "repoQ": sh(repo),
        "repoUrl": f"https://dev.azure.com/{org}/{q(project)}/_git/{q(repo)}",
    }


def render(template: str, v: dict[str, str]) -> str:
    def field(m: re.Match) -> str:
        if m.group(1) not in v:
            raise KeyError(f"template field {{{{{m.group(1)}}}}} has no value")
        return v[m.group(1)]

    return re.sub(r"\{\{(\w+)\}\}", field, template)


def build(p: dict[str, Any]) -> dict[str, str]:
    """File name -> text: one file per area and the whole pack in one file."""
    v = values(p)
    parts = {
        AREAS[a][0]: render((TEMPLATES / AREAS[a][0]).read_text(), v)
        for a in AREAS
        if a in p["areas"]
    }
    head = render((TEMPLATES / "header.md").read_text(), v)
    return {PACK: head + "".join(parts.values()), **parts}


def write(folder: pathlib.Path, p: dict[str, Any]) -> list[pathlib.Path]:
    errs = validate(p)
    if errs:
        raise SystemExit("--request-pack:\n  " + "\n  ".join(errs))
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for name, text in build(p).items():
        (folder / name).write_text(text)
        out.append(folder / name)
    return out


def area_list(text: str) -> list[str]:
    """--it sign-in,aws,ado -> the known areas, in pack order."""
    asked = {a.strip() for a in (text or "").split(",") if a.strip()}
    unknown = asked - set(AREAS)
    if unknown:
        raise SystemExit(f"--it: unknown {sorted(unknown)}; choose from {', '.join(AREAS)}")
    return [a for a in AREAS if a in asked]
