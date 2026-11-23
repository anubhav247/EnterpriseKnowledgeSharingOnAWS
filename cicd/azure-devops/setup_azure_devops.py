#!/usr/bin/env python3
"""Set up the Azure DevOps side of knowledge-base sync. Re-runnable.

Creates, in one Azure DevOps organization:
  - the AWS Toolkit extension (org-wide)
  - project <project> with repos <repo> (default kb-content) and kb-pipelines,
    seeded from the folders beside this script; an existing content repo
    without azure-pipelines.yml gets it in one commit
  - service connection aws-kb-sync: OIDC, no stored keys, assumes the CDK sync role
  - pipeline kb-content-sync, the only pipeline allowed to use the connection
  - checks on the connection: main only, must extend the approved template
  - branch policies: reviewer + validation build on kb-content, reviewer on kb-pipelines

Reads the org, project, repo and source from deployment.json and the role ARN,
bucket and knowledge base ids from infra/outputs.json, so run it after
`cdk deploy`. Repos are seeded from this folder, ../../sync and the sample
content (or --content-dir).

Needs an `az` login that is a Project Collection Administrator of the org.
Every call goes through `az devops invoke`, so it uses the same auth as the CLI.

    python cicd/azure-devops/setup_azure_devops.py
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "infra"))
from settings import deployment, outputs, sources  # noqa: E402

PIPELINE = "kb-content-sync"
TOOLKIT = ("AmazonWebServices", "aws-vsts-tools")

# Azure DevOps built-in type ids.
POLICY_MIN_REVIEWERS = "fa4e907d-c16b-4a4c-9dfa-4906e5d171dd"
POLICY_BUILD = "0609b952-1397-4640-95ec-e00a01b2c241"
CHECK_TASK = "fe1de3ee-a436-41b4-bb20-f6eb4cb879a7"  # branch control runs as a task check
CHECK_EXTENDS = "4020e66e-b0f3-47e1-bc88-48f3cc59b5f3"  # required template
BRANCH_CONTROL_TASK = "86b05a0c-73e6-4f7d-b3cf-e38f3b39a75b"
BASIC_PROCESS = "b8a3a935-7e91-48b8-a94c-606d37c3e9f2"

ORG = ""


def invoke(area, resource, route=None, query=None, method="GET", body=None, version="7.1"):
    cmd = ["az", "devops", "invoke", "--org", ORG, "--area", area, "--resource", resource,
           "--http-method", method, "--api-version", version, "-o", "json", "--only-show-errors"]
    if route:
        cmd += ["--route-parameters", *[f"{k}={v}" for k, v in route.items()]]
    if query:
        cmd += ["--query-parameters", *[f"{k}={v}" for k, v in query.items()]]
    tmp = None
    if body is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(body, tmp)
        tmp.close()
        cmd += ["--in-file", tmp.name]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if tmp:
        pathlib.Path(tmp.name).unlink()
    if r.returncode != 0:
        raise RuntimeError(f"{method} {area}/{resource}: {r.stderr.strip()[:500]}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def log(msg):
    print(f"[ado] {msg}", flush=True)


def stage_repos(work: pathlib.Path, project: str, source_id: str, region: str,
                connection: str, content_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Assemble kb-pipelines and kb-content exactly as they will be pushed."""
    out = outputs()
    pipes = work / "kb-pipelines"
    shutil.copytree(HERE / "kb-pipelines", pipes)
    shutil.copytree(REPO / "sync", pipes / "sync",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "config.json"))
    (pipes / "config").mkdir(exist_ok=True)
    (pipes / "config" / "kb-target.yml").write_text(
        "# Generated from the CDK stack outputs. Platform-owned: content pipelines\n"
        "# cannot override these values.\n"
        "variables:\n"
        f"  awsServiceConnection: {connection}\n"
        f"  awsRegion: {region}\n"
        f"  docsBucket: {out['DocsBucketName']}\n"
        f"  knowledgeBaseId: {out['KnowledgeBaseId']}\n"
        f"  dataSourceId: {out['DataSourceId']}\n"
        f"  mcpPublisherFunction: {out.get('McpPublisherFunctionName', '')}\n")

    content = work / "kb-content"
    content.mkdir()
    for name in ("README.md", "azure-pipelines.yml"):
        text = (HERE / "kb-content" / name).read_text()
        (content / name).write_text(
            text.replace("__PROJECT__", project).replace("__SOURCE_ID__", source_id))
    shutil.copytree(content_dir, content / "knowledge-base")
    mcp_dir = content_dir.parent / "mcp"
    if mcp_dir.is_dir():
        shutil.copytree(mcp_dir, content / "mcp")
    for name in ("deployment.json", "smoke.json"):  # a team repo (templates/team-content)
        if (content_dir.parent / name).is_file():
            shutil.copy(content_dir.parent / name, content / name)
    return pipes, content


