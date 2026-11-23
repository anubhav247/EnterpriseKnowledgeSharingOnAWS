"""Retrieval engines: what answers search_knowledge_base and retrieve_document.

One interface, used by server.py:

    search(query, k, document_id=None)  ranked Passage list (kb_client.Passage)
    retrieve(doc_id)                     one document, the retrieve_document shape
    list_documents()                     the list_documents shape
    sync_status()                        the get_sync_status shape

Engines, chosen by the RETRIEVAL_ENGINE environment variable:

    bedrock-kb (default)  Bedrock Knowledge Base + S3 docs bucket (kb_client.py)
    stub                  in-memory word-overlap search over a local folder
                          (STUB_DOCS_DIR); for tests and a local demo, no AWS

The tool names and their input and output shapes do not depend on the engine.
"""

from __future__ import annotations

import os
import pathlib
import re
from typing import Any, Protocol

import kb_client
from kb_client import KnowledgeBaseError, Passage


class RetrievalEngine(Protocol):
    name: str

    def search(self, query: str, k: int, document_id: str | None = None) -> list[Passage]: ...

    def retrieve(self, doc_id: str) -> dict[str, Any]: ...

    def list_documents(self) -> list[dict[str, Any]]: ...

    def sync_status(self) -> dict[str, Any]: ...


class BedrockKbEngine:
    """The current retrieval code (kb_client.py) behind the interface."""

    name = "bedrock-kb"

    def search(self, query: str, k: int, document_id: str | None = None) -> list[Passage]:
        return kb_client.search(query, max_results=k, document_id=document_id)

    def retrieve(self, doc_id: str) -> dict[str, Any]:
        return kb_client.get_document(doc_id)

    def list_documents(self) -> list[dict[str, Any]]:
        return kb_client.list_documents()

    def sync_status(self) -> dict[str, Any]:
        return kb_client.sync_status()


_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2}


class StubEngine:
    """Documents held in memory; score = share of query words in the document.

    shortcut: whole documents, word overlap only; good for tests and demos,
    not for real ranking.
    """

    name = "stub"
    URI_ROOT = "s3://stub/"

    def __init__(self, docs: dict[str, str]) -> None:
        self.docs = dict(sorted(docs.items()))

    @classmethod
    def from_folder(cls, folder: pathlib.Path | str, source_id: str = "") -> StubEngine:
        root = pathlib.Path(folder)
        prefix = f"{source_id}/" if source_id else ""
        return cls({f"{prefix}{p.name}": p.read_text(encoding="utf-8") for p in root.rglob("*.md")})

    def _uri(self, doc_id: str) -> str:
        return f"{self.URI_ROOT}{kb_client.DOCS_PREFIX}{doc_id}"

    def search(self, query: str, k: int, document_id: str | None = None) -> list[Passage]:
        wanted = _words(query)
        if not wanted:
            raise KnowledgeBaseError("query must be a non-empty string")
        passages = []
        for doc_id, text in self.docs.items():
            if document_id and doc_id.removesuffix(".md") != document_id.removesuffix(".md"):
                continue
            score = len(wanted & _words(text)) / len(wanted)
            if score > 0:
                passages.append(Passage(text=text, score=score, source_uri=self._uri(doc_id)))
        passages.sort(key=lambda p: p.score, reverse=True)
        return passages[: max(1, min(k, 100))]

    def retrieve(self, doc_id: str) -> dict[str, Any]:
        key = doc_id if doc_id.endswith(".md") else f"{doc_id}.md"
        if key not in self.docs:
            raise KnowledgeBaseError(
                f"document '{doc_id}' not found. Available: {', '.join(self.docs)}"
            )
        metadata, body = kb_client._split_front_matter(self.docs[key])
        return {
            "document_id": key,
            "s3_uri": self._uri(key),
            "metadata": metadata,
            "content": body,
            "size_bytes": len(self.docs[key].encode()),
            "last_modified": "1970-01-01T00:00:00+00:00",
            "truncated": False,
        }

    def list_documents(self) -> list[dict[str, Any]]:
        return [
            {
                "document_id": d,
                "s3_uri": self._uri(d),
                "size_bytes": len(t.encode()),
                "last_modified": "1970-01-01T00:00:00+00:00",
            }
            for d, t in self.docs.items()
        ]

    def sync_status(self) -> dict[str, Any]:
        return {"knowledge_base_id": "stub", "data_sources": []}


def from_env() -> RetrievalEngine:
    name = os.environ.get("RETRIEVAL_ENGINE", "bedrock-kb")
    if name == "bedrock-kb":
        return BedrockKbEngine()
    if name == "stub":
        folder = os.environ.get("STUB_DOCS_DIR", "")
        if not folder:
            raise KnowledgeBaseError("RETRIEVAL_ENGINE=stub needs STUB_DOCS_DIR")
        return StubEngine.from_folder(folder, kb_client.SOURCE_SCOPE)
    raise KnowledgeBaseError(f"RETRIEVAL_ENGINE must be bedrock-kb or stub, not {name!r}")
