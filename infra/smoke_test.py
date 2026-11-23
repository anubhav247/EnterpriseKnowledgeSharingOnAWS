#!/usr/bin/env python3
"""IAM-signed smoke test of every deployed MCP runtime.

Calls initialize, tools/list, list_documents, search_knowledge_base and
retrieve_document on each runtime in the stack outputs, and checks that
specialist servers stay inside their namespace.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import uuid

import boto3

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from settings import deployment, outputs, sources  # noqa: E402

# The base version deploys one server, "kb", over one source namespace.
QUERY = {"Kb": "how is net revenue defined"}

client = boto3.client("bedrock-agentcore", region_name=deployment().get("region", "us-east-1"))

# Usage events name this caller (the runtime allowlists the header).
CALLER_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Caller-Id"
CALLER = "ci-smoke-test"


def _add_caller(request, **_):
    request.headers[CALLER_HEADER] = CALLER


client.meta.events.register("before-sign.bedrock-agentcore.InvokeAgentRuntime", _add_caller)


def call(arn: str, session: str, method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session,
        payload=json.dumps(body).encode(),
        contentType="application/json",
        accept="application/json, text/event-stream",
    )
    raw = resp["response"].read().decode()
    if raw.startswith("event:") or "data:" in raw[:20]:
        raw = next(line[5:] for line in raw.splitlines() if line.startswith("data:"))
    msg = json.loads(raw)
    if "error" in msg:
        raise RuntimeError(msg["error"])
    return msg["result"]


def tool(arn: str, session: str, name: str, args: dict) -> dict:
    result = call(arn, session, "tools/call", {"name": name, "arguments": args})
    if result.get("isError"):
        raise RuntimeError(result["content"][0]["text"])
    return json.loads(result["content"][0]["text"])


def use_team_questions(path: str) -> None:
    """Replace the sample questions with a team's smoke.json (templates/team-content).

    {"answered": [{"question": "...", "document": "nda-policy.md"}],
     "unanswered": ["..."], "instructionsContain": "..."}
    """
    global SAMPLE_QUESTIONS, OFF_TOPIC_QUESTIONS, INSTRUCTIONS_CONTAIN
    spec = json.loads(pathlib.Path(path).read_text())
    SAMPLE_QUESTIONS = [(q["question"], q["document"]) for q in spec["answered"]]
    OFF_TOPIC_QUESTIONS = list(spec.get("unanswered", []))
    INSTRUCTIONS_CONTAIN = spec.get("instructionsContain", "")
    QUERY["Kb"] = SAMPLE_QUESTIONS[0][0]


def check_server(init: dict) -> None:
    """serverInfo.name is serverName from deployment.json; instructions are the team's."""
    name, text = init["serverInfo"]["name"], init.get("instructions", "")
    notice("Server", f"{name} | instructions: {text[:300]}")
    want = deployment().get("serverName")
    assert not want or name == want, f"server name {name} != {want}"
    assert INSTRUCTIONS_CONTAIN in text, f"instructions lack {INSTRUCTIONS_CONTAIN!r}"


def check_tools(listed: list[dict]) -> None:
    """The search tool names this team's knowledge base (#72)."""
    text = next(t["description"] for t in listed if t["name"] == "search_knowledge_base")
    notice("Search tool", text)
    want = deployment().get("serverName")
    assert not want or f"Knowledge base: {want}." in text, f"search tool lacks {want}"


def main() -> int:
    if "--questions" in sys.argv:
        use_team_questions(sys.argv[sys.argv.index("--questions") + 1])
    out = outputs()
    EXPECT_NAMESPACES = {"Kb": sorted(s["id"] for s in sources())}
    failures = 0
    for key, expected in EXPECT_NAMESPACES.items():
        arn = out[f"RuntimeArn{key}"]
        session = str(uuid.uuid4()) + "-smoke"  # >= 33 chars required
        try:
            init = call(arn, session, "initialize", {"protocolVersion": "2025-06-18"})
            check_server(init)
            listed = call(arn, session, "tools/list")["tools"]
            tools = [t["name"] for t in listed]
            notice("tools/list", ", ".join(tools))
            check_tools(listed)
            docs = tool(arn, session, "list_documents", {})
            doc_ids = [d["document_id"] for d in (docs.get("documents") or docs)]
            hits = tool(
                arn, session, "search_knowledge_base", {"query": QUERY.get(key, "overview")}
            )
            results = hits.get("results", hits)
            top = results[0]
            full = tool(arn, session, "retrieve_document", {"document_id": top["document_id"]})

            namespaces = sorted({d.split("/")[0] for d in doc_ids})
            assert namespaces == expected, f"namespaces {namespaces} != {expected}"

            print(
                f"[PASS] {key:9} server={init['serverInfo']['name']} "
                f"proto={init['protocolVersion']} tools={len(tools)} docs={len(doc_ids)} "
                f"namespaces={namespaces}"
            )
            print(
                f"        top hit {top['document_id']} | {top['section'][:50]} "
                f"| score {top['relevance_score']} | retrieved {len(full['content'])} chars"
            )
            notice("Source links", f"hit {top.get('source_url')} | doc {full.get('source_url')}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {key}: {exc}")
    failures += sample_questions(out)
    return 1 if failures else 0