def ensure_toolkit():
    try:
        e = invoke("ExtensionManagement", "InstalledExtensionsByName",
                   {"publisherName": TOOLKIT[0], "extensionName": TOOLKIT[1]}, version="7.1-preview")
        log(f"AWS Toolkit installed: {e.get('version')}")
    except RuntimeError:
        e = invoke("ExtensionManagement", "InstalledExtensionsByName",
                   {"publisherName": TOOLKIT[0], "extensionName": TOOLKIT[1]},
                   method="POST", version="7.1-preview")
        log(f"installed AWS Toolkit {e.get('version')}")


def ensure_project(name):
    for p in invoke("core", "projects").get("value", []):
        if p["name"] == name:
            log(f"project exists: {name}")
            return p["id"]
    invoke("core", "projects", method="POST", body={
        "name": name, "visibility": "private",
        "description": "Enterprise knowledge base: domain content and sync pipelines",
        "capabilities": {"versioncontrol": {"sourceControlType": "Git"},
                         "processTemplate": {"templateTypeId": BASIC_PROCESS}}})
    import time
    for _ in range(30):
        time.sleep(4)
        for p in invoke("core", "projects").get("value", []):
            if p["name"] == name and p["state"] == "wellFormed":
                log(f"created project: {name}")
                return p["id"]
    raise RuntimeError("project did not become ready")


def ensure_repo(project_id, name):
    for r in invoke("git", "repositories", {"project": project_id}).get("value", []):
        if r["name"] == name:
            return r["id"], r.get("defaultBranch")
    r = invoke("git", "repositories", {"project": project_id}, method="POST",
               body={"name": name, "project": {"id": project_id}})
    log(f"created repo: {name}")
    return r["id"], None


def seed_repo(project_id, repo_id, folder: pathlib.Path):
    """Initial commit through the pushes API, so no git credentials are needed."""
    changes = []
    for f in sorted(folder.rglob("*")):
        if f.is_dir() or "__pycache__" in f.parts:
            continue
        changes.append({"changeType": "add", "item": {"path": "/" + f.relative_to(folder).as_posix()},
                        "newContent": {"content": base64.b64encode(f.read_bytes()).decode(),
                                       "contentType": "base64encoded"}})
    invoke("git", "pushes", {"project": project_id, "repositoryId": repo_id}, method="POST", body={
        "refUpdates": [{"name": "refs/heads/main", "oldObjectId": "0" * 40}],
        "commits": [{"comment": f"Initial {folder.name}", "changes": changes}]})
    log(f"seeded {folder.name}: {len(changes)} files")


