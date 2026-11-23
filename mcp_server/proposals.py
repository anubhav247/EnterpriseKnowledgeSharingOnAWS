"""propose_change: a document change as a pull request into the draft branch.

Off unless PROPOSALS is set, which the stack does only on a team's draft server
(scripts/eks_add_team.py --draft with access.proposals true). The draft server's
sign-in is assigned to publishers only, so only publishers can call it.

    PROPOSALS                 JSON {source_id: {provider, base, path, branch}}:
                              where each source's documents live and the draft
                              branch the pull request goes into
    PROPOSALS_TOKEN_SECRET    Secrets Manager secret id holding the bot token
                              (GitHub token, GitLab token or Azure DevOps PAT)
    PROPOSALS_MAX_BYTES       largest new document, default 200000
    PROPOSALS_PER_HOUR        proposals per caller per hour, default 10

Flow: new branch off the draft branch, one commit with the new text, a pull
(merge) request back into the draft branch. A human publisher merges it. The
token comes from the secret, never from the caller, and is never logged.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

from kb_client import _FRONT_MATTER_BLOCK, KnowledgeBaseError

# request(method, url, headers, body) -> (status, parsed JSON or None). Faked in tests.
Http = Callable[[str, str, dict[str, str], Any], tuple[int, Any]]

DOC_RE = re.compile(r"^[a-z0-9][a-z0-9-]*/(?:[\w.-]+/)*[\w.-]+\.md$", re.IGNORECASE)
SUMMARY_MAX = 200


def http(method: str, url: str, headers: dict[str, str], body: Any = None) -> tuple[int, Any]:
    if not url.startswith("https://"):
        raise ValueError(f"only https URLs, not {url!r}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310  https only, checked above
        url, data=data, method=method, headers={**headers, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return exc.code, None


def keep_front_matter(old: str, new: str) -> str:
    """retrieve_document returns the body without front matter, so an agent's
    edit comes back without it: put the document's own front matter back."""
    block = _FRONT_MATTER_BLOCK.match(old)
    if not block or _FRONT_MATTER_BLOCK.match(new):
        return new
    return block.group(0).rstrip("\n") + "\n\n" + new.lstrip("\n")


def _b64text(found: Any) -> str:
    return base64.b64decode((found or {}).get("content", "")).decode("utf-8", "replace")


def _check(status: int, step: str, ok: tuple[int, ...] = (200, 201)) -> None:
    if status not in ok:
        # The status only: response bodies can echo request headers.
        raise KnowledgeBaseError(f"propose_change: {step} failed (HTTP {status})")


class GitHub:
    API = "https://api.github.com"

    def __init__(self, base: str, token: str, request: Http) -> None:
        self.repo = urllib.parse.urlsplit(base).path.strip("/")
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
        }
        self.request = request

    def _call(self, method: str, route: str, body: Any = None) -> tuple[int, Any]:
        return self.request(method, f"{self.API}/repos/{self.repo}{route}", self.headers, body)

    def open(self, path: str, text: str, base: str, head: str, title: str, body: str) -> str:
        status, ref = self._call("GET", f"/git/ref/heads/{urllib.parse.quote(base)}")
        _check(status, "read draft branch")
        status, _ = self._call(
            "POST", "/git/refs", {"ref": f"refs/heads/{head}", "sha": ref["object"]["sha"]}
        )
        _check(status, "create branch")
        quoted = urllib.parse.quote(path)
        status, old = self._call("GET", f"/contents/{quoted}?ref={urllib.parse.quote(head)}")
        _check(status, "read document", (200, 404))
        if status == 200:
            text = keep_front_matter(_b64text(old), text)
        commit = {
            "message": title,
            "content": base64.b64encode(text.encode()).decode(),
            "branch": head,
        }
        if status == 200:
            commit["sha"] = old["sha"]
        _check(self._call("PUT", f"/contents/{quoted}", commit)[0], "commit")
        status, pr = self._call(
            "POST", "/pulls", {"title": title, "head": head, "base": base, "body": body}
        )
        if status != 201:
            self._call("DELETE", f"/git/refs/heads/{urllib.parse.quote(head)}")  # no leftovers
        _check(status, "open pull request")
        return pr["html_url"]


