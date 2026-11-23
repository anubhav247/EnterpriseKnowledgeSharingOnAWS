"""Usage events: one JSON line per tool call, so publishers can see use.

On AgentCore Runtime the container's stdout goes to CloudWatch Logs, where
`python -m reports.usage --log-group ...` reads the lines back. The stdio
transport uses stdout for JSON-RPC, so it switches these lines to stderr.

Config (environment):
    USAGE_LOG                 "0" turns usage events off. Default on.
    USAGE_LOG_QUERIES         "0" drops query text from events (privacy).
                              Default on; text is masked and cut to 300 chars.
    USAGE_NO_ANSWER_SCORE     a search whose top score is under this counts as
                              no answer. Default 0.7.

Never logged: tokens, headers other than the caller id, document text.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, TextIO

QUERY_MAX_CHARS = 300
TOP_DOCS = 5

# Masks, applied in order. Each pattern is replaced by its label.
_MASKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), "[token]"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "[token]"),  # JWT
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[aws-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[token]"),
    (
        re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"),
        r"\1=[secret]",
    ),
    # Long opaque strings with a digit (keys, hashes); long table names survive.
    (re.compile(r"\b(?=[A-Za-z0-9+/-]*\d)[A-Za-z0-9+/-]{32,}={0,2}"), "[secret]"),
]

_stream: TextIO | None = None


def use_stream(stream: TextIO | None) -> None:
    """Send events to `stream` instead of stdout (None = stdout)."""
    global _stream
    _stream = stream


def _on(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in ("0", "false", "no", "off")


# Measured on our demo stack (Titan v2 embeddings, 2026-11-22): real answers 0.78 and
# 0.80, off-topic questions 0.56 to 0.63. shortcut: one score cut-off; re-measure
# when the embedding model or the content changes (deploy smoke prints scores).
DEFAULT_NO_ANSWER_SCORE = 0.7


def no_answer_score() -> float:
    try:
        return float(os.environ.get("USAGE_NO_ANSWER_SCORE", DEFAULT_NO_ANSWER_SCORE))
    except ValueError:
        return DEFAULT_NO_ANSWER_SCORE


def mask(text: str) -> str:
    """Hide obvious secrets and emails, then cut to QUERY_MAX_CHARS."""
    for pattern, label in _MASKS:
        text = pattern.sub(label, text)
    return text[:QUERY_MAX_CHARS]


def _results(tool: str, arguments: dict[str, Any], payload: dict[str, Any] | None):
    """(result_count, top docs) for one tool's payload."""
    if payload is None:
        return 0, []
    if tool == "search_knowledge_base":
        docs = [
            {"document_id": d["document_id"], "score": round(float(d["best_score"]), 4)}
            for d in payload.get("documents", [])[:TOP_DOCS]
        ]
        return int(payload.get("result_count", 0)), docs
    if tool == "retrieve_document":
        doc_id = payload.get("document_id") or arguments.get("document_id")
        return 1, [{"document_id": doc_id}]
    if tool == "list_documents":
        return int(payload.get("document_count", 0)), []
    return None, []


def build_event(
    tool: str,
    arguments: dict[str, Any],
    payload: dict[str, Any] | None,
    request_id: Any,
    latency_ms: float,
    caller: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """The usage event for one finished tool call."""
    result_count, top = _results(tool, arguments, payload)
    event: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "tool_call",
        "tool": tool,
        "request_id": request_id,
        "result_count": result_count,
        "top_documents": top,
        "latency_ms": round(latency_ms, 1),
    }
    if tool == "search_knowledge_base":
        query = str(arguments.get("query", ""))
        event["query"] = mask(query) if _on("USAGE_LOG_QUERIES") else None
        top_score = top[0]["score"] if top else 0.0
        event["no_answer"] = result_count == 0 or top_score < no_answer_score()
    else:
        event["no_answer"] = None
    if caller:
        event["caller"] = caller[:128]
    if error:
        event["error"] = error[:200]
    return event


def record(
    tool: str,
    arguments: dict[str, Any],
    payload: dict[str, Any] | None,
    request_id: Any,
    started: float,
    caller: str | None = None,
    error: str | None = None,
) -> None:
    """Write one usage event. Never raises: logging must not break a call."""
    if not _on("USAGE_LOG"):
        return
    try:
        latency = (time.monotonic() - started) * 1000
        event = build_event(tool, arguments, payload, request_id, latency, caller, error)
        stream = _stream or sys.stdout
        stream.write(json.dumps(event, default=str) + "\n")
        stream.flush()
    except Exception:  # noqa: BLE001, S110 - usage logging is best effort
        pass
