#!/usr/bin/env bash
# Azure DevOps side of one EKS team, run by you on your laptop.
#
# EKS never calls Azure DevOps. The team page gives you this script filled in
# (ado-setup-<team>.sh). It signs in with YOUR Azure DevOps PAT only (hidden
# prompt, or env ADO_PAT; never printed or stored). No Azure CLI, no AWS
# Toolkit extension, no AWS login on the laptop.
#
#   bash ado-setup-<team>.sh check          rights test: makes ci-check-* things, deletes them
#   bash ado-setup-<team>.sh                repo, pipeline, variables, first run
#   bash ado-setup-<team>.sh --repo NAME    an existing repo instead of a new one
#   bash ado-setup-<team>.sh --delete       the pipeline and the repo (asks first)
#   add --dry-run to see the planned calls; --pool NAME for your own agent pool
#
# It asks for the pipeline key's secret at a hidden prompt: on the team page,
# "Show pipeline key (once)". The pipeline downloads the EKS sync tools from
# the pinned public release (sha256-checked) and runs the real sync, so a
# green run means your documents are searchable. Docs: docs/ado-setup.md.
set -euo pipefail

# ---- values: the team install fills these in; flags and env override ----
EKS_ORG_URL="${EKS_ORG_URL:-}"
EKS_PROJECT="${EKS_PROJECT:-}"
EKS_REPO="${EKS_REPO:-}"
EKS_TEAM="${EKS_TEAM:-}"
EKS_REGION="${EKS_REGION:-}"
EKS_DOCS_BUCKET="${EKS_DOCS_BUCKET:-}"
EKS_SOURCE_ID="${EKS_SOURCE_ID:-}"
EKS_KB_ID="${EKS_KB_ID:-}"
EKS_DS_ID="${EKS_DS_ID:-}"
EKS_MCP_PUBLISHER="${EKS_MCP_PUBLISHER:-}"
EKS_AWS_ACCESS_KEY_ID="${EKS_AWS_ACCESS_KEY_ID:-}"
EKS_TOOLS_URL="${EKS_TOOLS_URL:-}"
EKS_TOOLS_SHA256="${EKS_TOOLS_SHA256:-}"
# ---- end of values ----

export EKS_ORG_URL EKS_PROJECT EKS_REPO EKS_TEAM EKS_REGION EKS_DOCS_BUCKET EKS_SOURCE_ID \
  EKS_KB_ID EKS_DS_ID EKS_MCP_PUBLISHER EKS_AWS_ACCESS_KEY_ID EKS_TOOLS_URL EKS_TOOLS_SHA256
EKS_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
export EKS_SCRIPT
command -v python3 >/dev/null || { echo "ado-setup: python3 is needed" >&2; exit 2; }
# The program is the Python below (standard library only); the sample after it.
exec python3 -c "$(sed -n '/^# ---- python ----$/,/^# ---- sample ----$/p' "$EKS_SCRIPT")" "$@"
# ---- python ----
"""ado-setup: the Azure DevOps side of one EKS team (see the comment at the top)."""

import argparse
import base64
import getpass
import io
import json
import os
import pathlib
import re
import secrets
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VALUE = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")
PARALLEL_GRANT_URL = "https://aka.ms/azpipelines-parallelism-request"
BASIC_PROCESS = "b8a3a935-7e91-48b8-a94c-606d37c3e9f2"  # the Basic process template
RIGHTS = {
    "git": "Code (Read & write); project right: Create repository / Contribute",
    "pipelines": "Build (Read & execute); project right: Edit build pipeline",
    "build": "Build (Read & execute); project right: Edit build pipeline",
    "distributedtask": "Agent Pools (Read)",
    "projects": "Project and Team (Read)",
    "connectionData": "a valid PAT for this organization",
}
# Pipeline variables (the names ado-quick/ado-new-repo.sh uses too).
PLAIN = {"AWS_REGION": "region", "DOCS_BUCKET": "docs_bucket", "SOURCE_ID": "source_id",
         "KB_ID": "kb_id", "DS_ID": "ds_id", "MCP_PUBLISHER": "mcp_publisher",
         "AWS_ACCESS_KEY_ID": "aws_access_key_id"}


class Fail(RuntimeError):
    pass


def say(msg=""):
    print(msg, flush=True)


# ------------------------------------------------------------------ arguments


