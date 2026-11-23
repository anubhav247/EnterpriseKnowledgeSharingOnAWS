"""MCP server exposing the analytics knowledge base as a retrieval surface.

Design intent: this server is a search index, not an agent. It ranks and returns
source material; the calling agent decides what to read and what it means.

The tool surface is deliberately small, modelled on how a person uses a search
engine:

    search_knowledge_base  ranked snippets with document ids
    retrieve_document      the full text of one document

Two supporting tools report what exists and how fresh it is. No tool invokes a
language model, so no interpretation is imposed on the caller and there is no
hidden inference cost.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

import engines
import mcp_content
import proposals
import usage_log
from kb_client import SOURCE_SCOPE, KnowledgeBaseError

_ENGINE: engines.RetrievalEngine | None = None


def engine() -> engines.RetrievalEngine:
    """The retrieval engine named by RETRIEVAL_ENGINE (engines.py), made once."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = engines.from_env()
    return _ENGINE


PROTOCOL_VERSION = "2024-11-05"

# Versions this server can speak. The tool surface is identical across them, so
# echoing the client's requested version is safe. AgentCore Gateway and newer
# clients such as Kiro request 2025-xx versions and may refuse an older answer.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def negotiate_protocol_version(requested: Any) -> str:
    """Echo the client's version when supported, else offer the newest we know."""
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return SUPPORTED_PROTOCOL_VERSIONS[0]


# The same server binary runs as either a generalist (spanning every content
# repository in the knowledge base) or a specialist (locked to one source
# namespace via SOURCE_SCOPE). MCP_SERVER_NAME lets a launcher label the two
# distinctly so a client such as Kiro lists them as separate servers.
SERVER_NAME = os.environ.get(
    "MCP_SERVER_NAME",
    f"kb-{SOURCE_SCOPE}" if SOURCE_SCOPE else "kb-general",
)
SERVER_VERSION = "2.3.0"

# The tool contract: names, inputs and outputs in schemas/mcp-tools.v1.json.
# Fields are only ever added (tests/test_contract.py). get_sync_status reports it,
# since the gateway answers initialize itself and hides this server's serverInfo.
CONTRACT_VERSION = "1"

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# --------------------------------------------------------------------------- tools


def tool_search_knowledge_base(
    query: str,
    max_results: int = 10,
    document_id: str | None = None,
    snippet_chars: int = 600,
) -> dict[str, Any]:
    """Rank chunks against a query and return them as search results."""
    passages = engine().search(query, max_results, document_id=document_id)
    results = [p.as_result(snippet_chars) for p in passages]

    # Distinct documents behind the hits, so an agent can see at a glance which
    # documents are worth retrieving in full.
    documents: dict[str, dict[str, Any]] = {}
    for passage, result in zip(passages, results):
        entry = documents.setdefault(
            passage.document_id,
            {"document_id": passage.document_id, "hits": 0, "best_score": 0.0},
        )
        entry["hits"] += 1
        entry["best_score"] = max(entry["best_score"], result["relevance_score"])

    return {
        "query": query,
        "result_count": len(results),
        "results": results,
        "documents": sorted(documents.values(), key=lambda d: d["best_score"], reverse=True),
        "next_step": (
            "Call retrieve_document with a document_id to read full context before answering."
        ),
    }


def tool_retrieve_document(document_id: str) -> dict[str, Any]:
    """Return one complete document."""
    return engine().retrieve(document_id)


def tool_list_documents() -> dict[str, Any]:
    docs = engine().list_documents()
    return {"document_count": len(docs), "documents": docs}


def tool_get_sync_status() -> dict[str, Any]:
    status = {**engine().sync_status(), "contract_version": CONTRACT_VERSION}
    if mcp_content.version_stamp():
        status["current_instructions"] = current_instructions()
    checks = mcp_content.content_checks()
    if checks:
        status["content_checks"] = checks
    return status


def _exists(document_id: str) -> bool:
    try:
        engine().retrieve(document_id)
    except KnowledgeBaseError:
        return False
    return True


