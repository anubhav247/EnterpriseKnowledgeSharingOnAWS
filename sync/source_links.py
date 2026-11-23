"""Links from search results back to the page a document lives on.

Used at sync time (sync_chunks.py). One link per source repository:

    {"provider": "github" | "gitlab" | "azure-devops",
     "base": web URL of the repository,
     "branch": branch name, "commit": commit id,
     "path": the markdown folder inside the repository,
     "link_to": "branch" (default, the newest page) or "commit"}

url(link, doc, start, end) turns it into the page URL for one document and
line range. detect(folder) reads the link from the git checkout the folder is
in; a folder that is not a git checkout of a known provider gets no link.
"""

from __future__ import annotations

import os
import re
import subprocess
import urllib.parse

PROVIDERS = ("github", "gitlab", "azure-devops")


def url(link: dict, doc: str, start: int | None = None, end: int | None = None) -> str:
    """Page URL for doc (relative to link["path"]), lines start..end when given."""
    provider = link["provider"]
    path = "/".join(p for p in (link.get("path", "").strip("/"), doc.strip("/")) if p)
    by_commit = link.get("link_to") == "commit" and link.get("commit")
    ref = link["commit"] if by_commit else link["branch"]
    base = link["base"].rstrip("/")
    end = end or start
    if provider == "azure-devops":
        query = {"path": f"/{path}", "version": ("GC" if by_commit else "GB") + ref}
        if start:
            query |= {"line": str(start), "lineEnd": str(end)}
        return f"{base}?{urllib.parse.urlencode(query, safe='/')}"
    page = f"{base}/{'-/' if provider == 'gitlab' else ''}blob/{urllib.parse.quote(ref)}/"
    page += urllib.parse.quote(path)
    if not start:
        return page
    if provider == "gitlab":
        return f"{page}#L{start}-{end}" if end != start else f"{page}#L{start}"
    return f"{page}#L{start}-L{end}" if end != start else f"{page}#L{start}"


def web_base(remote: str, provider: str = "") -> tuple[str, str] | None:
    """(provider, repository web URL) for a git remote URL, or None if unknown."""
    remote = remote.strip().removesuffix("/").removesuffix(".git")
    scp = re.match(r"^[\w.-]+@([\w.-]+):(?!//)(.+)$", remote)  # git@host:path
    if scp:
        host, path = scp.groups()
    else:
        parts = urllib.parse.urlsplit(remote)
        if not parts.hostname:
            return None
        host, path = parts.hostname, parts.path.strip("/")
    host = host.lower()
    if host == "ssh.dev.azure.com":  # git@ssh.dev.azure.com:v3/org/project/repo
        org, project, repo = path.removeprefix("v3/").split("/", 2)
        return "azure-devops", f"https://dev.azure.com/{org}/{project}/_git/{repo}"
    if host == "dev.azure.com" or host.endswith(".visualstudio.com"):
        return "azure-devops", f"https://{host}/{path}"
    if "github" in host:
        return "github", f"https://{host}/{path}"
    if "gitlab" in host:
        return "gitlab", f"https://{host}/{path}"
    # Only for a host that does not say (git.acme.io): a known host wins, so a
    # folder standing in for another provider's repo links where it really is.
    if provider in PROVIDERS:
        return provider, f"https://{host}/{path}"
    return None


def _git(folder: str, *args: str) -> str:
    # Fixed argv, no shell; folder is the docs folder this sync reads.
    out = subprocess.run(  # noqa: S603
        ["git", "-C", folder, *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def _ci_branch() -> str:
    """Branch from CI variables, for a checkout at a detached commit."""
    for name in ("GITHUB_HEAD_REF", "GITHUB_REF_NAME", "CI_COMMIT_REF_NAME", "BUILD_SOURCEBRANCH"):
        value = os.environ.get(name, "")
        if value and not value.endswith("/merge"):
            return value.removeprefix("refs/heads/")
    return ""


def detect(folder: str, source: dict | None = None) -> dict | None:
    """The link for a docs folder, read from its git checkout.

    source is the deployment.json source entry: provider "local" turns links
    off; another provider names a self-hosted host the URL does not reveal;
    linkTo "commit" pins links to the synced commit.
    """
    source = source or {}
    if source.get("provider") == "local":
        return None
    top = _git(folder, "rev-parse", "--show-toplevel")
    remote = _git(folder, "remote", "get-url", "origin") if top else ""
    found = web_base(remote, source.get("provider", ""))
    if not found:
        return None
    provider, base = found
    commit = _git(folder, "rev-parse", "HEAD")
    branch = _git(folder, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch if branch and branch != "HEAD" else _ci_branch()
    if not (branch or commit):
        return None
    rel = os.path.relpath(os.path.realpath(folder), os.path.realpath(top))
    return {
        "provider": provider,
        "base": base,
        "branch": branch or commit,
        "commit": commit,
        "path": "" if rel == "." else rel.replace(os.sep, "/"),
        "link_to": "commit" if source.get("linkTo") == "commit" else "branch",
    }


def line_spans(text: str, chunk_texts: list[str]) -> list[tuple[int, int] | None]:
    """1-based (first, last) source line of each chunk, in document order.

    Chunks are rebuilt from the document (headings repeated, blank lines
    trimmed), so each is matched line by line, moving forward through the file.
    A chunk with no matching line gets None.
    """
    lines = [line.strip() for line in text.splitlines()]
    spans: list[tuple[int, int] | None] = []
    cursor = 0
    for chunk in chunk_texts:
        found: list[int] = []
        at = cursor
        for wanted in (line.strip() for line in chunk.splitlines() if line.strip()):
            hit = next((i for i in range(at, len(lines)) if lines[i] == wanted), None)
            if hit is not None:  # a repeated heading is not found ahead: skipped
                found.append(hit)
                at = hit + 1
        spans.append((found[0] + 1, found[-1] + 1) if found else None)
        cursor = found[-1] + 1 if found else cursor
    return spans