def parse_args(argv, env=None):
    env = os.environ if env is None else env
    e = lambda k: env.get(f"EKS_{k}", "")  # noqa: E731
    p = argparse.ArgumentParser(prog="ado-setup.sh",
                                description="The Azure DevOps side of one EKS team, with your own PAT.")
    p.add_argument("cmd", nargs="?", choices=["setup", "check"], default="setup")
    p.add_argument("--check", action="store_const", const="check", dest="cmd_flag", help=argparse.SUPPRESS)
    p.add_argument("--org", "--org-url", dest="org_url", default=e("ORG_URL"))
    p.add_argument("--project", default=e("PROJECT"))
    p.add_argument("--new-project", action="store_true", help="create --project first if missing")
    r = p.add_mutually_exclusive_group()
    r.add_argument("--new-repo", default="", metavar="NAME", help="make this repo from the sample")
    r.add_argument("--repo", default="", metavar="NAME", help="use this existing repo")
    p.add_argument("--commit-to", choices=["pr", "main"], default="pr",
                   help="existing repo: add azure-pipelines.yml by pull request (default) or on main")
    p.add_argument("--team", default=e("TEAM"))
    p.add_argument("--region", default=e("REGION"))
    for name in ("docs-bucket", "source-id", "kb-id", "ds-id", "mcp-publisher", "aws-access-key-id",
                 "tools-url", "tools-sha256"):
        p.add_argument(f"--{name}", default=e(name.upper().replace("-", "_")))
    p.add_argument("--pipeline", default="", help="default: the repo name")
    p.add_argument("--pool", default="", help="agent pool name (self-hosted); default Microsoft-hosted")
    p.add_argument("--wait-min", type=int, default=20, help="wait for a pipeline run (minutes)")
    p.add_argument("--no-run", action="store_true", help="set up, but do not run the pipeline")
    p.add_argument("--demo-edit", default="", metavar="SENTENCE",
                   help="then add SENTENCE to a document on main and wait for the run it starts")
    p.add_argument("--delete", action="store_true", help="delete the pipeline and the repo")
    p.add_argument("--dry-run", action="store_true", help="print the planned calls; nothing changes")
    p.add_argument("--yes", action="store_true", help="no questions (CI)")
    p.add_argument("--require-prefix", default="", help=argparse.SUPPRESS)  # CI: only ci-* names
    a = p.parse_args(argv)
    a.cmd = a.cmd_flag or a.cmd
    a.org_url = a.org_url.rstrip("/")
    if not re.match(r"^https://(dev\.azure\.com/[^/]+|[^/.]+\.visualstudio\.com)$", a.org_url):
        p.error(f"--org: '{a.org_url}', want https://dev.azure.com/<org>")
    if not a.project:
        p.error("missing: --project")
    if a.pool and not NAME.match(a.pool.replace(" ", "-")):
        p.error(f"--pool: '{a.pool}', use letters, digits, spaces, . _ -")
    if a.cmd == "check":
        return a
    a.repo_mode = "existing" if a.repo else "new"
    a.repo = a.repo or a.new_repo or e("REPO")
    if not a.repo:
        p.error("missing: --new-repo NAME or --repo NAME")
    a.pipeline = a.pipeline or a.repo
    for flag in ("repo", "pipeline"):
        if not NAME.match(getattr(a, flag)):
            p.error(f"--{flag}: '{getattr(a, flag)}', use letters, digits, . _ -")
        if a.require_prefix and not getattr(a, flag).startswith(a.require_prefix):
            p.error(f"--{flag}: not {a.require_prefix}* (--require-prefix)")
    if not a.delete:
        need = ["region", *PLAIN.values(), "tools_url", "tools_sha256"]
        gone = [n for n in need if not getattr(a, n)]
        if gone:
            p.error("missing: " + ", ".join("--" + n.replace("_", "-") for n in gone)
                    + " (use the ado-setup-<team>.sh from the team page: it has them filled in)")
        for n in PLAIN.values():
            if not VALUE.match(getattr(a, n)):
                p.error(f"--{n.replace('_', '-')}: '{getattr(a, n)}' is not a valid value")
        if not a.tools_url.startswith("https://") or not re.match(r"^[0-9a-f]{64}$", a.tools_sha256):
            p.error("--tools-url must be https and --tools-sha256 64 hex characters")
    return a


# ------------------------------------------------------------------ Azure DevOps REST


