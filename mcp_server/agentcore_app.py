"""Streamable HTTP MCP server for Bedrock AgentCore Runtime.

AgentCore Runtime with serverProtocol=MCP expects an MCP server listening on
0.0.0.0:8080 at the /mcp path, speaking Streamable HTTP: JSON-RPC over POST,
with responses returned either as JSON or as an SSE stream depending on the
client's Accept header.

Implemented on the stdlib http.server so the container needs no web framework,
which keeps the image small and the dependency surface minimal.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from server import PARSE_ERROR, SERVER_NAME, SERVER_VERSION, _error, handle_request

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("agentcore-mcp")

# AgentCore Runtime with serverProtocol=MCP routes to 0.0.0.0:8000/mcp.
# (Port 8080 is the HTTP protocol contract, not MCP.)
PORT = int(os.environ.get("PORT", "8000"))
MCP_PATH = "/mcp"
MAX_BODY_BYTES = 2 * 1024 * 1024


# Opaque caller ids for usage events, first match wins. Never Authorization.
# The stack allowlists the caller header on the runtime (requestHeaderAllowlist).
# shortcut: the gateway does not set it yet, so gateway calls log the session
# id; direct SigV4 callers (the smoke test) send it. Upgrade: issue #15 item 2.
CALLER_HEADERS = (
    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Caller-Id",
    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id",
)


def _caller_id(headers, session_id: str) -> str:
    for name in CALLER_HEADERS:
        value = headers.get(name)
        if value:
            return value
    return session_id


class McpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    # Log every request so platform health probes are visible in CloudWatch.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("http %s", fmt % args)

    # ------------------------------------------------------------------ helpers

    def _send(self, status: int, payload: object, extra: dict[str, str] | None = None) -> None:
        body = (
            payload.encode("utf-8")
            if isinstance(payload, str)
            else json.dumps(payload, default=str).encode("utf-8")
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, payload: object, session_id: str) -> None:
        """Return a single JSON-RPC response framed as one SSE event."""
        data = json.dumps(payload, default=str)
        body = f"event: message\ndata: {data}\n\n".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("mcp-session-id", session_id)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _no_content(self, status: int, session_id: str) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("mcp-session-id", session_id)
        self.end_headers()

    # -------------------------------------------------------------------- verbs

    def do_GET(self) -> None:  # noqa: N802
        # Health probe. AgentCore checks the container is live before routing.
        if self.path.split("?", 1)[0] in ("/ping", "/health", "/"):
            self._send(200, {"status": "healthy", "server": SERVER_NAME})
            return
        # A GET on /mcp would open a server-initiated SSE stream; this server
        # only ever responds to client requests, so decline politely.
        self._send(405, {"error": "server-initiated streams are not supported"})

    def do_DELETE(self) -> None:  # noqa: N802
        # Session teardown. Sessions hold no state here, so accept and move on.
        if self.path.split("?", 1)[0] == MCP_PATH:
            self._no_content(204, self.headers.get("mcp-session-id") or str(uuid.uuid4()))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in (MCP_PATH, "/"):
            self._send(404, {"error": f"unknown path {path}"})
            return

        session_id = self.headers.get("mcp-session-id") or str(uuid.uuid4())

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, _error(None, PARSE_ERROR, "invalid Content-Length"))
            return

        if length > MAX_BODY_BYTES:
            self._send(413, _error(None, PARSE_ERROR, "request body too large"))
            return

        raw = self.rfile.read(length) if length else b""
        if not raw:
            self._send(400, _error(None, PARSE_ERROR, "empty request body"))
            return

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, _error(None, PARSE_ERROR, "invalid JSON"))
            return

        # SSE only when the client asked for it and cannot take plain JSON.
        accept = (self.headers.get("Accept") or "").lower()
        wants_sse = "text/event-stream" in accept and "application/json" not in accept

        caller = _caller_id(self.headers, session_id)
        if isinstance(payload, list):
            responses = [
                r for r in (handle_request(item, caller) for item in payload) if r is not None
            ]
            if not responses:
                self._no_content(202, session_id)
                return
            result: object = responses
        else:
            method = payload.get("method") if isinstance(payload, dict) else None
            single = handle_request(payload, caller)
            if single is None:
                log.info("notification %s acknowledged", method)
                self._no_content(202, session_id)
                return
            log.info("handled %s", method)
            result = single

        if wants_sse:
            self._send_sse(result, session_id)
        else:
            self._send(200, result, {"mcp-session-id": session_id})

    # Surface handler crashes in the log instead of dropping the connection,
    # which the platform would otherwise report only as a generic 502.
    def handle_one_request(self) -> None:  # noqa: D102
        try:
            super().handle_one_request()
        except Exception:
            log.exception("unhandled error while serving %s", self.path)
            raise


class Server(ThreadingHTTPServer):
    # ThreadingHTTPServer already mixes in ThreadingMixIn.
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    log.info(
        "starting %s v%s on 0.0.0.0:%s%s (kb=%s)",
        SERVER_NAME,
        SERVER_VERSION,
        PORT,
        MCP_PATH,
        os.environ.get("KB_ID", "<unset>"),
    )
    with Server(("0.0.0.0", PORT), McpHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.info("shutting down")


if __name__ == "__main__":
    main()
