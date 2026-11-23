#!/usr/bin/env python3
"""Live proof of propose_change (#70), through the gateway, on a draft server.

Run by deploy-team-demo.yml (job propose-live) after `add-team.sh --draft`,
with GITHUB_TOKEN set to the repo secret PROPOSALS_TOKEN (a fine-grained PAT):

1. assume the stack's CI content role; put GITHUB_TOKEN into the stack's
   proposals secret (the bot token the server opens PRs with)
2. tools/list on the gateway (SigV4): kb___propose_change is listed
3. retrieve the NDA policy, change its term, call kb___propose_change
4. check on GitHub: the PR is open and goes into the draft branch
5. close the PR and delete its branch (a smoke test never merges)

The demo's stack secret then holds that PAT until it expires
(docs/propose-change.md).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from settings import deployment, outputs  # noqa: E402

DOC = "legal/nda-policy.md"
TARGET = "kb"


def notice(title: str, message: str) -> None:
    print(f"{title}: {message}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title={title}::{message}")


def content_session(cfg: dict) -> boto3.Session:
    role = (cfg.get("ciContentRole") or {}).get("roleName") or f"{cfg['prefix']}-ci-content"
    arn = f"arn:aws:iam::{cfg['account']}:role/{role}"
    c = boto3.client("sts").assume_role(RoleArn=arn, RoleSessionName="propose-smoke")
    c = c["Credentials"]
    return boto3.Session(
        aws_access_key_id=c["AccessKeyId"],
        aws_secret_access_key=c["SecretAccessKey"],
        aws_session_token=c["SessionToken"],
        region_name=cfg["region"],
    )


class Gateway:
    def __init__(self, url: str, sess: boto3.Session) -> None:
        self.url, self.sess, self.session_id = url, sess, ""

    def rpc(self, method: str, params: dict | None = None) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:  # only the id the gateway gave on initialize
            headers["Mcp-Session-Id"] = self.session_id
        req = AWSRequest(method="POST", url=self.url, data=body, headers=headers)
        creds = self.sess.get_credentials().get_frozen_credentials()
        SigV4Auth(creds, "bedrock-agentcore", self.sess.region_name).add_auth(req)
        http = urllib.request.Request(  # noqa: S310  the stack's https gateway URL
            self.url, data=body.encode(), headers=dict(req.headers), method="POST"
        )
        with urllib.request.urlopen(http, timeout=120) as r:  # noqa: S310
            self.session_id = r.headers.get("Mcp-Session-Id") or self.session_id
            raw = r.read().decode()
        if "data:" in raw[:200]:
            raw = next(line[5:] for line in raw.splitlines() if line.startswith("data:"))
        msg = json.loads(raw)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg["result"]

    def tool(self, name: str, args: dict) -> dict:
        result = self.rpc("tools/call", {"name": f"{TARGET}___{name}", "arguments": args})
        payload = json.loads(result["content"][0]["text"])
        if result.get("isError"):
            raise RuntimeError(f"{name}: {payload}")
        return payload


def github(method: str, route: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(  # noqa: S310  fixed https://api.github.com base
        f"https://api.github.com{route}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        raw = r.read()
    return json.loads(raw) if raw else {}


def error(step: str, exc: BaseException) -> None:
    """The failing step as a check-run annotation (the job log is not readable to all)."""
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, urllib.error.HTTPError):
        detail += " | " + exc.read().decode(errors="replace")
    flat = detail[:800].replace("%", "%25").replace("\r", " ").replace("\n", " ")
    print(f"FAIL {step}: {flat}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error title=propose_change {step}::{flat}")


STEP = ["start"]


def main() -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001  report the step, then fail
        error(STEP[0], exc)
        return 1


def run() -> int:
    cfg, out = deployment(), outputs()
    target = cfg["proposals"][DOC.split("/")[0]]
    STEP[0] = "assume content role"
    sess = content_session(cfg)
    STEP[0] = "put token in secret"
    sess.client("secretsmanager").put_secret_value(
        SecretId=out["ProposalsTokenSecretArn"], SecretString=os.environ["GITHUB_TOKEN"]
    )
    gw = Gateway(out["GatewayUrl"], sess)
    STEP[0] = "gateway initialize"
    init = {"capabilities": {}, "clientInfo": {"name": "propose-smoke", "version": "1"}}
    gw.rpc("initialize", {"protocolVersion": "2025-06-18", **init})
    STEP[0] = "gateway tools/list"
    names = [t["name"] for t in gw.rpc("tools/list")["tools"]]
    assert f"{TARGET}___propose_change" in names, f"propose_change not listed: {names}"  # noqa: S101

    STEP[0] = "retrieve_document"
    text = gw.tool("retrieve_document", {"document_id": DOC})["content"]
    new = text.replace("three years", "3 years", 1)
    if new == text:
        new = text + "\n<!-- propose_change smoke test -->\n"
    STEP[0] = "propose_change"
    made = gw.tool(
        "propose_change",
        {"document_id": DOC, "new_text": new, "summary": "Update the NDA term to 3 years"},
    )
    notice("propose_change", f"{made['url']} into {made['target_branch']}")

    STEP[0] = "check and close PR"
    repo = target["base"].removeprefix("https://github.com/")
    number = re.search(r"/pull/(\d+)$", made["url"])
    assert number, made["url"]  # noqa: S101
    pr = github("GET", f"/repos/{repo}/pulls/{number.group(1)}")
    ok = pr["state"] == "open" and pr["base"]["ref"] == target["branch"]
    notice(f"PR {'PASS' if ok else 'FAIL'}", f"{pr['html_url']} base {pr['base']['ref']}")
    github("PATCH", f"/repos/{repo}/pulls/{number.group(1)}", {"state": "closed"})
    github("DELETE", f"/repos/{repo}/git/refs/heads/{made['branch']}")
    notice("PR closed", f"{pr['html_url']} closed, branch {made['branch']} deleted")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