class Ado:
    def __init__(self, org_url, project, pat, require_prefix=""):
        self.org = org_url.rstrip("/")
        self.org_name = self.org.rsplit("/", 1)[-1].split(".")[0] if "dev.azure.com" in self.org \
            else urllib.parse.urlparse(self.org).hostname.split(".")[0]
        self.project = project
        self.auth = "Basic " + base64.b64encode(f":{pat}".encode()).decode()
        self.require_prefix = require_prefix
        self.pid = ""

    def own(self, name):
        """CI guard: only names with the required prefix are created or deleted."""
        if self.require_prefix and not (name.startswith(self.require_prefix)
                                        or re.match(r"^eks-ci\d+$", name)):
            raise Fail(f"refused: '{name}' is not {self.require_prefix}*")
        return name

    def call(self, method, path, body=None, version="7.1", ok404=False, base=None):
        sep = "&" if "?" in path else "?"
        url = f"{base or self.org}/{path}{sep}api-version={version}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": self.auth, "Content-Type": "application/json",
                   "Accept": "application/json"}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if raw and "json" not in r.headers.get("content-type", "json"):
                    raise Fail(f"{method} {path}: the PAT was refused (expired, or another org?)")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if ok404 and e.code == 404:
                return None
            msg = e.read().decode(errors="replace")[:300]
            hint = ""
            if e.code in (401, 403):
                area = next((k for k in RIGHTS if f"/{k}" in "/" + path or f"_apis/{k}" in path), "")
                hint = f" -- missing right: {RIGHTS.get(area, 'see docs/ado-setup.md')}"
            raise Fail(f"{method} {path.split('?')[0]}: HTTP {e.code}{hint}: {msg}") from None

    def p(self, path):
        if not self.pid:
            proj = self.call("GET", f"_apis/projects/{urllib.parse.quote(self.project)}")
            self.pid = proj["id"]
        return f"{self.pid}/_apis/{path}"

    # git
    def repo(self, name):
        return self.call("GET", self.p(f"git/repositories/{urllib.parse.quote(name)}"), ok404=True)

    def make_repo(self, name):
        return self.call("POST", self.p("git/repositories"), {"name": self.own(name)})

    def head(self, repo_id, branch="main"):
        refs = self.call("GET", self.p(f"git/repositories/{repo_id}/refs?filter=heads/{branch}"))
        hit = [r for r in refs.get("value", []) if r["name"] == f"refs/heads/{branch}"]
        return hit[0]["objectId"] if hit else None

    def read(self, repo_id, path, branch="main"):
        q = urllib.parse.quote("/" + path)
        url = (f"git/repositories/{repo_id}/items?path={q}&includeContent=true"
               f"&versionDescriptor.version={branch}&$format=json")
        item = self.call("GET", self.p(url), ok404=True)
        return None if item is None else item.get("content", "")

    def push(self, repo_id, files, old, comment, branch="main", edit=()):
        changes = [{
            "changeType": "edit" if path in edit else "add",
            "item": {"path": "/" + path},
            "newContent": {"content": base64.b64encode(data).decode(),
                           "contentType": "base64encoded"},
        } for path, data in sorted(files.items())]
        self.call("POST", self.p(f"git/repositories/{repo_id}/pushes"), {
            "refUpdates": [{"name": f"refs/heads/{branch}", "oldObjectId": old or "0" * 40}],
            "commits": [{"comment": comment, "changes": changes}],
        })

    def delete_repo(self, repo):
        self.own(repo["name"])
        self.call("DELETE", self.p(f"git/repositories/{repo['id']}"))
        self.call("DELETE", self.p(f"git/recycleBin/repositories/{repo['id']}"), ok404=True)

    # pipelines
    def pipeline(self, name):
        for pl in self.call("GET", self.p("pipelines?$top=1000")).get("value", []):
            if pl["name"] == name:
                return pl
        return None

    def make_pipeline(self, name, repo_id, path="/azure-pipelines.yml"):
        return self.call("POST", self.p("pipelines"), {
            "name": self.own(name), "folder": "\\",
            "configuration": {"type": "yaml", "path": path,
                              "repository": {"id": repo_id, "type": "azureReposGit"}},
        })

    def set_variables(self, pipeline_id, plain, secret):
        """Pipeline variables on the definition; the secret ones are write-only once saved."""
        d = self.call("GET", self.p(f"build/definitions/{pipeline_id}"))
        d.setdefault("variables", {})
        for k, v in plain.items():
            d["variables"][k] = {"value": v, "isSecret": False, "allowOverride": False}
        for k, v in secret.items():
            d["variables"][k] = {"value": v, "isSecret": True, "allowOverride": False}
        self.call("PUT", self.p(f"build/definitions/{pipeline_id}"), d)

    def set_secrets(self, pipeline_id, values):
        """Secret pipeline variables on the definition (write-only once saved)."""
        d = self.call("GET", self.p(f"build/definitions/{pipeline_id}"))
        d.setdefault("variables", {})
        for k, v in values.items():
            d["variables"][k] = {"value": v, "isSecret": True, "allowOverride": False}
        self.call("PUT", self.p(f"build/definitions/{pipeline_id}"), d)

    def authorize(self, kind, rid, pipeline_id):
        self.call("PATCH", self.p(f"pipelines/pipelinePermissions/{kind}/{rid}"),
                  {"pipelines": [{"id": pipeline_id, "authorized": True}]}, version="7.1-preview.1")

    def authorize_queue(self, pipeline_id, pool=""):
        """The project's queue for the pool ('Azure Pipelines' = Microsoft-hosted) for this pipeline."""
        name = urllib.parse.quote(pool or "Azure Pipelines")
        q = self.call("GET", self.p(f"distributedtask/queues?queueNames={name}"), version="7.1-preview.1")
        if not q.get("value"):
            raise Fail(f"agent pool '{pool or 'Azure Pipelines'}' is not in this project")
        for queue in q["value"]:
            self.authorize("queue", queue["id"], pipeline_id)

    def can_create_projects(self):
        """The caller's collection-level 'Create new projects' right (a query, nothing is made)."""
        ns = "3e65f728-f8bc-4ecd-8764-7e378b19bfa7"  # the Collection security namespace
        d = self.call("GET", f"_apis/securitynamespaces/{ns}")
        bit = next(x["bit"] for n in d.get("value", [d]) for x in n.get("actions", [])
                   if x["name"] == "CREATE_PROJECTS")
        r = self.call("GET", f"_apis/permissions/{ns}/{bit}?tokens=NAMESPACE&alwaysAllowAdministrators=true")
        return bool((r.get("value") or [False])[0])

    def make_project(self, name):
        op = self.call("POST", "_apis/projects", {
            "name": name, "visibility": "private",
            "capabilities": {"versioncontrol": {"sourceControlType": "Git"},
                             "processTemplate": {"templateTypeId": BASIC_PROCESS}}})
        for _ in range(40):
            if self.call("GET", f"_apis/operations/{op['id']}").get("status") == "succeeded":
                return
            time.sleep(3)
        raise Fail(f"project {name}: not ready after 2 min")

    def run(self, pipeline_id, wait_min):
        return self.wait(pipeline_id, self.call("POST", self.p(f"pipelines/{pipeline_id}/runs"), {}),
                         wait_min)

    def wait(self, pipeline_id, run, wait_min):
        end = time.monotonic() + wait_min * 60
        while run.get("state") != "completed":
            if time.monotonic() > end:
                return run, [f"not finished after {wait_min} min (no free agent? see --check)"]
            time.sleep(15)
            run = self.call("GET", self.p(f"pipelines/{pipeline_id}/runs/{run['id']}"))
        issues = []
        if run.get("result") != "succeeded":
            tl = self.call("GET", self.p(f"build/builds/{run['id']}/timeline"), ok404=True) or {}
            failed = [r for r in tl.get("records") or [] if r.get("result") == "failed"]
            issues = [f"{r.get('name')}: {i.get('message')}" for r in failed
                      for i in r.get("issues") or []]
            # The failed step's own log tail: the reason is usually there.
            for r in failed:
                if r.get("type") == "Task" and (r.get("log") or {}).get("id"):
                    issues += [f"  | {line}" for line in self.log_tail(run["id"], r["log"]["id"])]
                    break
        return run, issues[-25:]

    def log_tail(self, build_id, log_id, n=15):
        url = f"{self.org}/{self.pid}/_apis/build/builds/{build_id}/logs/{log_id}"
        req = urllib.request.Request(url, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode(errors="replace")
        except (urllib.error.URLError, OSError):
            return []
        return [line.split(" ", 1)[-1] for line in text.splitlines() if line.strip()][-n:]

    def delete_pipeline(self, pl):
        self.own(pl["name"])
        self.call("DELETE", self.p(f"build/definitions/{pl['id']}"))

    # service connections and extensions
    def hosted_parallel_jobs(self):
        """Hosted parallel jobs for private projects; None when the API is not readable."""
        try:
            r = self.call("GET", "_apis/distributedtask/resourceusage?parallelismTag=Private"
                          "&poolIsHosted=true&includeRunningRequests=false",
                          version="7.1-preview.1")
        except Fail:
            return None
        return int((r.get("resourceLimit") or {}).get("totalCount") or 0)



CHECK_PIPELINE = """trigger: none
pr: none
pool:
  vmImage: ubuntu-latest
steps:
  - script: |
      set -eu
      aws --version
      test -n "$CHECK_SECRET" && echo "secret variable: reaches the step"
    displayName: aws cli and secret variable
    env:
      CHECK_SECRET: $(checkSecret)
"""


def check(ado, a):
    """PASS/FAIL per right. Makes only ci-check-* things and always deletes them."""
    results = []
    name = f"ci-check-{secrets.token_hex(3)}"
    ado.require_prefix = "ci-check-"
    made = {}

    def step(what, fn):
        try:
            detail = fn() or ""
            results.append((True, what, detail))
            say(f"PASS {what}{': ' + detail if detail else ''}")
            return True
        except Fail as exc:
            results.append((False, what, str(exc)))
            say(f"FAIL {what}: {exc}")
            return False

    def org():
        d = ado.call("GET", "_apis/connectionData", version="7.1-preview")
        return f"signed in as {(d.get('authenticatedUser') or {}).get('providerDisplayName', '?')}"

    def proj():
        ado.p("")
        return a.project

    def mk_repo():
        made["repo"] = ado.make_repo(name)
        return name

    def push():
        yml = CHECK_PIPELINE.replace("  vmImage: ubuntu-latest", f"  name: {a.pool}") if a.pool \
            else CHECK_PIPELINE
        ado.push(made["repo"]["id"], {"azure-pipelines.yml": yml.encode()}, None, "ado-setup --check")
        return "azure-pipelines.yml on main"

    def mk_pipe():
        made["pipeline"] = ado.make_pipeline(name, made["repo"]["id"])
        ado.authorize_queue(made["pipeline"]["id"], a.pool)
        return name

    def secret():
        pid = made["pipeline"]["id"]
        ado.set_secrets(pid, {"checkSecret": secrets.token_hex(8)})
        v = ado.call("GET", ado.p(f"build/definitions/{pid}"))["variables"]["checkSecret"]
        if not v.get("isSecret") or v.get("value"):
            raise Fail("saved, but not as a secret")
        return "checkSecret saved as secret"

    def parallel():
        n = ado.hosted_parallel_jobs()
        if n is None:
            return "not readable with this PAT; the pipeline run below tells"
        if n == 0:
            raise Fail(f"0 hosted parallel jobs: request the free grant at {PARALLEL_GRANT_URL} "
                       "(takes 2-3 working days), or buy one")
        return f"{n} hosted parallel job(s)"

    def run():
        r, issues = ado.run(made["pipeline"]["id"], a.wait_min)
        if r.get("result") != "succeeded":
            text = "; ".join(issues) or f"state {r.get('state')}, result {r.get('result')}"
            if "parallelism" in text.lower():
                text += f" -- request the free grant: {PARALLEL_GRANT_URL}"
            raise Fail(text)
        where = f"pool {a.pool}" if a.pool else "a Microsoft-hosted agent"
        return f"run {r['id']} succeeded on {where} (aws cli there, secret reached the step)"

    try:
        ok = step("PAT and organization", org) and step("project read", proj)
        ok = ok and step("Code: create repo", mk_repo) and step("Code: push", push)
        ok = ok and step("Build: create pipeline", mk_pipe) and step("Build: secret variable", secret)
        if ok:
            # The run is the real test: a count of 0 with a green run is a stale count.
            ran = step("Build: run pipeline", run)
            if not a.pool:  # org-level, shared by all projects: once
                step("hosted parallel job", (lambda: "a hosted job ran") if ran else parallel)
    finally:
        if made.get("pipeline"):
            step("Build: delete pipeline", lambda: ado.delete_pipeline(made["pipeline"]))
        if made.get("repo"):
            step("Code: delete repo", lambda: ado.delete_repo(made["repo"]))
    try:  # only for --new-project, so it never fails the check
        say("INFO create projects (only for --new-project): "
            + ("yes" if ado.can_create_projects() else
               "no -- use an existing project where you are Project Administrator"))
    except (Fail, StopIteration) as exc:
        say(f"INFO create projects (only for --new-project): not readable ({exc})")
    bad = [w for ok_, w, _ in results if not ok_]
    if not bad:
        say("PASS PAT scopes: Code (Read & write) and Build (Read & execute) are enough")
    say(f"\n{len(results) - len(bad)} PASS, {len(bad)} FAIL" + (": " + ", ".join(bad) if bad else ""))
    if bad:
        say("Ask your Azure DevOps project admin for the rights named above (docs/ado-setup.md).")
    return 1 if bad else 0




def pull_request(ado, repo, todo, have, head):
    branch = "eks-setup"
    for pr in ado.call("GET", ado.p(f"git/repositories/{repo['id']}/pullrequests"
                                    f"?searchCriteria.sourceRefName=refs/heads/{branch}"
                                    "&searchCriteria.status=active")).get("value", []):
        say(f"ok   pull request {pr['pullRequestId']} is open: merge it")
        return True
    old = ado.head(repo["id"], branch)
    if old is None:  # a new branch from main
        ado.call("POST", ado.p(f"git/repositories/{repo['id']}/refs"),
                 [{"name": f"refs/heads/{branch}", "oldObjectId": "0" * 40, "newObjectId": head}])
        old = head
    ado.push(repo["id"], todo, old, "EKS pipeline files (ado-setup.sh)", branch=branch,
             edit={k for k in todo if have[k] is not None})
    pr = ado.call("POST", ado.p(f"git/repositories/{repo['id']}/pullrequests"), {
        "sourceRefName": f"refs/heads/{branch}", "targetRefName": "refs/heads/main",
        "title": "EKS: knowledge base pipeline files",
        "description": "azure-pipelines.yml from ado-setup.sh (the EKS team page).",
    })
    say(f"made pull request {pr['pullRequestId']}: {', '.join(sorted(todo))}")
    return True




# ------------------------------------------------------------------ files


def sample_files():
    """The sample team repo: embedded in the team page's script, or from the EKS bundle."""
    text = pathlib.Path(os.environ.get("EKS_SCRIPT", "")).read_text() if os.environ.get("EKS_SCRIPT") else ""
    if "\n# ---- sample ----\n" in text:
        blob = "".join(line[2:] for line in text.split("\n# ---- sample ----\n", 1)[1].splitlines())
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(blob)), mode="r:gz") as t:
            return {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}
    if not os.environ.get("EKS_SCRIPT"):
        raise Fail("run this file with bash, not python")
    root = pathlib.Path(os.environ["EKS_SCRIPT"]).resolve().parents[1]  # the EKS bundle
    if not (root / "templates" / "team-content").is_dir():
        raise Fail("no sample repo here: use the ado-setup-<team>.sh from the team page")
    folder = root / "templates" / "team-content"
    return {f.relative_to(folder).as_posix(): f.read_bytes() for f in sorted(folder.rglob("*"))
            if f.is_file() and ".github" not in f.parts and f.name != "deployment.json"}


