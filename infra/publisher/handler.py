"""Copy domain-published instructions onto the gateway and refresh its catalog.

Invoked by the content pipeline after it publishes mcp/<source>/published.json,
and by CloudFormation on every deploy (a deploy that touches the gateway would
otherwise drop instructions the template does not know about).

The event payload is ignored: which gateway, which bucket and which object are
fixed by environment variables set in CDK. The caller can only say "publish
now", so the pipeline role that may invoke this function cannot use it to
change the gateway's authorizer, role or anything other than instructions.
"""

from __future__ import annotations

import json
import os
import time

import boto3

GATEWAY_ID = os.environ["GATEWAY_ID"]
DOCS_BUCKET = os.environ["DOCS_BUCKET"]
PUBLISHED_KEY = os.environ["MCP_PUBLISHED_KEY"]
DEFAULT_INSTRUCTIONS = os.environ["DEFAULT_INSTRUCTIONS"]
MAX_INSTRUCTIONS = 2048

control = boto3.client("bedrock-agentcore-control")
s3 = boto3.client("s3")


def _published_instructions() -> str:
    try:
        obj = s3.get_object(Bucket=DOCS_BUCKET, Key=PUBLISHED_KEY)
    except s3.exceptions.NoSuchKey:
        return ""
    data = json.loads(obj["Body"].read())
    return str(data.get("instructions") or "")


def _wait_ready(get, what: str, timeout: float = 240) -> dict:
    deadline = time.time() + timeout
    while True:
        current = get()
        status = current.get("status")
        if status in ("READY", "FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"):
            return current
        if time.time() > deadline:
            raise TimeoutError(f"{what} still {status} after {timeout}s")
        time.sleep(3)


def _update_instructions(gateway: dict, text: str) -> None:
    # UpdateGateway replaces the whole configuration, so send back every field
    # the gateway already has and change only instructions.
    allowed = control.meta.service_model.operation_model("UpdateGateway").input_shape.members
    request = {k: v for k, v in gateway.items() if k in allowed and v not in (None, "", [], {})}
    request["gatewayIdentifier"] = GATEWAY_ID
    mcp = dict(request["protocolConfiguration"]["mcp"])
    mcp["instructions"] = text
    request["protocolConfiguration"] = {**request["protocolConfiguration"], "mcp": mcp}
    control.update_gateway(**request)


def handler(event, context):  # noqa: ARG001
    # CloudFormation custom resource events: publish on create and update; on
    # delete the gateway may already be gone, so do nothing.
    if isinstance(event, dict) and event.get("RequestType") == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "mcp-publish")}
    result = publish()
    if isinstance(event, dict) and "RequestType" in event:
        return {"PhysicalResourceId": "mcp-publish",
                "Data": {"InstructionsChanged": str(result["instructions_changed"])}}
    return result


def publish() -> dict:
    text = _published_instructions() or DEFAULT_INSTRUCTIONS
    if len(text) > MAX_INSTRUCTIONS:
        raise ValueError(f"instructions are {len(text)} chars, gateway max is {MAX_INSTRUCTIONS}")

    get_gateway = lambda: control.get_gateway(gatewayIdentifier=GATEWAY_ID)  # noqa: E731
    gateway = _wait_ready(get_gateway, "gateway")
    changed = (gateway.get("protocolConfiguration", {}).get("mcp", {}).get("instructions") or "") != text
    if changed:
        _update_instructions(gateway, text)
        gateway = _wait_ready(get_gateway, "gateway")
        if gateway["status"] != "READY":
            raise RuntimeError(f"gateway update ended {gateway['status']}: {gateway.get('statusReasons')}")

    # Prompts and resources are listed from the gateway's cache, refreshed only
    # by a sync. Run it on every publish so new or removed prompts and documents
    # show up for clients.
    targets = [t["targetId"] for t in control.list_gateway_targets(
        gatewayIdentifier=GATEWAY_ID).get("items", [])]
    synced = []
    for target_id in targets:
        get_target = lambda: control.get_gateway_target(  # noqa: E731
            gatewayIdentifier=GATEWAY_ID, targetId=target_id)
        _wait_ready(get_target, f"target {target_id}")
        control.synchronize_gateway_targets(gatewayIdentifier=GATEWAY_ID, targetIdList=[target_id])
        result = _wait_ready(get_target, f"target {target_id}")
        synced.append({"target": target_id, "status": result["status"]})

    return {"instructions_changed": changed, "instructions_chars": len(text), "targets": synced}
