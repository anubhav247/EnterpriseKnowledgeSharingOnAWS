"""Content sources: where the markdown documents come from.

One interface, used by validate_docs.py, sync_chunks.py and verify_kb_state.py:

    list_documents()      sorted relative paths of the .md documents
    fetch(doc)            the text of one document
    changed_since(ref)    documents changed since ref (a commit, or a time for
                          the local folder); for logs and partial runs

Adapters:

    LocalFolderSource     a folder on disk (deploy content load, a CI checkout)
    AzureDevOpsSource     an Azure Repos folder, over the REST API
    GitHubSource          a GitHub repository folder, over the REST API
    GitLabSource          a GitLab project folder, over the REST API (v4)

The sync pipeline still diffs chunk hashes against its manifest, so a full
list + fetch is always correct; changed_since only says what moved.
Tokens come from environment variables and are never logged.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import pathlib
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Protocol

# get(url, headers) -> response body. Swapped for a fake in tests.
HttpGet = Callable[[str, dict[str, str]], bytes]


def http_get(url: str, headers: dict[str, str]) -> bytes:
    if not url.startswith("https://"):
        raise ValueError(f"only https URLs, not {url!r}")
    req = urllib.request.Request(url, headers=headers)  # noqa: S310  https only, checked above
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
        return r.read()


class ContentSource(Protocol):
    name: str

    def list_documents(self) -> list[str]: ...

    def fetch(self, doc: str) -> str: ...

    def changed_since(self, ref: str) -> list[str]: ...


class LocalFolderSource:
    """Markdown files under a folder. changed_since takes an ISO time."""

    def __init__(self, root: pathlib.Path | str) -> None:
        self.root = pathlib.Path(root)
        self.name = f"local:{self.root}"

    def list_documents(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*.md"))

    def fetch(self, doc: str) -> str:
        return (self.root / doc).read_text(encoding="utf-8")

    def changed_since(self, ref: str) -> list[str]:
        since = datetime.datetime.fromisoformat(ref).timestamp()
        return [d for d in self.list_documents() if (self.root / d).stat().st_mtime > since]


def _under(path: str, folder: str) -> str | None:
    """path relative to folder when it is a .md file under it, else None."""
    path, folder = path.strip("/"), folder.strip("/")
    if not path.lower().endswith(".md"):
        return None
    if not folder:
        return path
    return path[len(folder) + 1 :] if path.startswith(folder + "/") else None


class GitHubSource:
    """A folder in a GitHub repository at one ref. Token from GITHUB_TOKEN."""

    API = "https://api.github.com"
    ENV_VAR = "GITHUB_TOKEN"  # name of the variable, not a secret

    def __init__(
        self,
        repo: str,
        folder: str = "",
        ref: str = "main",
        get: HttpGet = http_get,
    ) -> None:
        if repo.count("/") != 1:
            raise ValueError(f"repo must be owner/name, not {repo!r}")
        self.repo, self.folder, self.ref, self._get = repo, folder.strip("/"), ref, get
        self._token = os.environ.get(self.ENV_VAR, "")
        self.name = f"github:{repo}/{self.folder}@{ref}"
        # Page links for search results (source_links.py).
        self.link = {
            "provider": "github",
            "base": f"https://github.com/{repo}",
            "branch": ref,
            "commit": "",
            "path": self.folder,
        }

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _json(self, path: str) -> dict:
        return json.loads(self._get(f"{self.API}{path}", self._headers()))

    def list_documents(self) -> list[str]:
        ref = urllib.parse.quote(self.ref, safe="")
        tree = self._json(f"/repos/{self.repo}/git/trees/{ref}?recursive=1")
        if tree.get("truncated"):
            # shortcut: one tree call; walk subtrees when a docs repo outgrows it.
            raise RuntimeError(f"{self.name}: tree listing truncated by GitHub")
        docs = (_under(e["path"], self.folder) for e in tree["tree"] if e["type"] == "blob")
        return sorted(d for d in docs if d)

    def fetch(self, doc: str) -> str:
        path = urllib.parse.quote(f"{self.folder}/{doc}" if self.folder else doc)
        ref = urllib.parse.quote(self.ref, safe="")
        url = f"{self.API}/repos/{self.repo}/contents/{path}?ref={ref}"
        return self._get(url, self._headers("application/vnd.github.raw+json")).decode("utf-8")

    def changed_since(self, ref: str) -> list[str]:
        base, head = urllib.parse.quote(ref, safe=""), urllib.parse.quote(self.ref, safe="")
        files = self._json(f"/repos/{self.repo}/compare/{base}...{head}").get("files", [])
        docs = (_under(f["filename"], self.folder) for f in files)
        return sorted({d for d in docs if d})


class AzureDevOpsSource:
    """A folder in an Azure Repos Git repository at one branch.

    Token from AZURE_DEVOPS_TOKEN (a PAT) or SYSTEM_ACCESSTOKEN (a pipeline).
    The Azure DevOps pipeline in cicd/azure-devops/ checks the repository out
    and uses LocalFolderSource on that checkout; this adapter reads the same
    content without a checkout.
    """

    API_VERSION = "7.1"

    def __init__(
        self,
        org_url: str,
        project: str,
        repo: str,
        folder: str = "",
        branch: str = "main",
        get: HttpGet = http_get,
    ) -> None:
        self.base = (
            f"{org_url.rstrip('/')}/{urllib.parse.quote(project)}/_apis/git/repositories/"
            f"{urllib.parse.quote(repo)}"
        )
        self.folder, self.branch, self._get = folder.strip("/"), branch, get
        self._token = os.environ.get("AZURE_DEVOPS_TOKEN") or os.environ.get(
            "SYSTEM_ACCESSTOKEN", ""
        )
        self.name = f"azure-devops:{project}/{repo}/{self.folder}@{branch}"
        self.link = {
            "provider": "azure-devops",
            "base": f"{org_url.rstrip('/')}/{urllib.parse.quote(project)}/_git/"
            f"{urllib.parse.quote(repo)}",
            "branch": branch,
            "commit": "",
            "path": self.folder,
        }

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._token:
            basic = base64.b64encode(f":{self._token}".encode()).decode()
            h["Authorization"] = f"Basic {basic}"
        return h

    def _url(self, route: str, **params: str) -> str:
        query = urllib.parse.urlencode({**params, "api-version": self.API_VERSION})
        return f"{self.base}{route}?{query}"

    def list_documents(self) -> list[str]:
        url = self._url(
            "/items",
            scopePath=f"/{self.folder}",
            recursionLevel="Full",
            **{"versionDescriptor.version": self.branch},
        )
        items = json.loads(self._get(url, self._headers())).get("value", [])
        docs = (_under(i["path"], self.folder) for i in items if not i.get("isFolder"))
        return sorted(d for d in docs if d)

    def fetch(self, doc: str) -> str:
        path = f"/{self.folder}/{doc}" if self.folder else f"/{doc}"
        url = self._url(
            "/items",
            path=path,
            includeContent="true",
            **{"versionDescriptor.version": self.branch},
        )
        return json.loads(self._get(url, self._headers()))["content"]

    def changed_since(self, ref: str) -> list[str]:
        url = self._url(
            "/diffs/commits",
            baseVersion=ref,
            baseVersionType="commit",
            targetVersion=self.branch,
            targetVersionType="branch",
        )
        changes = json.loads(self._get(url, self._headers())).get("changes", [])
        docs = (_under(c["item"]["path"], self.folder) for c in changes if "item" in c)
        return sorted({d for d in docs if d})


class GitLabSource:
    """A folder in a GitLab project at one branch, over the REST API (v4).

    Token from GITLAB_TOKEN (a project or personal access token, read_repository
    scope). Works with gitlab.com and self-managed GitLab (base_url).
    """

    ENV_VAR = "GITLAB_TOKEN"  # name of the variable, not a secret
    PER_PAGE = 100

    def __init__(
        self,
        project: str,
        folder: str = "",
        branch: str = "main",
        base_url: str = "https://gitlab.com",
        get: HttpGet = http_get,
    ) -> None:
        if "/" not in project.strip("/"):
            raise ValueError(f"project must be group/name, not {project!r}")
        project_id = urllib.parse.quote(project.strip("/"), safe="")
        self.api = f"{base_url.rstrip('/')}/api/v4/projects/{project_id}"
        self.folder, self.branch, self._get = folder.strip("/"), branch, get
        self._token = os.environ.get(self.ENV_VAR, "")
        self.name = f"gitlab:{project}/{self.folder}@{branch}"
        self.link = {
            "provider": "gitlab",
            "base": f"{base_url.rstrip('/')}/{project.strip('/')}",
            "branch": branch,
            "commit": "",
            "path": self.folder,
        }

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self._token} if self._token else {}

    def _url(self, route: str, **params: str) -> str:
        return f"{self.api}{route}?{urllib.parse.urlencode(params)}"

    def list_documents(self) -> list[str]:
        docs: list[str] = []
        page = 1
        while True:
            url = self._url(
                "/repository/tree",
                path=self.folder,
                ref=self.branch,
                recursive="true",
                per_page=str(self.PER_PAGE),
                page=str(page),
            )
            items = json.loads(self._get(url, self._headers()))
            docs += [
                d for i in items if i["type"] == "blob" and (d := _under(i["path"], self.folder))
            ]
            if len(items) < self.PER_PAGE:
                return sorted(docs)
            page += 1

    def fetch(self, doc: str) -> str:
        path = urllib.parse.quote(f"{self.folder}/{doc}" if self.folder else doc, safe="")
        url = self._url(f"/repository/files/{path}/raw", ref=self.branch)
        return self._get(url, self._headers()).decode("utf-8")

    def changed_since(self, ref: str) -> list[str]:
        url = self._url("/repository/compare", to=self.branch, **{"from": ref})
        diffs = json.loads(self._get(url, self._headers())).get("diffs", [])
        docs = (_under(d["new_path"], self.folder) for d in diffs)
        return sorted({d for d in docs if d})


def as_source(source: ContentSource | pathlib.Path | str) -> ContentSource:
    """A path means a local folder, so old callers keep working."""
    if isinstance(source, (str, pathlib.Path)):
        return LocalFolderSource(source)
    return source


def from_args(
    kind: str, docs_dir: str, repo: str, folder: str, ref: str, org_url: str, project: str
) -> ContentSource:
    """Build a source from the sync_chunks.py / validate_docs.py flags."""
    if kind == "local":
        return LocalFolderSource(docs_dir)
    if kind == "github":
        return GitHubSource(repo, folder, ref or "main")
    if kind == "azure-devops":
        return AzureDevOpsSource(org_url, project, repo, folder, ref or "main")
    if kind == "gitlab":
        return GitLabSource(repo, folder, ref or "main", org_url or "https://gitlab.com")
    raise ValueError(f"unknown source {kind!r}: local, github, azure-devops or gitlab")


def from_config(entry: dict, root: pathlib.Path | str = ".") -> ContentSource:
    """The adapter for one deployment.json "sources" entry (docs/add-a-team.md).

    {"id", "provider", "repo", "branch", "path"}. repo empty or a folder: a
    local folder under root (the team repo checkout). repo a git https URL:
    the provider's REST adapter, so the content is read without a clone.
    """
    repo, path = entry.get("repo", ""), entry.get("path", "")
    branch = entry.get("branch") or "main"
    url = urllib.parse.urlsplit(repo)
    if url.scheme != "https":
        return LocalFolderSource(pathlib.Path(root) / repo / (path or "knowledge-base"))
    parts = url.path.strip("/").removesuffix(".git")
    provider = entry.get("provider", "")
    if provider == "github":
        return GitHubSource(parts, path, branch)
    if provider == "gitlab":
        return GitLabSource(parts, path, branch, f"https://{url.netloc}")
    if provider == "azure-devops" and "/_git/" in parts:
        org_project, name = parts.split("/_git/", 1)
        org, _, project = org_project.rpartition("/")
        return AzureDevOpsSource(
            f"https://{url.netloc}/{org}",
            urllib.parse.unquote(project),
            urllib.parse.unquote(name),
            path,
            branch,
        )
    raise ValueError(f"source {entry.get('id')!r}: no adapter for {provider!r} at {repo!r}")


def add_arguments(parser) -> None:  # argparse.ArgumentParser
    parser.add_argument(
        "--source", default="local", choices=["local", "github", "azure-devops", "gitlab"]
    )
    parser.add_argument(
        "--repo", default="", help="github: owner/name; azure-devops: repo; gitlab: group/project"
    )
    parser.add_argument("--folder", default="", help="folder in the repo (not local)")
    parser.add_argument("--ref", default="", help="branch, tag or commit (default main)")
    parser.add_argument(
        "--org-url",
        default="",
        help="azure-devops: https://dev.azure.com/<org>; gitlab: base URL (default gitlab.com)",
    )
    parser.add_argument("--project", default="", help="azure-devops: project name")


def source_from_parsed(args) -> ContentSource:
    return from_args(
        args.source, args.docs_dir, args.repo, args.folder, args.ref, args.org_url, args.project
    )
