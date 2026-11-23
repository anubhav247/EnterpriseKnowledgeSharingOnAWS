"""Bedrock Knowledge Base retrieval layer.

This module retrieves and returns source material. It deliberately does no
reasoning and invokes no language model: the calling agent is the reasoning
layer, and pre-summarising results here would both hide the source material and
impose this server's interpretation on every caller.

The retrieval model is search-then-fetch:

  search_knowledge_base  ->  ranked chunks with document ids, like search results
  retrieve_document      ->  one full document, like opening a page

The agent decides which results are worth expanding.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-1")
KB_ID = os.environ.get("KB_ID", "")
DOCS_BUCKET = os.environ.get("DOCS_BUCKET", "")
DOCS_PREFIX = os.environ.get("DOCS_PREFIX", "knowledge-base/")

# Optional source namespace this server is locked to. One Bedrock knowledge base
# can hold several content repositories, each under its own namespace
# (knowledge-base/<source>/ and chunks/<source>/). Setting SOURCE_SCOPE makes a
# server behave like a specialist over exactly one repository: search only
# returns that namespace's chunks, listing only shows its documents, and an
# unqualified document id is resolved inside the namespace. Leaving it empty
# makes the server a generalist that spans every source in the knowledge base.
SOURCE_SCOPE = os.environ.get("SOURCE_SCOPE", "").strip().strip("/")

_BOTO_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"}, read_timeout=120)

# Guard against a caller pulling an unbounded document into its context.
MAX_DOCUMENT_CHARS = 200_000


class KnowledgeBaseError(RuntimeError):
    """Raised when the knowledge base cannot serve a request."""


@lru_cache(maxsize=1)
def _agent_runtime():
    return boto3.client("bedrock-agent-runtime", region_name=REGION, config=_BOTO_CONFIG)


@lru_cache(maxsize=1)
def _s3():
    return boto3.client("s3", region_name=REGION, config=_BOTO_CONFIG)


def _require_kb_id() -> str:
    if not KB_ID:
        raise KnowledgeBaseError(
            "KB_ID is not configured. Set the KB_ID environment variable to the "
            "Bedrock knowledge base identifier."
        )
    return KB_ID


def _require_bucket() -> str:
    if not DOCS_BUCKET:
        raise KnowledgeBaseError("DOCS_BUCKET is not configured")
    return DOCS_BUCKET


# ------------------------------------------------------------------ page links

# Written by each sync beside the source's documents (sync/sync_chunks.py):
# object key -> page URL. Not ingested, so the sync rewrites it freely.
LINKS_FILE = "_source-links.json"
LINKS_TTL_SECONDS = 60  # a sync's new line ranges show within a minute
_links_cache: dict[str, tuple[float, dict[str, str]]] = {}


def source_links(document_id: str) -> dict[str, str]:
    """The links map of the source a document id belongs to; {} when none."""
    namespace = document_id.split("/", 1)[0] if "/" in document_id else ""
    key = f"{DOCS_PREFIX}{namespace}/{LINKS_FILE}" if namespace else f"{DOCS_PREFIX}{LINKS_FILE}"
    now = time.monotonic()
    cached = _links_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    links: dict[str, str] = {}
    try:
        body = _s3().get_object(Bucket=_require_bucket(), Key=key)["Body"].read()
        links = {k: v for k, v in json.loads(body).items() if str(v).startswith("https://")}
    except Exception:  # noqa: BLE001, S110 - no links file (local source) or unreadable: no links
        pass
    _links_cache[key] = (now + LINKS_TTL_SECONDS, links)
    return links


# ------------------------------------------------------------------ identifiers


def document_id_from_uri(uri: str) -> str:
    """Stable document id derived from the S3 key.

    The id is what an agent passes back to retrieve_document, so it must be
    stable across syncs and safe to echo in text. Chunk objects live under a
    per-document folder, so their parent folder names the document.
    """
    if not uri.startswith("s3://"):
        return uri.rsplit("/", 1)[-1]

    key = uri[5:].partition("/")[2]

    # Chunk objects, either layout:
    #   chunks/<doc-stem>/<nnn>.md              single source
    #   chunks/<source>/<doc-stem>/<nnn>.md     namespaced source
    if key.startswith("chunks/"):
        parts = key[len("chunks/"):].split("/")
        if len(parts) >= 3:
            # Keep the source prefix so ids stay unique across repositories.
            return f"{parts[0]}/{parts[1]}.md"
        if len(parts) == 2:
            return f"{parts[0]}.md"
        return parts[-1]

    # Whole documents: knowledge-base/[<source>/]<doc>.md
    docs_root = DOCS_PREFIX.rstrip("/")
    if docs_root and key.startswith(f"{docs_root}/"):
        remainder = key[len(docs_root) + 1:]
        # A nested path means a namespaced source; keep it in the id.
        return remainder if "/" in remainder else remainder.rsplit("/", 1)[-1]

    return key.rsplit("/", 1)[-1]


_FRONT_MATTER_BLOCK = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Separate YAML front matter into fields, returning (metadata, body)."""
    match = _FRONT_MATTER_BLOCK.match(text)
    if not match:
        return {}, text

    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        metadata[key.strip()] = value.strip()

    return metadata, text[match.end():].lstrip("\n")