def ensure_pipeline_file(project_id, repo_id, branch, folder: pathlib.Path):
    """An existing content repo without azure-pipelines.yml: add it in one commit."""
    try:
        invoke("git", "items", {"project": project_id, "repositoryId": repo_id},
               query={"path": "/azure-pipelines.yml"})
        return
    except RuntimeError:
        pass
    head = invoke("git", "refs", {"project": project_id, "repositoryId": repo_id},
                  query={"filter": branch.removeprefix("refs/")})["value"][0]["objectId"]
    text = (folder / "azure-pipelines.yml").read_text()
    invoke("git", "pushes", {"project": project_id, "repositoryId": repo_id}, method="POST", body={
        "refUpdates": [{"name": branch, "oldObjectId": head}],
        "commits": [{"comment": "Add the EKS content sync pipeline", "changes": [{
            "changeType": "add", "item": {"path": "/azure-pipelines.yml"},
            "newContent": {"content": text, "contentType": "rawtext"}}]}]})
    log(f"added azure-pipelines.yml to {branch}")


def ensure_connection(project_id, project, role_arn, connection):
    found = invoke("serviceendpoint", "endpoints", {"project": project_id},
                   query={"endpointNames": connection}).get("value", [])
    if found:
        log(f"service connection exists: {connection}")
        return found[0]["id"]
    sc = invoke("serviceendpoint", "endpoints", method="POST", body={
        "name": connection, "type": "AWS", "url": "https://aws.amazon.com/",
        "description": "OIDC, no stored keys. Knowledge base sync role.",
        "authorization": {"scheme": "UsernamePassword", "parameters": {
            "username": "", "password": "", "assumeRoleArn": role_arn,
            "roleSessionName": "kb-sync", "externalId": "", "useOIDC": "true"}},
        "isShared": False,
        "serviceEndpointProjectReferences": [
            {"projectReference": {"id": project_id, "name": project}, "name": connection}]})
    log(f"created service connection: {connection} (OIDC)")
    return sc["id"]


def ensure_pipeline(project_id, repo_id):
    for p in invoke("pipelines", "pipelines", {"project": project_id}).get("value", []):
        if p["name"] == PIPELINE:
            return p["id"]
    p = invoke("pipelines", "pipelines", {"project": project_id}, method="POST", body={
        "name": PIPELINE, "folder": "\\",
        "configuration": {"type": "yaml", "path": "/azure-pipelines.yml",
                          "repository": {"id": repo_id, "type": "azureReposGit"}}})
    log(f"created pipeline: {PIPELINE} ({p['id']})")
    return p["id"]


def authorize(project_id, resource_type, resource_id, pipeline_id):
    invoke("pipelinePermissions", "pipelinePermissions",
           {"project": project_id, "resourceType": resource_type, "resourceId": resource_id},
           method="PATCH", version="7.1-preview",
           body={"allPipelines": {"authorized": False},
                 "pipelines": [{"id": pipeline_id, "authorized": True}]})
    log(f"only pipeline {pipeline_id} may use {resource_type} {resource_id}")


def ensure_policies(project_id, content_repo, pipelines_repo, pipeline_id, creator_can_approve):
    existing = invoke("policy", "configurations", {"project": project_id}).get("value", [])
    have = {(p["type"]["id"], s["repositoryId"]) for p in existing for s in p["settings"].get("scope", [])}

    def scope(repo):
        return [{"repositoryId": repo, "refName": "refs/heads/main", "matchKind": "exact"}]

    wanted = [
        (POLICY_MIN_REVIEWERS, content_repo, {"minimumApproverCount": 1,
            "creatorVoteCounts": creator_can_approve, "resetOnSourcePush": True}),
        (POLICY_BUILD, content_repo, {"buildDefinitionId": pipeline_id,
            "displayName": "Validate documents", "queueOnSourceUpdateOnly": False,
            "manualQueueOnly": False, "validDuration": 0}),
        (POLICY_MIN_REVIEWERS, pipelines_repo, {"minimumApproverCount": 1,
            "creatorVoteCounts": False, "resetOnSourcePush": True}),
    ]
    for type_id, repo, settings in wanted:
        if (type_id, repo) in have:
            continue
        invoke("policy", "configurations", {"project": project_id}, method="POST", body={
            "isEnabled": True, "isBlocking": True, "type": {"id": type_id},
            "settings": {**settings, "scope": scope(repo)}})
    log("branch policies in place")