def tool_propose_change(
    document_id: str, new_text: str, summary: str, _caller: str | None = None
) -> dict[str, Any]:
    """Open a pull request with the new text into the draft branch (proposals.py)."""
    return proposals.propose(document_id, new_text, summary, _caller, _exists)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search this knowledge base and return ranked "
            "excerpts. Works like a search engine: each result carries a "
            "document_id, the section it came from, a snippet, and a relevance "
            "score. Snippets are for judging relevance, not for answering. When "
            "a result looks relevant, call retrieve_document with its "
            "document_id to read the full context before you answer. Use this "
            "first for any question this knowledge base covers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to search for. Include exact names, terms or ids "
                        "when you know them, since they match strongly."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of excerpts to return. Default 10, max 100.",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 100,
                },
                "document_id": {
                    "type": "string",
                    "description": (
                        "Optional. Restrict the search to one document, for "
                        "example 'team/overview.md'."
                    ),
                },
                "snippet_chars": {
                    "type": "integer",
                    "description": "Snippet length in characters. Default 600.",
                    "default": 600,
                    "minimum": 100,
                    "maximum": 4000,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "retrieve_document",
        "description": (
            "Return the complete text of one document, identified by the "
            "document_id from a search_knowledge_base result. Use this when a "
            "search snippet looks relevant but you need the surrounding context: "
            "the whole section, the full example, or the caveats that follow a "
            "definition. Prefer this over guessing from a snippet, because "
            "snippets are truncated and a partial passage can be misleading."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": (
                        "Document identifier from a search result, for example 'team/overview.md'."
                    ),
                }
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "list_documents",
        "description": (
            "List every document available, with size and last "
            "modified time. Use to discover what topics the knowledge base "
            "covers, or to find a document_id when a search returns nothing."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sync_status",
        "description": (
            "Report when the knowledge base was last indexed and how many "
            "documents it holds, plus the current server instructions and the "
            "tool contract_version. Use to "
            "check whether a recent documentation change has propagated before "
            "trusting an answer about it, or to fetch the latest instructions "
            "when a tool result reports a newer instructions_version."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# Listed only when proposals are on (PROPOSALS, the draft server).
PROPOSE_TOOL: dict[str, Any] = {
    "name": "propose_change",
    "description": (
        "Propose a change to one document. Opens a pull request into the team's "
        "draft branch with the full new text; a human publisher reviews and "
        "merges it. Nothing changes until then. Call retrieve_document first, "
        "edit its content, and send the whole new document. Return the url to "
        "the user."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "description": "An existing document_id, for example 'team/overview.md'.",
            },
            "new_text": {
                "type": "string",
                "description": "The complete new markdown of the document, not a diff.",
            },
            "summary": {
                "type": "string",
                "description": "One line: what changes and why. Becomes the PR title.",
            },
        },
        "required": ["document_id", "new_text", "summary"],
    },
}


def tools_list() -> list[dict[str, Any]]:
    """TOOLS, with this team's server name, summary and owner on the search tool.

    The text is neutral; the domain comes from config: serverName
    (MCP_SERVER_NAME) and the owner and first paragraph of the team's
    mcp/instructions.md (published.json). The gateway caches this list and
    refreshes it when the publisher syncs its target.
    shortcut: the runtime caches published.json for up to MCP_CONTENT_CACHE_SECONDS,
    so a sync right after a publish can catch the old summary; the next publish
    fixes it. Make the publisher wait the cache out if that shows up.
    """
    owner, summary = mcp_content.about()
    about = f" Knowledge base: {SERVER_NAME}." + (f" {summary}" if summary else "")
    about += f" Owner: {owner}." if owner else ""
    first = {**TOOLS[0], "description": TOOLS[0]["description"] + about}
    return [first, *TOOLS[1:], *([PROPOSE_TOOL] if proposals.enabled() else [])]


_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "search_knowledge_base": tool_search_knowledge_base,
    "retrieve_document": tool_retrieve_document,
    "list_documents": tool_list_documents,
    "get_sync_status": tool_get_sync_status,
}


def _handler(name: Any) -> Callable[..., dict[str, Any]] | None:
    if name == "propose_change" and proposals.enabled():
        return tool_propose_change
    return _HANDLERS.get(name)


_SCOPE_LINE = (
    (
        f"This is a specialist index over the '{SOURCE_SCOPE}' content "
        f"repository only. It returns documents from that source and nothing "
        f"else; for other repositories use the matching specialist or the "
        f"general server.\n\n"
    )
    if SOURCE_SCOPE
    else (
        "This is the general index. It spans every content repository in the "
        "knowledge base, so results may come from more than one source; each "
        "document_id carries its source namespace.\n\n"
    )
)

INSTRUCTIONS = (
    "A team knowledge base, exposed as a search index. This server "
    "retrieves source material and does not interpret it; "
    "you are the reasoning layer.\n\n"
    + _SCOPE_LINE
    + "Work in two steps, the way you would use a search engine:\n"
    "1. search_knowledge_base to find candidate sections and their document ids.\n"
    "2. retrieve_document on the promising ones to read full context.\n\n"
    "Answer only from retrieved text. Never invent names, figures, definitions "
    "or code, and reproduce them exactly as written. Cite the "
    "document_id behind each claim, with its source_url as a link when the "
    "result has one, so the user can open the page. If the documentation does "
    "not cover something, say so instead of inferring."
)


INSTRUCTIONS_URI = "kb://instructions"
DOCUMENT_URI_PREFIX = "kb://documents/"

STALENESS_CHECK = (
    "Compare instructions_version with the Instructions-Version line in this "
    "server's instructions. If they differ, or your instructions have no such "
    "line, the instructions changed after you connected: read the resource "
    f"{INSTRUCTIONS_URI} (or call get_sync_status) for the current text, follow "
    "it, and tell the user to reconnect this MCP server to load it permanently."
)


def current_instructions() -> str:
    """Domain-published instructions when present, else the built-in default."""
    return mcp_content.instructions() or INSTRUCTIONS