def _in_scope(document_id: str) -> bool:
    """Whether a document id belongs to this server's source namespace.

    A generalist server (no SOURCE_SCOPE) admits everything. A specialist admits
    only ids under its namespace, matched on the leading path segment.
    """
    if not SOURCE_SCOPE:
        return True
    return document_id.lower().startswith(f"{SOURCE_SCOPE.lower()}/")


def _apply_scope(document_id: str) -> str:
    """Qualify a bare document id with the server's namespace.

    On a specialist server an agent may pass ``data-dictionary.md`` without the
    ``analytics/`` prefix; treat that as living inside the scoped namespace so
    the id round-trips against the namespaced S3 layout.
    """
    if SOURCE_SCOPE and "/" not in document_id.strip().lstrip("/"):
        return f"{SOURCE_SCOPE}/{document_id.strip().lstrip('/')}"
    return document_id


def _key_for_document_id(document_id: str) -> str:
    """Resolve a document id back to its source object key.

    An id may carry a source namespace (``analytics/data-dictionary.md``) when
    several repositories feed the knowledge base, so one path segment is allowed.
    """
    clean = _apply_scope(document_id).strip().lstrip("/")
    if ".." in clean or clean.startswith("s3://") or not clean.isprintable():
        raise KnowledgeBaseError(f"invalid document id: {document_id}")
    if clean.count("/") > 1:
        raise KnowledgeBaseError(
            f"document id may contain at most one path segment: {document_id}"
        )
    if not clean.endswith(".md"):
        clean = f"{clean}.md"
    return f"{DOCS_PREFIX}{clean}"


# ---------------------------------------------------------------------- results


