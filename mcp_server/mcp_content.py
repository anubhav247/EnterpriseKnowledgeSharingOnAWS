"""Domain-owned MCP content: server instructions and prompts.

Domain teams write `mcp/instructions.md` and `mcp/prompts/*.md` in their content
repository. The sync pipeline validates them and publishes one compiled object,
`mcp/<source>/published.json`, to the docs bucket. This module reads that object.

Nothing here parses markdown: the pipeline is the single parser, so the server,
the gateway publisher and the validator can never disagree about a file.

Published shape:

    {
      "version": "3f9a1c02be",            # hash of the instructions text
      "published_at": "2026-11-18T20:00:00+00:00",
      "instructions": "...",             # final text, version line included
      "prompts": [
        {"name": "...", "description": "...", "template": "... {{arg}} ...",
         "arguments": [{"name": "arg", "description": "...", "required": true}]}
      ],
      "content_checks": [                 # what the team has not added yet
        {"check": "no_instructions", "message": "..."}
      ]
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

log = logging.getLogger("mcp-content")

DOCS_BUCKET = os.environ.get("DOCS_BUCKET", "")
PUBLISHED_KEY = os.environ.get("MCP_PUBLISHED_KEY", "")

# Prompt and instruction edits should show up within a minute, without a GET on
# every request.
CACHE_SECONDS = float(os.environ.get("MCP_CONTENT_CACHE_SECONDS", "30"))

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)\s*\}\}")

_lock = threading.Lock()
_cached: dict[str, Any] | None = None
_cached_at = 0.0


class PromptError(ValueError):
    """Raised for an unknown prompt or missing required arguments."""


def _fetch() -> dict[str, Any]:
    if not (DOCS_BUCKET and PUBLISHED_KEY):
        return {}
    from kb_client import _s3  # shared, retried client

    try:
        obj = _s3().get_object(Bucket=DOCS_BUCKET, Key=PUBLISHED_KEY)
    except Exception as exc:  # noqa: BLE001 - missing content must not break tools
        if "NoSuchKey" not in str(exc):
            log.warning("could not read %s: %s", PUBLISHED_KEY, exc)
        return {}
    try:
        data = json.loads(obj["Body"].read())
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("ignoring malformed %s: %s", PUBLISHED_KEY, exc)
        return {}
    return data if isinstance(data, dict) else {}


def published() -> dict[str, Any]:
    """The current published content, cached for CACHE_SECONDS. {} if none."""
    global _cached, _cached_at
    with _lock:
        if _cached is None or time.monotonic() - _cached_at > CACHE_SECONDS:
            _cached = _fetch()
            _cached_at = time.monotonic()
        return _cached


def reset_cache() -> None:
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = None, 0.0


def instructions() -> str:
    return str(published().get("instructions") or "")


def about() -> tuple[str, str]:
    """(owner, summary) from the team's instructions.md; empty when not published."""
    data = published()
    return str(data.get("owner") or ""), str(data.get("summary") or "")


def content_checks() -> list[dict[str, str]]:
    """The sync's warnings (sync/publish_mcp.py content_checks); [] when none."""
    found = published().get("content_checks") or []
    return [
        {"check": str(c.get("check", "")), "message": str(c.get("message", ""))}
        for c in found
        if isinstance(c, dict)
    ]


def version_stamp() -> dict[str, str]:
    """Fields added to every tool result so an agent can spot stale instructions."""
    data = published()
    if not data.get("version"):
        return {}
    return {
        "instructions_version": str(data["version"]),
        "instructions_published_at": str(data.get("published_at", "")),
    }


def _prompts() -> list[dict[str, Any]]:
    prompts = published().get("prompts") or []
    return [p for p in prompts if isinstance(p, dict) and p.get("name")]


def list_prompts() -> list[dict[str, Any]]:
    return [
        {
            "name": p["name"],
            "description": p.get("description", ""),
            "arguments": [
                {
                    "name": a["name"],
                    "description": a.get("description", ""),
                    "required": bool(a.get("required")),
                }
                for a in p.get("arguments", [])
            ],
        }
        for p in _prompts()
    ]


def get_prompt(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    arguments = arguments or {}
    prompt = next((p for p in _prompts() if p["name"] == name), None)
    if prompt is None:
        raise PromptError(f"unknown prompt: {name}")
    missing = [
        a["name"]
        for a in prompt.get("arguments", [])
        if a.get("required") and not str(arguments.get(a["name"], "")).strip()
    ]
    if missing:
        raise PromptError(f"prompt '{name}' needs argument(s): {', '.join(missing)}")
    text = _PLACEHOLDER.sub(lambda m: str(arguments.get(m.group(1), "")), prompt["template"])
    return {
        "description": prompt.get("description", ""),
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }
