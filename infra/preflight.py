#!/usr/bin/env python3
"""Check an account and workstation can deploy this stack, before cdk deploy.

Read-only: it creates nothing. Each check calls the real service, so a denial
from a service control policy (SCP) shows up here as FAIL instead of halfway
through a CloudFormation deployment.

    python infra/preflight.py
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from settings import deployment  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from identity.config import resolve  # noqa: E402

EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool | None, detail: str = "") -> None:
    status = "PASS" if ok else ("WARN" if ok is None else "FAIL")
    results.append((status, name, detail))
    print(f"[{status}] {name}{' - ' + detail if detail else ''}", flush=True)


def aws_check(name: str, fn, detail_fn=lambda r: "") -> object | None:
    try:
        r = fn()
        record(name, True, detail_fn(r))
        return r
    except (ClientError, BotoCoreError) as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        record(name, False, f"{code}: {str(exc)[:160]}")
        return None


def main() -> int:
    cfg = deployment()
    region = cfg.get("region", "us-east-1")
    session = boto3.Session(region_name=region)

    # ------------------------------------------------------------ configuration
    record("deployment.json source.id", bool(cfg.get("source", {}).get("id")), cfg.get("source", {}).get("id", ""))
    try:
        auth = resolve(cfg, region)
        if auth.mode == "iam":
            record("Gateway configured", True, "on: AWS IAM (SigV4) sign-in")
        elif auth.mode == "oidc":
            record("Gateway configured", True,
                   "on: OIDC, Cognito test pool made by the stack" if auth.cognito_create
                   else f"on: OIDC ({auth.oidc.label if auth.oidc else ''})")
        else:
            record("Gateway configured", None,
                   'off: no developer access until gatewayAuth "oidc" or "iam" is set')
    except ValueError as exc:
        record("Gateway configured", False, str(exc))
    ado = cfg.get("azureDevOps") or {}
    if ado.get("orgUrl") and "<" not in ado["orgUrl"]:
        # Config only: EKS never calls Azure DevOps (a private org answers with a sign-in page).
        record("Azure DevOps orgId", bool(ado.get("orgId")) or None,
               "set" if ado.get("orgId") else
               f"empty; set azureDevOps.orgId to instanceId of {ado['orgUrl'].rstrip('/')}"
               "/_apis/connectionData (open it in a signed-in browser)")
    else:
        record("Azure DevOps sync configured", None, "off: no pipeline role until azureDevOps.* is set")

    # -------------------------------------------------------------- identity
    ident = aws_check("AWS credentials", lambda: session.client("sts").get_caller_identity(),
                      lambda r: f"{r['Account']} as {r['Arn'].split('/')[-2] if 'assumed-role' in r['Arn'] else r['Arn']}")
    if not ident:
        return summary()

    # -------------------------------------------------------------- services
    bedrock = session.client("bedrock")
    aws_check("Bedrock embedding model access",
              lambda: _model_available(bedrock),
              lambda r: f"{EMBEDDING_MODEL} {r}")
    aws_check("Bedrock knowledge bases", lambda: session.client("bedrock-agent").list_knowledge_bases(maxResults=1),
              lambda r: f"{len(r.get('knowledgeBaseSummaries', []))} visible")
    aws_check("S3 Vectors", lambda: session.client("s3vectors").list_vector_buckets(maxResults=1))
    aws_check("AgentCore Runtime", lambda: session.client("bedrock-agentcore-control").list_agent_runtimes(maxResults=1))
    aws_check("AgentCore Gateway", lambda: session.client("bedrock-agentcore-control").list_gateways(maxResults=1))
    aws_check("IAM read (roles, OIDC providers)",
              lambda: session.client("iam").list_open_id_connect_providers())
    aws_check("CloudFormation", lambda: session.client("cloudformation").describe_stacks(StackName="CDKToolkit"),
              lambda r: "CDK bootstrapped (CDKToolkit "
                        f"{r['Stacks'][0]['StackStatus']})")

    # ------------------------------------------------------------- workstation
    docker = shutil.which("docker")
    if cfg.get("runtimeArtifact", "image") == "code":
        record("Docker", True, 'not needed: runtimeArtifact "code"')
    elif not docker:
        record("Docker", False, "not installed; needed to build the arm64 MCP image")
    else:
        r = subprocess.run([docker, "buildx", "ls"], capture_output=True, text=True)
        arm = "linux/arm64" in r.stdout
        record("Docker buildx linux/arm64", r.returncode == 0 and arm,
               "available" if arm else "no arm64 builder; install QEMU or use an arm64 machine")
    record("Node.js (CDK CLI)", bool(shutil.which("node")), shutil.which("node") or "not installed")

    return summary()


def _model_available(bedrock) -> str:
    r = bedrock.get_foundation_model_availability(modelId=EMBEDDING_MODEL)
    status = r.get("authorizationStatus")
    if status != "AUTHORIZED":
        raise ClientError({"Error": {"Code": "ModelNotAuthorized",
                                     "Message": f"authorizationStatus={status}"}}, "GetFoundationModelAvailability")
    return "authorized"


def summary() -> int:
    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    print(f"\n{len(results) - len(fails) - len(warns)} pass, {len(warns)} warn, {len(fails)} fail")
    if any(r[1] == "CloudFormation" for r in fails):
        print("If only CloudFormation failed: run `cdk bootstrap` once for this account and region.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