def ensure_checks(project_id, project, connection_id):
    existing = invoke("PipelinesChecks", "configurations", {"project": project_id},
                      query={"resourceType": "endpoint", "resourceId": connection_id},
                      version="7.1-preview").get("value", [])
    types = {c["type"]["id"] for c in existing}
    resource = {"type": "endpoint", "id": connection_id}
    if CHECK_TASK not in types:
        invoke("PipelinesChecks", "configurations", {"project": project_id}, method="POST",
               version="7.1-preview", body={
                   "type": {"id": CHECK_TASK, "name": "Task Check"}, "timeout": 1440,
                   "resource": resource,
                   "settings": {"displayName": "Branch control: main only", "retryInterval": 5,
                                "definitionRef": {"id": BRANCH_CONTROL_TASK,
                                                  "name": "evaluatebranchProtection",
                                                  "version": "0.0.1"},
                                "inputs": {"allowedBranches": "refs/heads/main",
                                           "ensureProtectionOfBranch": "true",
                                           "allowUnknownStatusBranch": "false"}}})
    if CHECK_EXTENDS not in types:
        invoke("PipelinesChecks", "configurations", {"project": project_id}, method="POST",
               version="7.1-preview", body={
                   "type": {"id": CHECK_EXTENDS, "name": "ExtendsCheck"}, "timeout": 1440,
                   "resource": resource,
                   "settings": {"extendsChecks": [{
                       "repositoryType": "git", "repositoryName": f"{project}/kb-pipelines",
                       "repositoryRef": "refs/heads/main",
                       "templatePath": "templates/kb-sync.yml"}]}})
    log("service connection checks: main only + required template")


def main() -> int:
    global ORG
    cfg = deployment()
    ado = cfg.get("azureDevOps") or {}
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--content-dir", default=str(REPO / "sample-content" / "knowledge-base"),
                    help="markdown to seed kb-content with on first run")
    ap.add_argument("--creator-can-approve", action="store_true",
                    help="let PR authors approve their own content PRs (single-person test orgs only)")
    a = ap.parse_args()

    ORG = ado["orgUrl"].rstrip("/")
    project = ado["project"]
    connection = ado.get("serviceConnection", "aws-kb-sync")
    role_arn = outputs().get("AzureDevOpsSyncRoleArn")
    if not role_arn:
        raise SystemExit("AzureDevOpsSyncRoleArn missing from outputs: set azureDevOps.orgId "
                         "in deployment.json and run cdk deploy first")

    ensure_toolkit()
    project_id = ensure_project(project)
    content_id, content_branch = ensure_repo(project_id, ado.get("repo", "kb-content"))
    pipes_id, pipes_branch = ensure_repo(project_id, "kb-pipelines")
    with tempfile.TemporaryDirectory() as tmp:
        pipes_dir, content_dir = stage_repos(pathlib.Path(tmp), project, sources(cfg)[0]["id"],
                                             cfg.get("region", "us-east-1"), connection,
                                             pathlib.Path(a.content_dir))
        if not pipes_branch:
            seed_repo(project_id, pipes_id, pipes_dir)
        if not content_branch:
            seed_repo(project_id, content_id, content_dir)
        else:
            ensure_pipeline_file(project_id, content_id, content_branch, content_dir)

    connection_id = ensure_connection(project_id, project, role_arn, connection)
    pipeline_id = ensure_pipeline(project_id, content_id)
    authorize(project_id, "endpoint", connection_id, pipeline_id)
    authorize(project_id, "repository", f"{project_id}.{pipes_id}", pipeline_id)
    ensure_policies(project_id, content_id, pipes_id, pipeline_id, a.creator_can_approve)
    ensure_checks(project_id, project, connection_id)

    org_name = ORG.rsplit("/", 1)[-1]
    print(json.dumps({"project_id": project_id, "pipeline_id": pipeline_id,
                      "service_connection_id": connection_id,
                      "oidc_subject": f"sc://{org_name}/{project}/{connection}"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
