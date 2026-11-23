#!/usr/bin/env python3
"""Check the AWS login has the rights an install needs, before anything changes.

Read-only. It asks IAM (iam:SimulatePrincipalPolicy) whether this login may
do each action the install will do, and prints one line per need: PASS, FAIL
or UNKNOWN, what is missing and who to ask. Exit 0 = all pass, 1 = something
is missing, 2 = could not check (never a guessed pass).

When the login may not call SimulatePrincipalPolicy (some SSO roles), it
says so and runs a few read-only probes instead. A probe can prove a read or
an sts:AssumeRole, never a write, so writes stay UNKNOWN and the exit is 2.

    python infra/permissions.py            # reads deployment.json
Guide: docs/install-permissions.md.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from settings import (
    CDK_QUALIFIER as QUALIFIER,  # noqa: E402  EKS's own, never hnb659fds
    CDK_TOOLKIT_STACK as TOOLKIT,  # noqa: E402
)

GUIDE = "docs/install-permissions.md"
ASK = (
    "Ask your AWS cloud admin for the missing actions, or to set up path B "
    f"(one-time stack factory/infra/customer/eks-installer-role.yaml). See {GUIDE}."
)


@dataclass
class Need:
    name: str
    actions: list[str]
    resources: list[str]
    why: str


def cdk_role(account: str, region: str, kind: str, partition: str = "aws") -> str:
    return f"arn:{partition}:iam::{account}:role/cdk-{QUALIFIER}-{kind}-role-{account}-{region}"


def needs(
    cfg: dict[str, Any],
    account: str,
    region: str,
    *,
    bootstrapped: bool,
    content_roles: list[str],
    github_trust: bool = False,
    partition: str = "aws",
) -> list[Need]:
    """What this install will do, as IAM actions on resources.

    content_roles: the CI content roles the install loads content through
    (path B). Empty = the login loads content itself (path A).
    """
    p = cfg.get("prefix", "kb-mcp")
    arn = f"arn:{partition}"
    acct = f"{account}:"
    out = [
        Need(
            "read checks",
            [
                "cloudformation:DescribeStacks",
                "bedrock:GetFoundationModelAvailability",
                "bedrock:ListKnowledgeBases",
                "s3vectors:ListVectorBuckets",
                "bedrock-agentcore:ListAgentRuntimes",
                "bedrock-agentcore:ListGateways",
                "iam:ListOpenIDConnectProviders",
            ],
            ["*"],
            "infra/preflight.py and the bootstrap check",
        )
    ]
    if not bootstrapped:
        out += [
            Need(
                "CDK bootstrap: CloudFormation",
                [
                    "cloudformation:CreateChangeSet",
                    "cloudformation:DescribeChangeSet",
                    "cloudformation:ExecuteChangeSet",
                    "cloudformation:DescribeStackEvents",
                    "cloudformation:GetTemplate",
                ],
                [f"{arn}:cloudformation:{region}:{acct}stack/{TOOLKIT}/*"],
                f"no {TOOLKIT} stack in this region: the install runs cdk bootstrap",
            ),
            Need(
                "CDK bootstrap: IAM roles",
                ["iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:TagRole"],
                [f"{arn}:iam::{acct}role/cdk-{QUALIFIER}-*"],
                "cdk bootstrap makes the deploy, publishing and execution roles",
            ),
            Need(
                "CDK bootstrap: assets bucket",
                [
                    "s3:CreateBucket",
                    "s3:PutBucketPolicy",
                    "s3:PutEncryptionConfiguration",
                    "s3:PutBucketVersioning",
                    "s3:PutLifecycleConfiguration",
                    "s3:PutBucketPublicAccessBlock",
                    "s3:PutBucketTagging",
                ],
                [f"{arn}:s3:::cdk-{QUALIFIER}-assets-{account}-{region}"],
                "cdk bootstrap makes the assets bucket",
            ),
            Need(
                "CDK bootstrap: image repo",
                [
                    "ecr:CreateRepository",
                    "ecr:SetRepositoryPolicy",
                    "ecr:PutLifecyclePolicy",
                    "ecr:TagResource",
                ],
                [
                    f"{arn}:ecr:{region}:{acct}repository/"
                    f"cdk-{QUALIFIER}-container-assets-{account}-{region}"
                ],
                "cdk bootstrap makes the image repo",
            ),
            Need(
                "CDK bootstrap: version parameter",
                ["ssm:PutParameter", "ssm:AddTagsToResource"],
                [f"{arn}:ssm:{region}:{acct}parameter/cdk-bootstrap/{QUALIFIER}/version"],
                "cdk bootstrap writes its version",
            ),
        ]
    out.append(
        Need(
            "CDK deploy (assume bootstrap roles)",
            ["sts:AssumeRole"],
            [
                cdk_role(account, region, k, partition)
                for k in ("deploy", "file-publishing", "image-publishing", "lookup")
            ],
            "cdk deploy runs through the bootstrap roles",
        )
    )
    if github_trust:  # add-team.sh with a GitHub repo URL deploys cicd/github/deploy-role.yaml
        out += [
            Need(
                "GitHub deploy role: CloudFormation",
                [
                    "cloudformation:CreateChangeSet",
                    "cloudformation:ExecuteChangeSet",
                    "cloudformation:DescribeChangeSet",
                ],
                [f"{arn}:cloudformation:{region}:{acct}stack/{p}-github-deploy/*"],
                "stack <prefix>-github-deploy",
            ),
            Need(
                "GitHub deploy role: IAM",
                ["iam:CreateRole", "iam:PutRolePolicy", "iam:TagRole"],
                [f"{arn}:iam::{acct}role/{p}-{region}-github-deploy"],
                "the role GitHub Actions assumes",
            ),
            Need(
                "GitHub deploy role: OIDC provider",
                ["iam:CreateOpenIDConnectProvider"],
                [f"{arn}:iam::{acct}oidc-provider/token.actions.githubusercontent.com"],
                "only used when the account has no GitHub OIDC provider yet",
            ),
        ]
    if content_roles:
        out.append(
            Need(
                "load content (assume content role)",
                ["sts:AssumeRole"],
                [f"{arn}:iam::{acct}role/{r}" for r in content_roles],
                "content load and smoke test run as the stack's content role",
            )
        )
    else:
        out += [
            Need(
                "load content: docs bucket",
                ["s3:ListBucket"],
                [f"{arn}:s3:::{p}-*"],
                "infra/load_content.py lists the documents",
            ),
            Need(
                "load content: documents",
                ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
                [f"{arn}:s3:::{p}-*/*"],
                "infra/load_content.py writes the documents",
            ),
            Need(
                "load content: ingestion and smoke test",
                [
                    "bedrock:StartIngestionJob",
                    "bedrock:GetIngestionJob",
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "lambda:InvokeFunction",
                ],
                ["*"],
                "ingestion job, smoke test, mcp/ publisher",
            ),
        ]
    return out


def principal_arn(iam, caller_arn: str) -> str:
    """The IAM ARN to simulate. An assumed role's ARN has no path: ask IAM for it."""
    if ":assumed-role/" not in caller_arn:
        return caller_arn  # an IAM user
    name = caller_arn.split(":assumed-role/", 1)[1].split("/", 1)[0]
    return iam.get_role(RoleName=name)["Role"]["Arn"]