def pipeline_yml(a):
    """azure-pipelines.yml: the docs repo's only non-doc file. The tools come from the pinned release."""
    return render_pipeline(PIPELINE_YML, a.tools_url, a.tools_sha256, a.pool)


def render_pipeline(text, url, sha, pool=""):
    text = text.replace("__TOOLS_URL__", url).replace("__TOOLS_SHA256__", sha)
    if pool:
        text = text.replace("  vmImage: ubuntu-latest", f"  name: {pool}")
    return text


PIPELINE_YML = """# Syncs knowledge-base/ and mcp/ into the EKS knowledge base on every push to
# main. Made by ado-setup.sh. Plain script steps with the agent's python3 and
# aws CLI (no AWS Toolkit extension). The sync tools come from the EKS release
# pinned below, checked by sha256.
#
# Pipeline variables: AWS_REGION, DOCS_BUCKET, SOURCE_ID, KB_ID, DS_ID,
# MCP_PUBLISHER, AWS_ACCESS_KEY_ID, and AWS_SECRET_ACCESS_KEY (secret).
trigger:
  branches:
    include: [main]
  paths:
    include: [knowledge-base/*, mcp/*]

pr: none

pool:
  vmImage: ubuntu-latest

variables:
  EKS_TOOLS_URL: __TOOLS_URL__
  EKS_TOOLS_SHA256: __TOOLS_SHA256__

steps:
  - checkout: self

  - script: |
      set -euo pipefail
      curl --proto =https -fsSL -o eks.tar.gz "$(EKS_TOOLS_URL)"
      echo "$(EKS_TOOLS_SHA256)  eks.tar.gz" | sha256sum -c -
      mkdir -p "$(Agent.TempDirectory)/eks"
      tar -xzf eks.tar.gz -C "$(Agent.TempDirectory)/eks" --strip-components 1
      rm eks.tar.gz
      python3 -m pip install --quiet boto3 markdown-it-py
    displayName: EKS sync tools (pinned release)

  - script: |
      set -euo pipefail
      tools="$(Agent.TempDirectory)/eks/sync"
      python3 "$tools/validate_docs.py" --docs-dir knowledge-base
      python3 "$tools/publish_mcp.py" --validate-only --mcp-dir mcp --docs-dir knowledge-base
    displayName: Validate documents

  - script: |
      set -euo pipefail
      tools="$(Agent.TempDirectory)/eks/sync"
      python3 - "$tools/config.json" <<'EOF'
      import json, os, sys
      json.dump({
          "region": os.environ["AWS_REGION"],
          "docs_bucket": os.environ["DOCS_BUCKET"],
          "docs_prefix": "knowledge-base/",
          "chunk_prefix": "chunks/",
          "source_id": os.environ["SOURCE_ID"],
          "knowledge_base_id": os.environ["KB_ID"],
          "data_source_id": os.environ["DS_ID"],
          "mcp_publisher_function": os.environ["MCP_PUBLISHER"],
      }, open(sys.argv[1], "w"), indent=2)
      EOF
      export AWS_DEFAULT_REGION="$AWS_REGION"
      aws sts get-caller-identity --query Arn --output text
      python3 "$tools/sync_chunks.py" --docs-dir knowledge-base --source-id "$SOURCE_ID"
      python3 "$tools/verify_kb_state.py" --docs-dir knowledge-base
      python3 "$tools/publish_mcp.py" --mcp-dir mcp --docs-dir knowledge-base --source-id "$SOURCE_ID"
    displayName: Sync changed sections and ingest
    condition: and(succeeded(), eq(variables['Build.SourceBranch'], 'refs/heads/main'))
    env:
      AWS_REGION: $(AWS_REGION)
      DOCS_BUCKET: $(DOCS_BUCKET)
      SOURCE_ID: $(SOURCE_ID)
      KB_ID: $(KB_ID)
      DS_ID: $(DS_ID)
      MCP_PUBLISHER: $(MCP_PUBLISHER)
      AWS_ACCESS_KEY_ID: $(AWS_ACCESS_KEY_ID)
      AWS_SECRET_ACCESS_KEY: $(AWS_SECRET_ACCESS_KEY)
"""


