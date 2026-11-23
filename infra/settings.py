"""Shared access to deployment.json and the CDK stack outputs.

Every script in this repository reads its parameters from deployment.json at the
repository root, and the deployed resource ids from infra/outputs.json, which
`cdk deploy --outputs-file infra/outputs.json` writes.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
DEPLOYMENT_FILE = REPO / "deployment.json"
OUTPUTS_FILE = REPO / "infra" / "outputs.json"

# EKS's own CDK set-up (cdk bootstrap): stack kbmcp-cdk-toolkit, resources
# cdk-kbmcp-*. Never the account's default CDKToolkit (qualifier hnb659fds):
# in a customer account that may belong to another team.
CDK_QUALIFIER = "kbmcp"
CDK_TOOLKIT_STACK = "kbmcp-cdk-toolkit"
# On every resource EKS makes; install.sh --inventory finds them by it.
MANAGED_TAG = "kbmcp:managed"
STACK_TAG = "kbmcp:stack"


def cdk_qualifier() -> str:
    """kbmcp. EKS_CDK_QUALIFIER overrides it for the factory's own accounts only.

    shortcut: the factory's cross-account CI deploys (tooling -> solution) may
    only assume cdk-hnb659fds-* roles today; drop the override once the
    solution account has kbmcp-cdk-toolkit with --trust and ekb-github-oidc
    allows cdk-kbmcp-* (docs/deploy.md).
    """
    q = os.environ.get("EKS_CDK_QUALIFIER") or CDK_QUALIFIER
    if not re.fullmatch(r"[a-z0-9]{1,10}", q):
        raise SystemExit(f"EKS_CDK_QUALIFIER: 1-10 lowercase letters or digits, not {q!r}")
    return q


def deployment() -> dict[str, Any]:
    if not DEPLOYMENT_FILE.is_file():
        raise SystemExit(
            f"{DEPLOYMENT_FILE} not found. Copy deployment.example.json to "
            "deployment.json and fill it in."
        )
    return json.loads(DEPLOYMENT_FILE.read_text())


SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PROVIDERS = ("github", "azure-devops", "gitlab", "local")


def sources(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The content sources: "sources" (a list), else the single "source".

    Each is {id, provider, repo, path, branch}; only id is required. Each id is
    its own namespace (knowledge-base/<id>/, chunks/<id>/) in the one index.
    The first is the primary: its repo holds the team's mcp/ and smoke.json.
    """
    cfg = deployment() if cfg is None else cfg
    items = cfg.get("sources") or [cfg.get("source") or {"id": "analytics"}]
    ids = [s.get("id", "") for s in items]
    bad = [i for i in ids if not SOURCE_ID_RE.match(i)]
    if bad or len(set(ids)) != len(ids):
        raise SystemExit(f"deployment.json sources: ids must be unique and lowercase: {ids}")
    for s in items:
        if s.get("provider", "local") not in PROVIDERS:
            raise SystemExit(f"source {s['id']}: provider must be one of {PROVIDERS}")
    return items


def stack_name() -> str:
    return f"{deployment().get('prefix', 'kb-mcp')}-stack"


def outputs() -> dict[str, str]:
    if not OUTPUTS_FILE.is_file():
        raise SystemExit(
            f"{OUTPUTS_FILE} not found. Run cdk deploy with "
            "--outputs-file outputs.json from infra/."
        )
    all_outputs = json.loads(OUTPUTS_FILE.read_text())
    name = stack_name()
    if name not in all_outputs:
        raise SystemExit(f"stack {name} not in {OUTPUTS_FILE}; found {list(all_outputs)}")
    return all_outputs[name]