@dataclass
class Passage:
    """One retrieved chunk, presented as a search result."""

    text: str
    score: float
    source_uri: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def document_id(self) -> str:
        return document_id_from_uri(self.source_uri)

    @property
    def s3_uri(self) -> str:
        return self.source_uri

    @property
    def s3_bucket(self) -> str:
        if self.source_uri.startswith("s3://"):
            return self.source_uri[5:].split("/", 1)[0]
        return DOCS_BUCKET

    @property
    def s3_key(self) -> str:
        if self.source_uri.startswith("s3://"):
            return self.source_uri[5:].partition("/")[2]
        return self.document_id

    @property
    def section(self) -> str:
        """Heading trail, read from the chunk marker written at sync time."""
        match = re.search(
            r"<!--\s*source:.*?\|\s*section:\s*(.*?)\s*(?:\|\s*url:\s*\S+\s*)?-->", self.text
        )
        if match:
            return match.group(1)

        # No chunk marker: fall back to the first real heading in the content.
        heading = re.search(r"^#{1,6}\s+(.+)$", self.snippet_text, re.MULTILINE)
        if heading:
            return heading.group(1).strip()

        # Last resort: the document's own title from front matter.
        title = re.search(r"^title:\s*(.+)$", self.text, re.MULTILINE)
        return title.group(1).strip() if title else ""

    @property
    def source_url(self) -> str:
        """Page URL of the chunk's lines, from the source's links file."""
        if self.s3_bucket != DOCS_BUCKET or not DOCS_BUCKET:
            return ""  # the stub engine, or a chunk from another bucket
        return source_links(self.document_id).get(self.s3_key, "")

    @property
    def snippet_text(self) -> str:
        """Chunk text without the provenance marker or YAML front matter.

        Front matter is metadata, not content. Leaving it in a snippet wastes
        the whole preview on 'title:' and 'tags:' lines and gives the agent
        nothing to judge relevance by.
        """
        text = re.sub(r"<!--\s*source:.*?-->\s*", "", self.text).strip()
        text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.DOTALL)
        # Bedrock may flatten a document, collapsing front matter onto one line.
        text = re.sub(r"\A---\s+(?:\w[\w -]*:.*?)\s+---\s+", "", text)
        return text.strip()

    def snippet(self, max_chars: int = 600) -> str:
        text = self.snippet_text
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rsplit(" ", 1)[0] + " ..."

    def as_result(self, snippet_chars: int = 600) -> dict[str, Any]:
        """Search-result shape: enough to judge relevance, not the whole document."""
        result = {
            "document_id": self.document_id,
            "section": self.section,
            "snippet": self.snippet(snippet_chars),
            "relevance_score": round(self.score, 4),
            "s3_uri": self.s3_uri,
        }
        if self.source_url:  # none for a local folder source
            result["source_url"] = self.source_url
        return result


def _passage_from_result(result: dict[str, Any]) -> Passage:
    location = result.get("location", {}) or {}
    uri = (location.get("s3Location", {}) or {}).get("uri", "")
    return Passage(
        text=(result.get("content", {}) or {}).get("text", ""),
        score=float(result.get("score") or 0.0),
        source_uri=uri,
        metadata=result.get("metadata", {}) or {},
    )


# --------------------------------------------------------------------- retrieval


def search(
    query: str, max_results: int = 10, document_id: str | None = None
) -> list[Passage]:
    """Rank chunks against a query. Returns candidates, not conclusions."""
    kb_id = _require_kb_id()
    if not query or not query.strip():
        raise KnowledgeBaseError("query must be a non-empty string")

    limit = max(1, min(max_results, 100))
    # Over-fetch when a filter will discard some hits, so the filter still has
    # depth: narrowing to one document, or scoping to one source namespace.
    narrowing = bool(document_id) or bool(SOURCE_SCOPE)
    fetch = min(limit * 4, 100) if narrowing else limit

    try:
        response = _agent_runtime().retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query.strip()},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": fetch}
            },
        )
    except Exception as exc:
        raise KnowledgeBaseError(f"knowledge base retrieval failed: {exc}") from exc

    passages = [_passage_from_result(r) for r in response.get("retrievalResults", [])]

    # A specialist server sees only its own repository, even though the retrieve
    # call spans the whole knowledge base.
    if SOURCE_SCOPE:
        passages = [p for p in passages if _in_scope(p.document_id)]

    if document_id:
        wanted = _apply_scope(document_id).strip().lower().removesuffix(".md")
        passages = [
            p for p in passages if p.document_id.lower().removesuffix(".md") == wanted
        ]

    return passages[:limit]