def ask_secret(prompt, env_name):
    if os.environ.get(env_name):
        return os.environ[env_name]
    try:
        return getpass.getpass(prompt)  # reads /dev/tty, no echo
    except (EOFError, KeyboardInterrupt):
        raise Fail(f"no {env_name} given") from None


# ------------------------------------------------------------------ plan (dry run)


def plan(a):
    pr = a.repo_mode == "existing" and a.commit_to == "pr"
    steps = ["GET  _apis/connectionData                      PAT and organization",
             f"GET  _apis/projects/{a.project}" + ("  (POST _apis/projects if missing)" if a.new_project else "")]
    if a.repo_mode == "new":
        steps += [f"POST git/repositories {a.repo}                (if missing)",
                  f"POST git/repositories/{a.repo}/pushes          sample documents + azure-pipelines.yml"]
    else:
        steps += [f"GET  git/repositories/{a.repo}/items          knowledge-base/, mcp/instructions.md",
                  f"POST git/repositories/{a.repo}/pushes          azure-pipelines.yml on "
                  + ("branch eks-setup" if pr else "main"),
                  *([f"POST git/repositories/{a.repo}/pullrequests   eks-setup -> main"] if pr else [])]
    steps += [f"POST pipelines {a.pipeline}                    /azure-pipelines.yml of {a.repo}",
              f"PUT  build/definitions/{a.pipeline}           variables; AWS_SECRET_ACCESS_KEY secret",
              f"PATCH pipelinePermissions/queue {a.pool or 'Azure Pipelines'}  this pipeline"]
    if not (a.no_run or pr):
        steps.append(f"POST pipelines/{a.pipeline}/runs               then wait {a.wait_min} min")
    if a.demo_edit:
        steps.append(f"POST git/repositories/{a.repo}/pushes          the sentence, then wait for its run")
    say(f"Dry run: {a.org_url}/{a.project}, team {a.team} ({a.region}), "
        f"pool {a.pool or 'Microsoft-hosted'}. Planned calls:")
    for i, s in enumerate(steps, 1):
        say(f"  {i:2}. {s}")
    return 0


