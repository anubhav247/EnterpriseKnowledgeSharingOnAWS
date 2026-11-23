#!/usr/bin/env python3
"""CDK entry point. All parameters come from deployment.json at the repo root.

Account comes from the active AWS CLI login. Optional parts switch on when
their section of deployment.json is filled in:

    gatewayAuth: "oidc" + identity      adds the MCP gateway with OIDC sign-in
                                        (any provider; presets entra, cognito;
                                        see identity/config.py, docs/identity.md)
    gatewayAuth: "iam"                  adds the MCP gateway with AWS IAM (SigV4)
    entra.tenantId + entra.clientId     deprecated; read as identity.provider entra
    azureDevOps.orgId                   adds the OIDC sync role for pipelines
    runtimeArtifact: "code"             ships the MCP server as a zip (no Docker)
                                        instead of a container image
    ciContentRole.trustRoleArn          adds <prefix>-ci-content, a role that one
                                        CI role may assume to load content and
                                        smoke test (deploy workflow);
    ciContentRole.roleName              its name; default <prefix>-ci-content
    account                             target account; default: the CLI login's
    gatewayUserTrustAccount             adds <prefix>-gateway-user, a role that
                                        principals of that account may assume
                                        to call the gateway only (Kiro access)
    usage.logQueries / noAnswerScore    usage event settings on the runtime
                                        (default true / 0.7)
    sources: [{id, provider, repo, ...}] more than one content repo behind
                                        one server; each extra source gets its
                                        own CI role <ciContentRole>-<id>
    serverName                          MCP server name clients see and the Kiro
                                        mcp.json entry name (default <prefix>-kb)
    proposals: {source_id: target}      turns on propose_change (draft server
                                        only; add-team.sh --draft writes it
                                        from access.proposals)
    permissionsBoundary                 managed policy name put on every role the
                                        stack makes (path B installs,
                                        docs/install-permissions.md)
"""

from __future__ import annotations

import os
import pathlib
import sys

import aws_cdk as cdk
from aws_cdk import aws_iam as iam

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # identity/

from identity.config import resolve  # noqa: E402
from settings import deployment, sources, stack_name  # noqa: E402
from stacks.kb_mcp_stack import AzureDevOpsSync, KbMcpStack, McpServerSpec  # noqa: E402
from stacks.monitoring import MonitoringSettings  # noqa: E402

cfg = deployment()
prefix = cfg.get("prefix", "kb-mcp")
# One or more content sources, one namespace each; the first is the primary.
source_ids = [s["id"] for s in sources(cfg)]
source_id = source_ids[0]

region = cfg.get("region") or os.environ.get("CDK_DEFAULT_REGION", "us-east-1")
try:
    auth = resolve(cfg, region)
except ValueError as exc:
    raise SystemExit(f"deployment.json: {exc}") from exc
runtime_artifact = cfg.get("runtimeArtifact", "image")
if runtime_artifact not in ("image", "code"):
    raise SystemExit(
        f'deployment.json runtimeArtifact must be "image" or "code", not {runtime_artifact!r}'
    )

ado_cfg = cfg.get("azureDevOps") or {}
ado = (
    AzureDevOpsSync(
        org_id=ado_cfg["orgId"],
        org_name=ado_cfg["orgUrl"].rstrip("/").rsplit("/", 1)[-1],
        project=ado_cfg["project"],
        service_connection=ado_cfg.get("serviceConnection", "aws-kb-sync"),
        source_id=source_id,
        access_key_user=bool(ado_cfg.get("accessKeyUser", True)),
    )
    if ado_cfg.get("orgId")
    else None
)

mon_cfg = cfg.get("monitoring") or {}
monitoring = MonitoringSettings(
    enabled=bool(mon_cfg.get("enabled", True)),
    alert_email=mon_cfg.get("alertEmail", ""),
    log_retention_days=int(mon_cfg.get("logRetentionDays", 90)),
    auth_failure_threshold=int(mon_cfg.get("authFailureThreshold", 50)),
    stale_after_days=int(mon_cfg.get("staleAfterDays", 0)),
)

app = cdk.App()
stack = KbMcpStack(
    app,
    stack_name(),
    prefix=prefix,
    # Base version: one content source, one MCP server.
    servers=[McpServerSpec(name="kb", scope="")],
    gateway_auth=auth.mode,
    oidc=auth.oidc,
    cognito_create=auth.cognito_create,
    runtime_artifact=runtime_artifact,
    ci_trust_role_arn=(cfg.get("ciContentRole") or {}).get("trustRoleArn", ""),
    ci_content_role_name=(cfg.get("ciContentRole") or {}).get("roleName", ""),
    gateway_user_trust_account=str(cfg.get("gatewayUserTrustAccount", "")),
    usage_log_queries=bool((cfg.get("usage") or {}).get("logQueries", True)),
    usage_no_answer_score=float((cfg.get("usage") or {}).get("noAnswerScore", 0.7)),
    source_id=source_id,
    extra_source_ids=source_ids[1:],
    server_name=str(cfg.get("serverName", "")),
    proposals=cfg.get("proposals") or None,
    ado_sync=ado,
    retain_data=bool(cfg.get("retainData", False)),
    monitoring=monitoring,
    gateway_debug_errors=auth.debug_errors,
    env=cdk.Environment(
        # A deploy from another account (CI in tooling) names the target here.
        account=cfg.get("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=region,
    ),
    description="Enterprise knowledge base served over MCP (Bedrock KB + AgentCore)",
)
# Path B installs (docs/install-permissions.md): every role the stack makes
# gets this boundary, which the scoped CloudFormation execution policy demands.
if cfg.get("permissionsBoundary"):
    iam.PermissionsBoundary.of(stack).apply(
        iam.ManagedPolicy.from_managed_policy_name(
            stack, "PermissionsBoundary", cfg["permissionsBoundary"]
        )
    )
cdk.Tags.of(stack).add("project", prefix)
cdk.Tags.of(stack).add("managed-by", "cdk")
app.synth()