def get_document(document_id: str) -> dict[str, Any]:
    """Fetch one complete document, so the agent can read past the snippets."""
    bucket = _require_bucket()
    key = _key_for_document_id(document_id)

    # A specialist server must not read outside its namespace, even if an agent
    # passes a fully-qualified id from another source.
    qualified = _apply_scope(document_id)
    if SOURCE_SCOPE and not _in_scope(qualified):
        raise KnowledgeBaseError(
            f"document '{document_id}' is outside this server's source "
            f"'{SOURCE_SCOPE}'. This server only serves documents under "
            f"'{SOURCE_SCOPE}/'."
        )

    try:
        obj = _s3().get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if "NoSuchKey" in str(exc) or "404" in str(exc):
            available = [d["document_id"] for d in list_documents()]
            raise KnowledgeBaseError(
                f"document '{document_id}' not found. Available: {', '.join(available)}"
            ) from exc
        raise KnowledgeBaseError(f"could not read document '{document_id}': {exc}") from exc

    raw = obj["Body"].read().decode("utf-8", errors="replace")

    # Front matter is metadata, not answerable content. Returning it spends the
    # caller's context on 'owner:' and 'tags:' lines, so it is surfaced as
    # structured fields instead of left inline.
    metadata, body = _split_front_matter(raw)

    truncated = len(body) > MAX_DOCUMENT_CHARS
    content = body[:MAX_DOCUMENT_CHARS] if truncated else body

    result = {
        "document_id": document_id if document_id.endswith(".md") else f"{document_id}.md",
        "s3_uri": f"s3://{bucket}/{key}",
        "metadata": metadata,
        "content": content,
        "size_bytes": obj["ContentLength"],
        "last_modified": obj["LastModified"].isoformat(),
        "truncated": truncated,
    }
    # Written by the sync (sync/sync_chunks.py); none for a local folder source.
    url = source_links(_apply_scope(document_id)).get(key, "")
    if url:
        result["source_url"] = url
    return result


def list_documents() -> list[dict[str, Any]]:
    """Inventory of source documents available to retrieve."""
    bucket = _require_bucket()
    paginator = _s3().get_paginator("list_objects_v2")
    docs: list[dict[str, Any]] = []

    # A specialist lists only its own namespace by narrowing the S3 prefix, so
    # it never even sees other repositories' objects.
    prefix = f"{DOCS_PREFIX}{SOURCE_SCOPE}/" if SOURCE_SCOPE else DOCS_PREFIX

    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.lower().endswith(".md"):
                    continue
                docs.append(
                    {
                        "document_id": key[len(DOCS_PREFIX):],
                        "s3_uri": f"s3://{bucket}/{key}",
                        "size_bytes": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    }
                )
    except Exception as exc:
        raise KnowledgeBaseError(f"could not list documents: {exc}") from exc

    return sorted(docs, key=lambda d: d["document_id"])


def sync_status() -> dict[str, Any]:
    """Freshness of the last ingestion, so an agent can flag stale answers."""
    kb_id = _require_kb_id()
    agent = boto3.client("bedrock-agent", region_name=REGION, config=_BOTO_CONFIG)

    try:
        sources = agent.list_data_sources(knowledgeBaseId=kb_id).get(
            "dataSourceSummaries", []
        )
        results = []
        for src in sources:
            jobs = agent.list_ingestion_jobs(
                knowledgeBaseId=kb_id,
                dataSourceId=src["dataSourceId"],
                sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
                maxResults=1,
            ).get("ingestionJobSummaries", [])
            latest = jobs[0] if jobs else None
            results.append(
                {
                    "data_source": src.get("name"),
                    "status": src.get("status"),
                    "last_ingestion_status": latest.get("status") if latest else None,
                    "last_ingestion_started": (
                        latest["startedAt"].isoformat()
                        if latest and latest.get("startedAt")
                        else None
                    ),
                    "documents_indexed": (
                        (latest.get("statistics") or {}).get("numberOfDocumentsScanned")
                        if latest
                        else None
                    ),
                }
            )
        return {"knowledge_base_id": kb_id, "data_sources": results}
    except Exception as exc:
        raise KnowledgeBaseError(f"could not read sync status: {exc}") from exc
