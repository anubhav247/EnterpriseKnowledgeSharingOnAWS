#!/usr/bin/env python3
"""Install or remove the knowledge base stack in one AWS account.

Run it through the wrappers, which find a Python with the deps first:

    scripts/install.sh   --prefix kb --region us-east-1 --auth iam --content sample-content
    scripts/uninstall.sh

install: checks AWS permissions first (read-only, infra/permissions.py), then
tools, writes deployment.json from the flags, bootstraps CDK if needed,
deploys, loads the content folder, runs the smoke test and prints the mcp.json
entry for Kiro. Safe to run again: each step skips or updates.

uninstall: destroys the stack, deletes what CloudFormation leaves behind
(service-made log groups; buckets unless retainData is true), then lists
everything still named after the prefix and exits 1 if anything is left.

Guide: docs/install.md.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "infra"))
from settings import sources  # noqa: E402

DEPLOYMENT_FILE = REPO / "deployment.json"
CDK_DIR = REPO / ".eks" / "cdk"
AUTH_CHOICES = ("iam", "cognito", "entra", "oidc")
PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{1,22}[a-z0-9]$")
SERVER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,46}[a-z0-9]$")
MIN_NODE = 20


# ---------------------------------------------------------------- arguments


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="eks_install.py", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("install", help="deploy, load content, smoke test")
    i.add_argument("--prefix", help="name prefix for every resource (default kb-mcp)")
    i.add_argument("--region", help="AWS region (default: your CLI region, else us-east-1)")
    i.add_argument("--auth", choices=AUTH_CHOICES, help="gateway sign-in (default iam)")
    i.add_argument("--content", help="folder of markdown (or with a knowledge-base/ inside)")
    i.add_argument("--source-id", help="content namespace, lowercase (default analytics)")
    i.add_argument("--server-name", help="MCP server and Kiro entry name (default <prefix>-kb)")
    i.add_argument("--artifact", choices=("image", "code"), help="default: image with Docker")
    i.add_argument("--account", help="target account id (default: your login's account)")
    i.add_argument("--retain-data", action="store_true", default=None, help="keep buckets")
    i.add_argument("--alert-email", help="monitoring: e-mail for the stack's alarms")
    i.add_argument("--monitoring", choices=("on", "off"), help="alarms and dashboard (default on)")
    # --auth entra
    i.add_argument("--entra-tenant-id")
    i.add_argument("--entra-client-id")
    # --auth oidc
    i.add_argument("--oidc-discovery-url")
    i.add_argument("--oidc-audience", action="append", help="repeat for more than one")
    i.add_argument("--oidc-client", action="append", help="allowed client id; repeatable")
    # CI: deploy from another account, load content through a role the stack makes.
    i.add_argument("--ci-trust-role-arn", help="CI role that may assume the content role")
    i.add_argument("--ci-role-name", help="content role name (default <prefix>-ci-content)")
    i.add_argument("--skip-bootstrap", action="store_true")
    i.add_argument("--skip-preflight", action="store_true")
    i.add_argument(
        "--skip-permissions",
        action="store_true",
        help="skip the AWS permissions check (infra/permissions.py)",
    )
    i.add_argument(
        "--permissions-boundary",
        help="path B: managed policy name put on every role the stack makes",
    )
    i.add_argument(
        "--synth-only",
        action="store_true",
        help="stop after cdk synth: nothing is deployed (CI clean-machine test)",
    )
    i.add_argument("--summary", help="write step timings as JSON to this file")

    u = sub.add_parser("uninstall", help="destroy the stack and check nothing is left")
    u.add_argument("--scan-role-arn", help="role to clean up and scan with (CI)")
    u.add_argument("--summary", help="write steps and leftovers as JSON to this file")
    u.add_argument("--passes", type=int, default=3, help="clean-up passes (late log groups)")
    u.add_argument("--wait", type=int, default=30, help="seconds between passes")

    args = p.parse_args(argv)
    if args.command == "install":
        _validate_install(p, args)
    return args


def _validate_install(p: argparse.ArgumentParser, a: argparse.Namespace) -> None:
    if a.prefix is not None and not PREFIX_RE.match(a.prefix):
        p.error("--prefix: 3-24 chars, lowercase letters, digits and '-', start with a letter")
    if a.source_id is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a.source_id):
        p.error("--source-id: lowercase letters, digits and '-'")
    if a.server_name is not None and not SERVER_NAME_RE.match(a.server_name):
        p.error("--server-name: 2-48 chars, lowercase letters, digits and '-'")
    if a.account is not None and not re.fullmatch(r"\d{12}", a.account):
        p.error("--account: 12 digits")
    if a.auth == "entra" and not (a.entra_tenant_id and a.entra_client_id):
        p.error("--auth entra needs --entra-tenant-id and --entra-client-id")
    if a.auth == "oidc" and not (a.oidc_discovery_url and a.oidc_audience):
        p.error("--auth oidc needs --oidc-discovery-url and --oidc-audience")
    if a.oidc_discovery_url and not a.oidc_discovery_url.startswith("https://"):
        p.error("--oidc-discovery-url must be https://")
    if a.content is not None and not pathlib.Path(a.content).is_dir():
        p.error(f"--content: not a folder: {a.content}")


# ------------------------------------------------------------ deployment.json


def build_config(a: argparse.Namespace, existing: dict[str, Any] | None) -> dict[str, Any]:
    """deployment.json from the flags. Keeps other sections of an existing file.

    A flag left out keeps the existing value, so a re-run with no flags
    changes nothing. An existing file for another prefix is refused.
    """
    cfg: dict[str, Any] = json.loads(json.dumps(existing or {}))
    if existing and a.prefix and existing.get("prefix") not in (None, a.prefix):
        raise SystemExit(
            f"deployment.json is for prefix {existing['prefix']!r}, not {a.prefix!r}. "
            "Uninstall that one first, or move the file away."
        )
    if not existing:
        cfg["_comment"] = "Written by scripts/install.sh. See docs/install.md."
    cfg["prefix"] = a.prefix or cfg.get("prefix") or "kb-mcp"
    if a.region:
        cfg["region"] = a.region
    cfg.setdefault("region", _default_region())
    if a.account:
        cfg["account"] = a.account
    # "sources" (several repos) or the single "source"; the flag names the first.
    source = cfg["sources"][0] if cfg.get("sources") else cfg.setdefault("source", {})
    source["id"] = a.source_id or source.get("id") or "analytics"
    if a.server_name:
        cfg["serverName"] = a.server_name
    if a.artifact:
        cfg["runtimeArtifact"] = a.artifact
    cfg.setdefault("runtimeArtifact", "image" if _docker_ok() else "code")
    if a.retain_data is not None:
        cfg["retainData"] = bool(a.retain_data)
    if a.permissions_boundary:
        cfg["permissionsBoundary"] = a.permissions_boundary
    cfg.setdefault("retainData", False)
    monitoring = cfg.setdefault("monitoring", {"alertEmail": "", "logRetentionDays": 30})
    if a.alert_email:
        monitoring["alertEmail"] = a.alert_email
    if a.monitoring:
        monitoring["enabled"] = a.monitoring == "on"
    cfg.setdefault("usage", {"logQueries": True, "noAnswerScore": 0.7})

    if a.auth:
        cfg.pop("identity", None)
        cfg.pop("entra", None)
        if a.auth == "iam":
            cfg["gatewayAuth"] = "iam"
        else:
            cfg["gatewayAuth"] = "oidc"
            cfg["identity"] = _identity(a)
    cfg.setdefault("gatewayAuth", "iam")

    if a.ci_trust_role_arn:
        role = cfg.setdefault("ciContentRole", {})
        role["trustRoleArn"] = a.ci_trust_role_arn
        if a.ci_role_name:
            role["roleName"] = a.ci_role_name
    return cfg


def _identity(a: argparse.Namespace) -> dict[str, Any]:
    if a.auth == "cognito":
        return {"provider": "cognito", "cognito": {"create": True}}
    if a.auth == "entra":
        return {
            "provider": "entra",
            "entra": {"tenantId": a.entra_tenant_id, "clientId": a.entra_client_id},
        }
    section: dict[str, Any] = {
        "discoveryUrl": a.oidc_discovery_url,
        "allowedAudiences": a.oidc_audience,
    }
    if a.oidc_client:
        section["allowedClients"] = a.oidc_client
    return {"provider": "oidc", "oidc": section}


def _default_region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0  # noqa: S607


def content_file(content: str, name: str) -> pathlib.Path | None:
    """A file or folder next to knowledge-base/ in a team content repo, if present."""
    root = pathlib.Path(content).resolve()
    path = root / name
    return path if (root / "knowledge-base").is_dir() and path.exists() else None


def publish_mcp_content(mcp_dir: pathlib.Path, cfg: dict[str, Any], sess) -> None:
    """mcp/instructions.md + mcp/prompts/ to the stack, as the content pipeline does."""
    sys.path.insert(0, str(REPO / "sync"))
    import publish_mcp

    errors = publish_mcp.validate(mcp_dir)
    if errors:
        raise SystemExit("mcp/ failed validation:\n  " + "\n  ".join(errors))
    content = publish_mcp.compile_content(mcp_dir)
    publish_mcp.warn(content["content_checks"])  # never fails the install
    out = json.loads((REPO / "infra" / "outputs.json").read_text())[f"{cfg['prefix']}-stack"]
    publish_mcp.publish(
        sess,
        out["DocsBucketName"],
        sources(cfg)[0]["id"],
        content,
        out.get("McpPublisherFunctionName", ""),
    )


def source_folder(root: pathlib.Path, src: dict[str, Any]) -> pathlib.Path:
    """Local markdown folder of one source.

    repo: empty = the team content repo itself; a folder relative to it; or a
    git URL (any provider), cloned into .eks/sources/<id>. path: the markdown
    folder inside the repo (default knowledge-base/ when there is one).
    """
    repo = src.get("repo", "")
    if re.match(r"^(https://|git@|ssh://)", repo):
        base = clone(repo, REPO / ".eks" / "sources" / src["id"], src.get("branch", ""))
    else:
        base = (root / repo).resolve()
    return base / src["path"] if src.get("path") else docs_dir(str(base))


def clone(url: str, dest: pathlib.Path, branch: str = "") -> pathlib.Path:
    """Shallow clone of a content repo, or a fast-forward pull when it is there."""
    if dest.is_dir():
        sh(["git", "-C", str(dest), "pull", "--ff-only", "-q"])
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        sh(
            [
                "git",
                "clone",
                "-q",
                "--depth",
                "1",
                *(["--branch", branch] if branch else []),
                url,
                str(dest),
            ]
        )
    return dest


def source_roles(cfg: dict[str, Any]) -> dict[str, str]:
    """CI content role per source id: <roleName> for the first, <roleName>-<id> after."""
    role = cfg.get("ciContentRole") or {}
    if not role.get("trustRoleArn"):
        return {}
    name = role.get("roleName") or f"{cfg['prefix']}-ci-content"
    ids = [s["id"] for s in sources(cfg)]
    return {i: name if n == 0 else f"{name}-{i}" for n, i in enumerate(ids)}


def runs_as_trust_role(cfg: dict[str, Any], caller_arn: str) -> bool:
    """True when this login is the CI role the content roles trust; only it may assume them."""
    name = (cfg.get("ciContentRole") or {}).get("trustRoleArn", "").rsplit("/", 1)[-1]
    return bool(name) and f":assumed-role/{name}/" in caller_arn


def docs_dir(content: str) -> pathlib.Path:
    """--content may be the markdown folder or a folder with knowledge-base/ in it."""
    path = pathlib.Path(content).resolve()
    return path / "knowledge-base" if (path / "knowledge-base").is_dir() else path


def read_pins() -> dict[str, str]:
    pins = {}
    for line in (REPO / "factory" / "toolchain" / "versions.env").read_text().splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(\S+)", line)
        if m:
            pins[m.group(1)] = m.group(2)
    return pins


# --------------------------------------------------------------- prereqs


def check_prereqs(artifact: str, which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """Problems that stop an install, as lines to print. Empty = ready."""
    problems = []
    if sys.version_info < (3, 12):
        problems.append(f"Python 3.12+ needed, this is {sys.version.split()[0]}")
    node = which("node")
    if not node:
        problems.append(f"Node.js {MIN_NODE}+ not found (https://nodejs.org)")
    else:
        out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout  # noqa: S603
        major = int(re.sub(r"[^0-9.]", "", out).split(".")[0] or 0)
        if major < MIN_NODE:
            problems.append(f"Node.js {MIN_NODE}+ needed, found {out.strip()}")
    if not which("npm"):
        problems.append("npm not found (comes with Node.js)")
    if artifact == "image" and not which("docker"):
        problems.append("Docker not found; install it or pass --artifact code (no Docker)")
    return problems


# ---------------------------------------------------------------- steps


class Steps:
    """Runs named steps, prints and records their time."""

    def __init__(self, summary: str | None) -> None:
        self.summary = summary
        self.done: list[dict[str, Any]] = []
        self.start = time.monotonic()
        self.extra: dict[str, Any] = {}

    def run(self, name: str, fn: Callable[[], Any]) -> Any:
        print(f"\n==> {name}", flush=True)
        t = time.monotonic()
        ok = False
        try:
            result = fn()
            ok = True
            return result
        finally:
            secs = round(time.monotonic() - t)
            self.done.append({"name": name, "seconds": secs, "ok": ok})
            print(f"<== {name}: {'ok' if ok else 'FAILED'} in {secs}s", flush=True)
            self.write(ok)

    def write(self, ok: bool) -> None:
        if not self.summary:
            return
        data = {
            "ok": ok and all(s["ok"] for s in self.done),
            "seconds": round(time.monotonic() - self.start),
            "steps": self.done,
            **self.extra,
        }
        pathlib.Path(self.summary).write_text(json.dumps(data, indent=2) + "\n")


def sh(cmd: list[str], cwd: pathlib.Path = REPO, env: dict[str, str] | None = None) -> None:
    print("$ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})}, check=True)  # noqa: S603


def cdk_cli() -> str:
    """The pinned CDK CLI, installed once into .eks/cdk (no global npm install)."""
    version = read_pins()["AWS_CDK_CLI_VERSION"]
    cdk = CDK_DIR / "node_modules" / ".bin" / "cdk"
    pkg = CDK_DIR / "node_modules" / "aws-cdk" / "package.json"
    if not (cdk.exists() and json.loads(pkg.read_text()).get("version") == version):
        sh(
            [
                "npm",
                "install",
                "--no-save",
                "--no-audit",
                "--no-fund",
                "--loglevel=error",
                "--prefix",
                str(CDK_DIR),
                f"aws-cdk@{version}",
            ]
        )
    return str(cdk)


def cdk(*args: str) -> None:
    """Every cdk command runs app.py with this installer's Python (.venv on a
    laptop). infra/cdk.json says `python3 app.py`: the system python3 has no aws_cdk."""
    py_dir = os.path.dirname(sys.executable)
    sh(
        [cdk_cli(), *args, "--app", f"{sys.executable} app.py"],
        cwd=REPO / "infra",
        env={
            "JSII_RUNTIME_PACKAGE_CACHE": "disabled",
            "PATH": py_dir + os.pathsep + os.environ.get("PATH", ""),
        },
    )


def session(region: str, role_arn: str = "", name: str = "eks-install"):
    import boto3

    base = boto3.Session(region_name=region)
    if not role_arn:
        return base
    c = base.client("sts").assume_role(RoleArn=role_arn, RoleSessionName=name)["Credentials"]
    return boto3.Session(
        aws_access_key_id=c["AccessKeyId"],
        aws_secret_access_key=c["SecretAccessKey"],
        aws_session_token=c["SessionToken"],
        region_name=region,
    )


def env_for(sess) -> dict[str, str]:
    c = sess.get_credentials().get_frozen_credentials()
    return {
        "AWS_ACCESS_KEY_ID": c.access_key,
        "AWS_SECRET_ACCESS_KEY": c.secret_key,
        "AWS_SESSION_TOKEN": c.token or "",
        "AWS_REGION": sess.region_name,
    }


def check_permissions(cfg: dict[str, Any], github_trust: bool = False) -> None:
    """infra/permissions.py: exits 1 (missing) or 2 (could not check) before any change."""
    import permissions

    code = permissions.check(session(cfg["region"]), cfg, github_trust=github_trust)
    if code:
        raise SystemExit(code)


def bootstrap_if_missing(region: str, account: str) -> str:
    from botocore.exceptions import ClientError

    cfn = session(region).client("cloudformation")
    try:
        cfn.describe_stacks(StackName="CDKToolkit")
        return "already bootstrapped"
    except ClientError as exc:
        if "does not exist" not in str(exc):
            raise
    cdk("bootstrap", f"aws://{account}/{region}")
    return "bootstrapped"


def install(a: argparse.Namespace) -> int:
    steps = Steps(a.summary)
    existing = json.loads(DEPLOYMENT_FILE.read_text()) if DEPLOYMENT_FILE.is_file() else None
    cfg = build_config(a, existing)

    def prereqs() -> None:
        problems = check_prereqs(cfg["runtimeArtifact"])
        if problems:
            raise SystemExit("not ready:\n  " + "\n  ".join(problems))

    def write_config() -> None:
        if existing != cfg:
            DEPLOYMENT_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
        print(
            f"{DEPLOYMENT_FILE}: prefix {cfg['prefix']}, region {cfg['region']}, "
            f"auth {cfg['gatewayAuth']}, artifact {cfg['runtimeArtifact']}"
        )

    region = cfg["region"]
    if not a.skip_permissions:  # first: stops before anything changes
        steps.run("0 AWS permissions (read-only)", lambda: check_permissions(cfg))
    steps.run("1 prerequisites", prereqs)
    steps.run("2 write deployment.json", write_config)
    account = cfg.get("account") or session(region).client("sts").get_caller_identity()["Account"]
    if not a.skip_bootstrap:
        steps.run(
            "3 cdk bootstrap if missing", lambda: print(bootstrap_if_missing(region, account))
        )
    if not a.skip_preflight:  # after bootstrap: preflight checks the CDKToolkit stack
        steps.run("4 preflight (read-only)", lambda: sh([sys.executable, "infra/preflight.py"]))
    if a.synth_only:
        steps.run("5 cdk synth (no deploy)", lambda: cdk("synth", "--quiet"))
        return 0
    steps.run(
        "5 cdk deploy",
        lambda: cdk(
            "deploy",
            "--all",
            "--require-approval",
            "never",
            "--progress",
            "events",
            "--outputs-file",
            "outputs.json",
        ),
    )

    # One load per source, each with its own role (its namespace only). The
    # first source's role also runs the smoke test and publishes mcp/.
    roles = source_roles(cfg)
    caller = session(region).client("sts").get_caller_identity()["Arn"] if roles else ""
    if roles and not runs_as_trust_role(cfg, caller):
        roles = {}  # an admin's own login (add-team.sh, GitHub repo) loads directly
    root = pathlib.Path(a.content or REPO / "sample-content").resolve()
    content_env: dict[str, str] = {}
    content_sess = session(region)
    for n, src in enumerate(sources(cfg)):
        sess, env = session(region), {}
        if src["id"] in roles:
            sess = session(region, f"arn:aws:iam::{account}:role/{roles[src['id']]}")
            env = env_for(sess)
        if n == 0:
            content_sess, content_env = sess, env
        load = [sys.executable, "infra/load_content.py", "--source-id", src["id"]]
        load += ["--docs-dir", str(source_folder(root, src))]
        steps.run(
            f"6.{n + 1} load content {src['id']}", lambda load=load, env=env: sh(load, env=env)
        )
    smoke = [sys.executable, "infra/smoke_test.py"]
    # A team repo always publishes, mcp/ or not: get_sync_status then reports
    # what is missing (content_checks) and the gateway keeps the default text.
    kb = content_file(a.content, "knowledge-base") if a.content else None
    mcp_dir = kb.parent / "mcp" if kb else None
    if mcp_dir:
        steps.run(
            "6b publish mcp/ instructions and prompts",
            lambda: publish_mcp_content(mcp_dir, cfg, content_sess),
        )
    questions = content_file(a.content, "smoke.json") if a.content else None
    if questions:
        smoke += ["--questions", str(questions)]
    steps.run("7 smoke test", lambda: sh(smoke, env=content_env))
    steps.run(
        "8 Kiro mcp.json entry", lambda: sh([sys.executable, "clients/kiro/make_mcp_config.py"])
    )
    print(
        f"\nDone in {round((time.monotonic() - steps.start) / 60, 1)} min. "
        "Add the entry above to ~/.kiro/settings/mcp.json."
    )
    return 0


# ------------------------------------------------------------- leftovers


def name_tokens(prefix: str) -> tuple[str, str]:
    """The prefix as resource names use it: ekb-x and ekb_x (runtime names)."""
    return prefix, prefix.replace("-", "_")


def _pages(client, op: str, key: str, **kw) -> Iterable[dict[str, Any]]:
    if client.can_paginate(op):
        for page in client.get_paginator(op).paginate(**kw):
            yield from page.get(key, [])
    else:
        yield from getattr(client, op)(**kw).get(key, [])


def find_leftovers(sess, prefix: str, extra_roles: Iterable[str] = ()) -> list[tuple[str, str]]:
    """(kind, name) for every resource still named after the prefix.

    The prefix must end at a non-letter, non-digit: ekb-pr-1 does not match
    ekb-pr-12-stack, so one preview's uninstall leaves the others alone. The
    team's draft server (<prefix>-draft, scripts/eks_add_team.py --draft) is
    its own stack and is skipped too.
    """
    dash, under = name_tokens(prefix)
    draft = re.compile(rf"(?:{re.escape(dash)}-draft|{re.escape(under)}_draft)(?![a-z0-9])").search
    own_starts = re.compile(re.escape(dash) + r"(?![a-z0-9])").match
    own_inside = re.compile(rf"(?:{re.escape(dash)}|{re.escape(under)})(?![a-z0-9])").search

    def starts(name: str) -> bool:
        return bool(own_starts(name)) and not draft(name)

    def inside(name: str) -> bool:
        return bool(own_inside(name)) and not draft(name)

    extra = set(extra_roles)
    found: list[tuple[str, str]] = []
    gone = {"DELETE_COMPLETE"}
    for s in _pages(sess.client("cloudformation"), "list_stacks", "StackSummaries"):
        if starts(s["StackName"]) and s["StackStatus"] not in gone:
            found.append(("cloudformation", s["StackName"]))
    for b in sess.client("s3").list_buckets().get("Buckets", []):
        if starts(b["Name"]):
            found.append(("s3", b["Name"]))
    for v in _pages(sess.client("s3vectors"), "list_vector_buckets", "vectorBuckets"):
        if starts(v["vectorBucketName"]):
            found.append(("s3vectors", v["vectorBucketName"]))
    for g in _pages(sess.client("logs"), "describe_log_groups", "logGroups"):
        if inside(g["logGroupName"]):
            found.append(("logs", g["logGroupName"]))
    for pool in _pages(sess.client("cognito-idp"), "list_user_pools", "UserPools", MaxResults=60):
        if starts(pool["Name"]):
            found.append(("cognito", pool["Name"]))
    for r in _pages(sess.client("iam"), "list_roles", "Roles"):
        if starts(r["RoleName"]) or r["RoleName"] in extra:
            found.append(("iam", r["RoleName"]))
    return found


def delete_leftovers(sess, leftovers: list[tuple[str, str]], retain_data: bool) -> list[str]:
    """Delete what CloudFormation leaves behind. Returns what was deleted.

    Log groups always go. Buckets go unless retainData is true. Stacks, roles
    and user pools are CloudFormation's: listed, never deleted here.
    """
    deleted = []
    for kind, name in leftovers:
        if kind == "logs":
            sess.client("logs").delete_log_group(logGroupName=name)
        elif kind == "s3" and not retain_data:
            _empty_and_delete_bucket(sess, name)
        elif kind == "s3vectors" and not retain_data:
            v = sess.client("s3vectors")
            for ix in _pages(v, "list_indexes", "indexes", vectorBucketName=name):
                v.delete_index(vectorBucketName=name, indexName=ix["indexName"])
            v.delete_vector_bucket(vectorBucketName=name)
        else:
            continue
        deleted.append(f"{kind} {name}")
    return deleted


def _empty_and_delete_bucket(sess, name: str) -> None:
    s3 = sess.client("s3")
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=name):
        objs = [
            {"Key": o["Key"], "VersionId": o["VersionId"]}
            for o in page.get("Versions", []) + page.get("DeleteMarkers", [])
        ]
        if objs:
            s3.delete_objects(Bucket=name, Delete={"Objects": objs, "Quiet": True})
    s3.delete_bucket(Bucket=name)


def delete_github_trust(sess, prefix: str) -> str:
    """The <prefix>-github-deploy stack add-team.sh made for a GitHub repo, if any."""
    from botocore.exceptions import ClientError

    cfn, name = sess.client("cloudformation"), f"{prefix}-github-deploy"
    try:
        cfn.describe_stacks(StackName=name)
    except ClientError as exc:
        if "does not exist" not in str(exc):
            raise
        return "none"
    cfn.delete_stack(StackName=name)
    cfn.get_waiter("stack_delete_complete").wait(StackName=name)
    return f"deleted {name}"


def blocking(leftovers: list[tuple[str, str]], retain_data: bool) -> list[tuple[str, str]]:
    """Leftovers that fail the uninstall. Retained data buckets are kept on purpose."""
    kept = {"s3", "s3vectors"} if retain_data else set()
    return [x for x in leftovers if x[0] not in kept]


def unstick_data_source(region: str, stack: str, sess=None) -> str:
    """A stack left DELETE_FAILED on ChunkDataSource (Bedrock could not empty the
    vector store, stacks made before RETAIN): set that data source to RETAIN so
    the next delete goes through. The vector index and bucket go with the stack."""
    from botocore.exceptions import ClientError

    sess = sess or session(region)
    cfn = sess.client("cloudformation")
    try:
        status = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
    except ClientError as exc:
        if "does not exist" in str(exc):
            return "no stack"
        return f"not checked ({exc.response['Error']['Code']})"  # this login may not read it
    if status != "DELETE_FAILED":
        return status
    found = cfn.describe_stack_resource(StackName=stack, LogicalResourceId="ChunkDataSource")
    detail = found["StackResourceDetail"]
    if detail["ResourceStatus"] != "DELETE_FAILED":
        return f"{status}, data source fine"
    kb_id, ds_id = detail["PhysicalResourceId"].split("|")
    agent = sess.client("bedrock-agent")
    ds = agent.get_data_source(knowledgeBaseId=kb_id, dataSourceId=ds_id)["dataSource"]
    agent.update_data_source(
        knowledgeBaseId=kb_id,
        dataSourceId=ds_id,
        name=ds["name"],
        dataSourceConfiguration=ds["dataSourceConfiguration"],
        vectorIngestionConfiguration=ds.get("vectorIngestionConfiguration", {}),
        dataDeletionPolicy="RETAIN",
    )
    return f"{status}: data source {ds_id} set to RETAIN, delete again"


def uninstall(a: argparse.Namespace) -> int:
    if not DEPLOYMENT_FILE.is_file():
        raise SystemExit(f"{DEPLOYMENT_FILE} not found: nothing to uninstall from here.")
    cfg = json.loads(DEPLOYMENT_FILE.read_text())
    prefix, region = cfg["prefix"], cfg.get("region") or _default_region()
    retain = bool(cfg.get("retainData", False))
    extra = (
        list(source_roles(cfg).values()) if (cfg.get("ciContentRole") or {}).get("roleName") else []
    )
    steps = Steps(a.summary)

    steps.run(
        "0 a failed earlier delete", lambda: print(unstick_data_source(region, f"{prefix}-stack"))
    )
    steps.run("1 cdk destroy", lambda: cdk("destroy", "--all", "--force"))
    sess = session(region, a.scan_role_arn or "", "eks-uninstall")
    steps.run("1b GitHub deploy role", lambda: print(delete_github_trust(sess, prefix)))
    # Several passes: the stack's own clean-up functions write their log
    # groups while the stack is being deleted, sometimes a little after.
    for n in range(1, a.passes + 1):
        left = find_leftovers(sess, prefix, extra)
        todo = [x for x in left if x[0] == "logs" or (x[0] in ("s3", "s3vectors") and not retain)]
        if not todo:
            break
        steps.run(
            f"2.{n} delete leftovers",
            lambda: print("\n".join(delete_leftovers(sess, todo, retain))),
        )
        time.sleep(a.wait)
    left = steps.run("3 find leftovers", lambda: find_leftovers(sess, prefix, extra))
    bad = blocking(left, retain)
    steps.extra = {"leftovers": [f"{k} {n}" for k, n in left], "blocking": len(bad)}
    steps.write(not bad)
    for kind, name in left:
        print(f"LEFT {kind:15} {name}{'' if (kind, name) in bad else '  (retainData)'}")
    print(
        f"\n{len(bad)} leftover(s) named {prefix}*." if bad else f"\nNothing left named {prefix}*."
    )
    (REPO / "infra" / "outputs.json").unlink(missing_ok=True)
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    a = parse_args(sys.argv[1:] if argv is None else argv)
    return install(a) if a.command == "install" else uninstall(a)


if __name__ == "__main__":
    sys.exit(main())