# ------------------------------------------------------------------ set up


def setup(ado, a):
    yml = pipeline_yml(a).encode()
    ado.call("GET", "_apis/connectionData", version="7.1-preview")
    if a.new_project and ado.call("GET", f"_apis/projects/{urllib.parse.quote(a.project)}",
                                  ok404=True) is None:
        try:
            ado.make_project(a.project)
        except Fail as exc:
            raise Fail(f"cannot create project {a.project} ({exc}). Use an existing project where "
                       "you are Project Administrator (--project), without --new-project") from None
        say(f"made project {a.project}")
    ado.p("")
    say(f"ok   {a.org_url}/{a.project}: PAT works")
    secret = ask_secret("AWS secret access key, from the team page's 'Show pipeline key' (hidden): ",
                        "EKS_AWS_SECRET_ACCESS_KEY").strip()
    if not re.match(r"^[A-Za-z0-9/+=]{30,60}$", secret):
        raise Fail("that is not an AWS secret access key")

    repo = ado.repo(a.repo)
    pr_open = False
    if repo is None and a.repo_mode == "existing":
        raise Fail(f"repo {a.repo} not found in {a.project} (use --new-repo {a.repo} to make it)")
    if repo is None:
        repo = ado.make_repo(a.repo)
        say(f"made repo {a.repo}")
    head = ado.head(repo["id"])
    if head is None:
        files = {**sample_files(), "azure-pipelines.yml": yml}
        ado.push(repo["id"], files, None, "EKS team repo (ado-setup.sh)")
        say(f"push {a.repo}: {len(files)} files")
    else:
        if a.repo_mode == "existing":
            gone = [p for p in ("knowledge-base", "mcp/instructions.md") if ado.read(repo["id"], p) is None]
            if gone:
                raise Fail(f"{a.repo} has no {', '.join(gone)}: copy them from templates/team-content")
        have = ado.read(repo["id"], "azure-pipelines.yml")
        todo = {"azure-pipelines.yml": yml}
        if have == yml.decode():
            say(f"ok   {a.repo}: azure-pipelines.yml already on main")
        elif a.repo_mode == "new" or a.commit_to == "main":
            ado.push(repo["id"], todo, head, "EKS pipeline (ado-setup.sh)",
                     edit={"azure-pipelines.yml"} if have is not None else set())
            say(f"push {a.repo} main: azure-pipelines.yml")
        else:
            pr_open = pull_request(ado, repo, todo, {"azure-pipelines.yml": have}, head)

    pl = ado.pipeline(a.pipeline) or ado.make_pipeline(a.pipeline, repo["id"])
    say(f"ok   pipeline {a.pipeline}")
    ado.set_variables(pl["id"], {k: getattr(a, v) for k, v in PLAIN.items()},
                      {"AWS_SECRET_ACCESS_KEY": secret})
    secret = None
    say("ok   variables set; AWS_SECRET_ACCESS_KEY is a secret variable")
    ado.authorize_queue(pl["id"], a.pool)
    say("ok   agent pool allowed for this pipeline")
    if pr_open:
        say("\nNext: merge the pull request above, then run this script again: it runs the pipeline.")
        return 0
    if a.no_run:
        return 0
    say(f"run  {a.pipeline} (up to {a.wait_min} min)...")
    run, issues = ado.run(pl["id"], a.wait_min)
    if not report(a, run, issues, "pipeline run"):
        return 1
    say(f"Your documents are searchable now. Repo: {a.org_url}/{urllib.parse.quote(a.project)}/_git/{a.repo}")
    if a.demo_edit:
        return demo_edit(ado, a, repo, pl)
    return 0