class GitLab:
    def __init__(self, base: str, token: str, request: Http) -> None:
        url = urllib.parse.urlsplit(base)
        project = urllib.parse.quote(url.path.strip("/"), safe="")
        self.api = f"{url.scheme}://{url.netloc}/api/v4/projects/{project}"
        self.headers = {"PRIVATE-TOKEN": token}
        self.request = request

    def open(self, path: str, text: str, base: str, head: str, title: str, body: str) -> str:
        quoted = urllib.parse.quote(path, safe="")
        status, old = self.request(
            "GET",
            f"{self.api}/repository/files/{quoted}?ref={urllib.parse.quote(base)}",
            self.headers,
            None,
        )
        _check(status, "read document", (200, 404))
        if status == 200:
            text = keep_front_matter(_b64text(old), text)
        action = {
            "action": "update" if status == 200 else "create",
            "file_path": path,
            "content": text,
        }
        commit = {
            "branch": head,
            "start_branch": base,
            "commit_message": title,
            "actions": [action],
        }
        _check(
            self.request("POST", f"{self.api}/repository/commits", self.headers, commit)[0],
            "commit",
        )
        mr = {
            "source_branch": head,
            "target_branch": base,
            "title": title,
            "description": body,
            "remove_source_branch": True,
        }
        status, made = self.request("POST", f"{self.api}/merge_requests", self.headers, mr)
        if status != 201:
            branch = urllib.parse.quote(head, safe="")
            self.request("DELETE", f"{self.api}/repository/branches/{branch}", self.headers, None)
        _check(status, "open merge request")
        return made["web_url"]


class AzureDevOps:
    API_VERSION = "7.1"

    def __init__(self, base: str, token: str, request: Http) -> None:
        # base: https://dev.azure.com/<org>/<project>/_git/<repo>
        self.web = base.rstrip("/")
        org_project, repo = self.web.split("/_git/", 1)
        self.api = f"{org_project}/_apis/git/repositories/{repo}"
        basic = base64.b64encode(f":{token}".encode()).decode()
        self.headers = {"Accept": "application/json", "Authorization": f"Basic {basic}"}
        self.request = request

    def _url(self, route: str, **params: str) -> str:
        query = urllib.parse.urlencode({**params, "api-version": self.API_VERSION})
        return f"{self.api}{route}?{query}"

    def open(self, path: str, text: str, base: str, head: str, title: str, body: str) -> str:
        status, refs = self.request(
            "GET", self._url("/refs", filter=f"heads/{base}"), self.headers, None
        )
        _check(status, "read draft branch")
        match = [r for r in refs.get("value", []) if r["name"] == f"refs/heads/{base}"]
        if not match:
            raise KnowledgeBaseError(f"propose_change: no draft branch {base!r}")
        status, old = self.request(
            "GET",
            self._url(
                "/items",
                path=f"/{path}",
                includeContent="true",
                **{"versionDescriptor.version": base},
            ),
            self.headers,
            None,
        )
        _check(status, "read document", (200, 404))
        if status == 200:
            text = keep_front_matter((old or {}).get("content", ""), text)
        push = {
            "refUpdates": [{"name": f"refs/heads/{head}", "oldObjectId": match[0]["objectId"]}],
            "commits": [
                {
                    "comment": title,
                    "changes": [
                        {
                            "changeType": "edit" if status == 200 else "add",
                            "item": {"path": f"/{path}"},
                            "newContent": {"content": text, "contentType": "rawtext"},
                        }
                    ],
                }
            ],
        }
        status, pushed = self.request("POST", self._url("/pushes"), self.headers, push)
        _check(status, "commit")
        pr = {
            "sourceRefName": f"refs/heads/{head}",
            "targetRefName": f"refs/heads/{base}",
            "title": title,
            "description": body,
        }
        status, made = self.request("POST", self._url("/pullrequests"), self.headers, pr)
        if status != 201:  # no leftovers: delete the new branch (its id -> zeros)
            new_id = ((pushed or {}).get("refUpdates") or [{}])[0].get("newObjectId", "")
            gone = {"name": f"refs/heads/{head}", "oldObjectId": new_id, "newObjectId": "0" * 40}
            self.request("POST", self._url("/refs"), self.headers, [gone])
        _check(status, "open pull request")
        return f"{self.web}/pullrequest/{made['pullRequestId']}"