def simulate(iam, principal: str, need: Need) -> list[str]:
    """Actions of one need IAM does not allow. Empty = all allowed."""
    denied: set[str] = set()
    kw: dict[str, Any] = {
        "PolicySourceArn": principal,
        "ActionNames": need.actions,
        "ResourceArns": need.resources,
    }
    while True:
        r = iam.simulate_principal_policy(**kw)
        for e in r["EvaluationResults"]:
            per = e.get("ResourceSpecificResults") or [e]
            for x in per:
                key = "EvalResourceDecision" if "EvalResourceDecision" in x else "EvalDecision"
                if x[key] != "allowed":
                    denied.add(e["EvalActionName"])
        if not r.get("IsTruncated"):
            return sorted(denied)
        kw["Marker"] = r["Marker"]


def probes(sess, account: str, region: str) -> list[tuple[str, Callable[[], Any]]]:
    """Read-only calls for when simulation is not allowed. None changes anything."""
    sts = sess.client("sts")

    def assume(kind: str) -> Callable[[], Any]:
        role = cdk_role(account, region, kind)
        return lambda: sts.assume_role(RoleArn=role, RoleSessionName="eks-permissions-probe")

    return [
        (
            "CloudFormation read",
            lambda: sess.client("cloudformation").describe_stacks(StackName=TOOLKIT),
        ),
        ("Bedrock read", lambda: sess.client("bedrock-agent").list_knowledge_bases(maxResults=1)),
        ("IAM read", lambda: sess.client("iam").list_open_id_connect_providers()),
        ("assume CDK deploy role", assume("deploy")),
        ("assume CDK file-publishing role", assume("file-publishing")),
    ]