def _stamp(payload: dict[str, Any]) -> dict[str, Any]:
    stamp = mcp_content.version_stamp()
    if not stamp:
        return payload
    return {**payload, **stamp, "instructions_check": STALENESS_CHECK}


# ------------------------------------------------------------------- resources


def list_resources() -> list[dict[str, Any]]:
    resources = [
        {
            "uri": INSTRUCTIONS_URI,
            "name": "Current server instructions",
            "description": (
                "The latest instructions for using this knowledge base. Read this "
                "if a tool result reports a newer instructions_version than yours."
            ),
            "mimeType": "text/markdown",
        }
    ]
    for doc in engine().list_documents():
        resources.append(
            {
                "uri": f"{DOCUMENT_URI_PREFIX}{doc['document_id']}",
                "name": doc["document_id"],
                "mimeType": "text/markdown",
                "size": doc.get("size_bytes"),
            }
        )
    return resources


def read_resource(uri: str) -> dict[str, Any]:
    if uri == INSTRUCTIONS_URI:
        text = current_instructions()
    elif uri.startswith(DOCUMENT_URI_PREFIX):
        text = engine().retrieve(uri[len(DOCUMENT_URI_PREFIX) :])["content"]
    else:
        raise KnowledgeBaseError(f"unknown resource: {uri}")
    return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]}


# ------------------------------------------------------------------- jsonrpc plumbing


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _links(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One MCP resource_link per distinct source_url, so clients can show links."""
    links: dict[str, dict[str, Any]] = {}
    for item in [payload, *(payload.get("results") or [])]:
        url = item.get("source_url") if isinstance(item, dict) else None
        if url and url not in links:
            links[url] = {
                "type": "resource_link",
                "uri": url,
                "name": item.get("document_id", url),
                "description": item.get("section") or "Source page",
                "mimeType": "text/markdown",
            }
    return list(links.values())


def _tool_text(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    text = {"type": "text", "text": json.dumps(payload, indent=2, default=str)}
    return {"content": [text, *_links(payload)], "isError": is_error}


def handle_request(request: dict[str, Any], caller: str | None = None) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC request. Returns None for notifications.

    `caller` is an opaque caller id for usage events (never a token).
    """
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "expected a JSON-RPC 2.0 request object")

    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    is_notification = "id" not in request

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": negotiate_protocol_version(
                    params.get("protocolVersion") if isinstance(params, dict) else None
                ),
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                    "resources": {"listChanged": False, "subscribe": False},
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": current_instructions(),
            },
        )

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": tools_list()})

    if method == "prompts/list":
        return _result(request_id, {"prompts": mcp_content.list_prompts()})

    if method == "prompts/get":
        try:
            return _result(
                request_id,
                mcp_content.get_prompt(params.get("name"), params.get("arguments")),
            )
        except mcp_content.PromptError as exc:
            return _error(request_id, INVALID_PARAMS, str(exc))

    if method == "resources/list":
        try:
            return _result(request_id, {"resources": list_resources()})
        except KnowledgeBaseError as exc:
            return _error(request_id, INTERNAL_ERROR, str(exc))

    if method == "resources/templates/list":
        return _result(request_id, {"resourceTemplates": []})

    if method == "resources/read":
        try:
            return _result(request_id, read_resource(str(params.get("uri", ""))))
        except KnowledgeBaseError as exc:
            return _error(request_id, INVALID_PARAMS, str(exc))

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = _handler(name)

        if handler is None:
            return _error(request_id, INVALID_PARAMS, f"unknown tool: {name}")
        if not isinstance(arguments, dict):
            return _error(request_id, INVALID_PARAMS, "arguments must be an object")
        # The caller comes from the transport, never from the client's arguments.
        call_args = {k: v for k, v in arguments.items() if not k.startswith("_")}
        if handler is tool_propose_change:
            call_args["_caller"] = caller

        started = time.monotonic()
        payload: dict[str, Any] | None = None
        error: str | None = None
        try:
            payload = handler(**call_args)
            response = _result(request_id, _tool_text(_stamp(payload)))
        except KnowledgeBaseError as exc:
            error = str(exc)
            response = _result(request_id, _tool_text({"error": error}, is_error=True))
        except TypeError as exc:
            error = f"invalid arguments: {exc}"
            response = _error(request_id, INVALID_PARAMS, f"invalid arguments for {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - report unexpected failures to the client
            error = f"{type(exc).__name__}: {exc}"
            response = _result(request_id, _tool_text({"error": error}, is_error=True))

        usage_log.record(str(name), arguments, payload, request_id, started, caller, error)
        return response

    if is_notification:
        return None

    return _error(request_id, METHOD_NOT_FOUND, f"unsupported method: {method}")


def main() -> None:
    """Local stdio transport, for running the server from a desktop MCP client."""
    import sys

    usage_log.use_stream(sys.stderr)  # stdout carries JSON-RPC here
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response: dict[str, Any] | None = _error(None, PARSE_ERROR, "invalid JSON")
        else:
            response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