PROVIDERS: dict[str, Any] = {"github": GitHub, "gitlab": GitLab, "azure-devops": AzureDevOps}


def targets() -> dict[str, dict[str, str]]:
    raw = os.environ.get("PROPOSALS", "")
    return json.loads(raw) if raw else {}


def enabled() -> bool:
    return bool(targets())


_token: tuple[float, str] | None = None
_lock = threading.Lock()
_calls: dict[str, list[float]] = {}


def token() -> str:
    """The bot token from Secrets Manager, kept 5 minutes so a rotation lands soon."""
    global _token
    if _token and time.monotonic() - _token[0] < 300:
        return _token[1]
    import boto3

    secret = os.environ.get("PROPOSALS_TOKEN_SECRET", "")
    if not secret:
        raise KnowledgeBaseError("propose_change: PROPOSALS_TOKEN_SECRET is not set")
    try:
        value = boto3.client("secretsmanager").get_secret_value(SecretId=secret)["SecretString"]
    except Exception as exc:  # boto errors name the secret, never its value
        raise KnowledgeBaseError(
            f"propose_change: cannot read the token: {type(exc).__name__}"
        ) from None
    _token = (time.monotonic(), value.strip())
    return _token[1]


def _rate_limit(caller: str) -> None:
    # shortcut: per runtime instance, in memory; a busy server needs a shared store.
    limit = int(os.environ.get("PROPOSALS_PER_HOUR", "10"))
    now = time.time()
    with _lock:
        recent = [t for t in _calls.get(caller, []) if now - t < 3600]
        if len(recent) >= limit:
            raise KnowledgeBaseError(
                f"propose_change: limit of {limit} proposals per hour reached; try later"
            )
        _calls[caller] = [*recent, now]


def propose(
    document_id: str,
    new_text: str,
    summary: str,
    caller: str | None,
    exists: Callable[[str], bool],
    request: Http | None = None,
) -> dict[str, Any]:
    """Check the change, then open the pull request. exists(document_id) -> bool."""
    if not isinstance(document_id, str) or not DOC_RE.match(document_id) or ".." in document_id:
        raise KnowledgeBaseError("propose_change: document_id must be <source>/<path>.md")
    source, rel = document_id.split("/", 1)
    target = targets().get(source)
    if target is None:
        raise KnowledgeBaseError(f"propose_change: source {source!r} takes no proposals")
    if not isinstance(new_text, str) or not new_text.strip():
        raise KnowledgeBaseError("propose_change: new_text is empty (deletes are not allowed)")
    size = len(new_text.encode())
    cap = int(os.environ.get("PROPOSALS_MAX_BYTES", "200000"))
    if size > cap:
        raise KnowledgeBaseError(f"propose_change: new_text is {size} bytes, over {cap}")
    summary = " ".join(str(summary or "").split())[:SUMMARY_MAX]
    if not summary:
        raise KnowledgeBaseError("propose_change: summary is empty")
    if not exists(document_id):
        raise KnowledgeBaseError(f"propose_change: unknown document {document_id!r}")
    who = caller or "unknown"
    _rate_limit(who)

    provider = PROVIDERS.get(target.get("provider", ""))
    if provider is None:
        raise KnowledgeBaseError(f"propose_change: no provider {target.get('provider')!r}")
    path = "/".join(p for p in (target.get("path", "").strip("/"), rel) if p)
    head = f"eks-proposal/{source}-{uuid.uuid4().hex[:10]}"
    title = f"Proposal: {summary}"
    body = (
        f"{summary}\n\nDocument: `{path}`\nAsked by: {who}\n\n"
        "Opened by the EKS propose_change tool. A publisher reviews and merges it; "
        "the draft server picks it up after the merge."
    )
    url = provider(target["base"], token(), request or http).open(
        path, new_text, target["branch"], head, title, body
    )
    return {
        "document_id": document_id,
        "url": url,
        "branch": head,
        "target_branch": target["branch"],
        "status": "proposed",
        "next_step": "A publisher reviews the pull request. Share the url with the user.",
    }