def is_bootstrapped(sess) -> bool | None:
    """True/False from describe_stacks kbmcp-cdk-toolkit; None when that read is denied."""
    try:
        sess.client("cloudformation").describe_stacks(StackName=TOOLKIT)
        return True
    except ClientError as exc:
        return False if "does not exist" in str(exc) else None


def check(
    sess,
    cfg: dict[str, Any],
    *,
    github_trust: bool = False,
    out: Callable[[str], None] = print,
) -> int:
    """Print the table; 0 pass, 1 missing, 2 could not check."""
    region = cfg.get("region") or sess.region_name
    try:
        ident = sess.client("sts").get_caller_identity()
    except (ClientError, BotoCoreError) as exc:
        out(f"[FAIL] AWS login - {exc}. Sign in first (aws sso login / aws configure).")
        return 1
    account, caller = cfg.get("account") or ident["Account"], ident["Arn"]
    out(f"AWS permissions check for {caller} in {account} {region} (read-only)")
    if caller.endswith(":root"):
        out("[PASS] root user: IAM policies do not limit it (an SCP, if any, is not checked)")
        out("Root is not recommended: use an admin role, or path B. " + GUIDE)
        return 0
    bootstrapped = is_bootstrapped(sess)
    content_roles = content_roles_for(cfg, caller)
    plan = needs(
        cfg,
        account,
        region,
        bootstrapped=bool(bootstrapped),
        content_roles=content_roles,
        github_trust=github_trust,
        partition=caller.split(":")[1],
    )
    iam = sess.client("iam")
    try:
        principal = principal_arn(iam, caller)
        rows = [(n, simulate(iam, principal, n)) for n in plan]
    except (ClientError, BotoCoreError) as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        out(
            f"[UNKNOWN] cannot simulate this login's policies ({code}): it may not call "
            "iam:GetRole / iam:SimulatePrincipalPolicy on itself. Trying read-only probes."
        )
        return fallback(sess, account, region, plan, out)
    width = max(len(n.name) for n, _ in rows)
    for n, denied in rows:
        status = "PASS" if not denied else "FAIL"
        detail = n.why if not denied else "missing " + ", ".join(denied)
        out(f"[{status}] {n.name:<{width}}  {detail}")
    missing = [a for _, d in rows for a in d]
    if not missing:
        out(f"\nAll {len(rows)} permission checks pass. Nothing was changed.")
        return 0
    out(f"\n{len(missing)} action(s) missing. Nothing was changed.\n{ASK}")
    if bootstrapped is False:
        out(
            "This region is not CDK-bootstrapped: bootstrap needs IAM rights most "
            "team admins do not have. Path B has a cloud admin do it once."
        )
    return 1


def fallback(sess, account: str, region: str, plan: list[Need], out) -> int:
    for name, call in probes(sess, account, region):
        try:
            call()
            out(f"[PASS] {name}")
        except (ClientError, BotoCoreError) as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
            out(f"[FAIL] {name} - {code}")
    for n in plan:
        if n.name != "read checks":
            out(f"[UNKNOWN] {n.name} - a probe cannot test a write without making it")
    out(
        "\nCould not verify the write permissions. Nothing was changed.\n"
        "Ask your cloud admin to allow iam:SimulatePrincipalPolicy and iam:GetRole on "
        f"your own role, or use path B ({GUIDE}). If you know you are an admin, "
        "rerun with --skip-permissions."
    )
    return 2


def content_roles_for(cfg: dict[str, Any], caller: str) -> list[str]:
    """Content roles the install will assume: only when this login is their trust role.

    Same rule as scripts/eks_install.py source_roles and runs_as_trust_role.
    """
    role = cfg.get("ciContentRole") or {}
    trust = role.get("trustRoleArn", "").rsplit("/", 1)[-1]
    if not trust or f":assumed-role/{trust}/" not in caller:
        return []
    name = role.get("roleName") or f"{cfg.get('prefix', 'kb-mcp')}-ci-content"
    ids = [s["id"] for s in (cfg.get("sources") or [cfg.get("source") or {"id": ""}])]
    return [name if n == 0 else f"{name}-{i}" for n, i in enumerate(ids)]


def main() -> int:
    import boto3

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from settings import deployment

    cfg = deployment()
    return check(boto3.Session(region_name=cfg.get("region", "us-east-1")), cfg)


if __name__ == "__main__":
    sys.exit(main())