# The two owner sample questions (docs/first-deploy-findings.md, section 6) and
# the document that holds each answer.
SAMPLE_QUESTIONS = [
    (
        "What is the freshness target for the Curated layer, and how is it measured?",
        "sla-and-freshness.md",
    ),
    ("Who do I escalate to for an on-call data incident?", "oncall-escalation.md"),
]
# Nothing in the sample content answers these: the usage report must list them.
OFF_TOPIC_QUESTIONS = [
    "What is our policy on lunar data centres?",
    "What is the cafeteria menu on Fridays?",
    "How do I renew my passport?",
]
# Text the server instructions must hold ("" = any); set from smoke.json.
INSTRUCTIONS_CONTAIN = ""


def notice(title: str, message: str) -> None:
    """Print; in GitHub Actions also as a notice, which the check-run API shows."""
    print(f"{title}: {message}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        flat = message.replace("%", "%25").replace("\r", " ").replace("\n", " ")
        print(f"::notice title={title}::{flat}")


def top_score(hits: dict) -> float:
    """Best document score of a search: what usage_log compares to the threshold."""
    docs = hits.get("documents") or []
    return docs[0]["best_score"] if docs else 0.0


def no_answer_score() -> float:
    """The runtime's USAGE_NO_ANSWER_SCORE, from deployment.json usage."""
    return float((deployment().get("usage") or {}).get("noAnswerScore", 0.7))


def sample_questions(out: dict) -> int:
    """Sample questions: answer document in the top 3 and not flagged no-answer.

    Off-topic questions must be flagged: top score under the no-answer score.
    """
    cutoff = no_answer_score()
    for key in ("GatewayUrl", "GatewayUserRoleArn"):
        if key in out:
            notice(key, out[key])
    arn = out["RuntimeArnKb"]
    session = str(uuid.uuid4()) + "-sample"
    failures = 0
    for n, (question, expected) in enumerate(SAMPLE_QUESTIONS, 1):
        try:
            hits = tool(arn, session, "search_knowledge_base", {"query": question})
            results = (hits.get("results", hits))[:3]
            ids = [r["document_id"] for r in results]
            ok = any(i.endswith(expected) for i in ids) and top_score(hits) >= cutoff
            top = results[0]
            notice(
                f"Q{n} {'PASS' if ok else 'FAIL'}",
                f"{question} | score {top_score(hits)} | top {ids} | {top['section'][:60]} "
                f"| link {top.get('source_url', 'none')} | {top.get('snippet', '')[:300]}",
            )
            failures += 0 if ok else 1
        except Exception as exc:  # noqa: BLE001
            failures += 1
            notice(f"Q{n} FAIL", f"{question} | {exc}")
    for n, question in enumerate(OFF_TOPIC_QUESTIONS, len(SAMPLE_QUESTIONS) + 1):
        try:
            hits = tool(arn, session, "search_knowledge_base", {"query": question})
            top = (hits.get("documents") or [{}])[0]
            ok = top_score(hits) < cutoff
            notice(
                f"Q{n} {'PASS' if ok else 'FAIL'} off-topic",
                f"{question} | score {top_score(hits)} (no answer under {cutoff}) "
                f"| top {top.get('document_id')}",
            )
            failures += 0 if ok else 1
        except Exception as exc:  # noqa: BLE001
            failures += 1
            notice(f"Q{n} FAIL", f"{question} | {exc}")
    return failures


if __name__ == "__main__":
    sys.exit(main())