def report(a, run, issues, what):
    url = f"{a.org_url}/{urllib.parse.quote(a.project)}/_build/results?buildId={run['id']}"
    if run.get("result") == "succeeded":
        say(f"PASS {what} {run['id']}: {url}")
        return True
    say(f"FAIL {what} {run['id']} ({run.get('result') or run.get('state')}): {url}")
    for i in issues:
        say(f"  {i}")
    return False


DEMO_DOC = "knowledge-base/example-policy.md"


def demo_edit(ado, a, repo, pl):
    """A document change on main starts the pipeline by itself (its trigger); wait for that run."""
    text = ado.read(repo["id"], DEMO_DOC)
    if text is None:
        raise Fail(f"{a.repo} has no {DEMO_DOC} for --demo-edit")
    before = {r["id"] for r in ado.call("GET", ado.p(f"pipelines/{pl['id']}/runs")).get("value", [])}
    ado.push(repo["id"], {DEMO_DOC: (text.rstrip("\n") + f"\n\n{a.demo_edit}\n").encode()},
             ado.head(repo["id"]), "Demo: edit a document (ado-setup.sh --demo-edit)", edit={DEMO_DOC})
    say(f"push {a.repo} main: one sentence added to {DEMO_DOC}")
    end = time.monotonic() + a.wait_min * 60
    while True:
        new = [r for r in ado.call("GET", ado.p(f"pipelines/{pl['id']}/runs")).get("value", [])
               if r["id"] not in before]
        if new:
            break
        if time.monotonic() > end:
            raise Fail("the document change did not start the pipeline (its trigger on main)")
        time.sleep(10)
    say(f"ok   the change started run {new[0]['id']} by itself")
    run, issues = ado.wait(pl["id"], new[0], a.wait_min)
    if not report(a, run, issues, "triggered run"):
        return 1
    say(f"The new sentence is searchable now: ask your MCP client about \"{a.demo_edit[:60]}\".")
    return 0


def delete(ado, a):
    if not a.yes and input(f"Delete pipeline {a.pipeline} and repo {a.repo}? [y/N] ") != "y":
        raise Fail("not confirmed")
    done = []
    pl = ado.pipeline(a.pipeline)
    if pl:
        ado.delete_pipeline(pl)
        done.append(f"pipeline {a.pipeline}")
    r = ado.repo(a.repo)
    if r:
        ado.delete_repo(r)
        done.append(f"repo {a.repo}")
    say("deleted: " + (", ".join(done) or "nothing left"))
    return 0


def main(argv):
    a = parse_args(argv)
    if a.dry_run:
        return plan(a) if a.cmd == "setup" else (say("Dry run: check makes and deletes ci-check-* "
                                                     "things; nothing to plan") or 0)
    pat = ask_secret(f"Azure DevOps PAT for {a.org_url} (not shown): ", "ADO_PAT").strip()
    if not pat:
        raise Fail("no PAT")
    ado = Ado(a.org_url, a.project, pat, a.require_prefix)
    if a.cmd == "check":
        return check(ado, a)
    if a.delete:
        return delete(ado, a)
    return setup(ado, a)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Fail as exc:
        print(f"FAIL {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - one readable line, never the PAT
        print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
