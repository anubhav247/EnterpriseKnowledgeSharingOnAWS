#!/usr/bin/env python3
"""Sync structure-aware chunks to S3 with section-level change detection.

Where sync_kb.py uploads whole documents and lets Bedrock slice them into token
windows, this uploads one object per structural chunk and uses chunkingStrategy
NONE so Bedrock embeds each object as-is.

That granularity is what makes section-level re-embedding possible: Bedrock
tracks change at the object level, so a section must be its own object for an
edit to it to avoid re-embedding its siblings.

A manifest of chunk hashes is kept alongside the chunks so each sync knows
exactly which sections changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import time

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import source_links

import ast_chunker as ast
from content_sources import ContentSource, add_arguments, as_source, source_from_parsed

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent


def _load_config() -> dict:
    """Find config.json whether this runs from the code repo or vendored into CI.

    In the application repository the file sits at the repo root. When these
    scripts are vendored into the content repository they live beside it, so both
    locations are checked rather than assuming a layout.
    """
    for candidate in (ROOT / "config.json", HERE / "config.json"):
        if candidate.is_file():
            return json.loads(candidate.read_text())
    # No file: callers (infra/load_content.py, tests) set the targets themselves.
    return {
        "region": os.environ.get("AWS_REGION", "us-east-1"),
        "docs_bucket": os.environ.get("DOCS_BUCKET", ""),
        "docs_prefix": "knowledge-base/",
        "chunk_prefix": "chunks/",
    }


CFG = _load_config()

CHUNK_PREFIX = CFG.get("chunk_prefix", "chunks/")
MANIFEST_KEY = CFG.get("manifest_key", "manifests/chunk-manifest.json")
SOURCE_ID = ""


def docs_prefix() -> str:
    base = CFG["docs_prefix"]
    return f"{base}{SOURCE_ID}/" if SOURCE_ID else base


def chunk_prefix() -> str:
    return f"{CHUNK_PREFIX}{SOURCE_ID}/" if SOURCE_ID else CHUNK_PREFIX


def manifest_key() -> str:
    if not SOURCE_ID:
        return MANIFEST_KEY
    directory, _, filename = MANIFEST_KEY.rpartition("/")
    return f"{directory}/{SOURCE_ID}-{filename}" if directory else f"{SOURCE_ID}-{filename}"


def log(msg: str) -> None:
    print(f"[chunk-sync] {msg}", flush=True)


def make_session() -> boto3.Session:
    region = CFG["region"]
    if os.environ.get("AWS_PROFILE"):
        return boto3.Session(region_name=region)
    default = boto3.Session(region_name=region)
    if default.get_credentials() is not None:
        return default
    profile = CFG.get("profile")
    if profile and profile in default.available_profiles:
        return boto3.Session(profile_name=profile, region_name=region)
    raise SystemExit("no AWS credentials available")


def chunk_key(doc_name: str, index: int, chunk: ast.Chunk) -> str:
    """S3 key for one chunk. Stable across runs so edits overwrite in place."""
    stem = doc_name[:-3] if doc_name.endswith(".md") else doc_name
    suffix = f"-p{chunk.part}" if chunk.of_parts > 1 else ""
    return f"{chunk_prefix()}{stem}/{index:03d}{suffix}.md"


def load_manifest(s3, bucket: str) -> dict:
    try:
        body = s3.get_object(Bucket=bucket, Key=manifest_key())["Body"].read()
        return json.loads(body)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            log("no previous manifest; treating every chunk as new")
            return {}
        raise


def save_manifest(s3, bucket: str, manifest: dict) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=manifest_key(),
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def links_key() -> str:
    """Page links of this source: object key -> source_url (mcp_server/kb_client.py).

    It sits beside the whole documents, which Bedrock does not ingest, so
    rewriting it never re-embeds anything. Readers of the prefix take .md only.
    """
    return f"{docs_prefix()}{LINKS_FILE}"


LINKS_FILE = "_source-links.json"


def build_state(docs: ContentSource | pathlib.Path) -> tuple[dict, dict, dict]:
    """Return (manifest, chunk key -> text, source key -> whole document text)."""
    manifest, bodies, sources, _ = build_state_and_links(docs, None)
    return manifest, bodies, sources


def build_state_and_links(
    docs: ContentSource | pathlib.Path, link: dict | None
) -> tuple[dict, dict, dict, dict]:
    """build_state, plus object key -> page URL when there is a link.

    The manifest hash is the section text only. Line ranges and URLs go in the
    links map, so a line added above a section moves its link without
    embedding it again.

    Both prefixes are produced together. search_knowledge_base is backed by the
    chunk objects, but retrieve_document reads the whole document under
    DOCS_PREFIX, so writing only chunks would let the two diverge: search would
    quote new content while retrieval served the old file.
    """
    manifest: dict[str, dict[str, str]] = {}
    bodies: dict[str, str] = {}
    sources: dict[str, str] = {}
    links: dict[str, str] = {}

    source = as_source(docs)
    for doc in source.list_documents():
        text = source.fetch(doc)
        name = doc.rsplit("/", 1)[-1]  # documents are keyed by file name
        sources[f"{docs_prefix()}{name}"] = text
        if link:
            links[f"{docs_prefix()}{name}"] = source_links.url(link, doc)

        chunks = ast.chunk_document(text)
        spans = source_links.line_spans(text, [c.text for c in chunks]) if link else []
        doc_entry: dict[str, str] = {}
        for index, chunk in enumerate(chunks):
            key = chunk_key(name, index, chunk)
            doc_entry[key] = chunk.content_hash
            if link:
                links[key] = source_links.url(link, doc, *(spans[index] or (None, None)))
            # Prepend the heading trail so a chunk retrieved alone still says
            # which document and section it came from.
            bodies[key] = (
                f"<!-- source: {name} | section: {chunk.section_path} -->\n\n{chunk.text}\n"
            )
        manifest[name] = doc_entry

    return manifest, bodies, sources, links


def save_links(s3, bucket: str, links: dict[str, str]) -> None:
    """Write the links file, or remove it when the source has no links."""
    if not links:
        s3.delete_object(Bucket=bucket, Key=links_key())  # no error when absent
        return
    s3.put_object(
        Bucket=bucket,
        Key=links_key(),
        Body=json.dumps(links, indent=1, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )


def sync_source_documents(s3, bucket: str, sources: dict[str, str]) -> dict[str, int]:
    """Keep DOCS_PREFIX in step with the chunks.

    retrieve_document reads whole documents from this prefix. If it is not updated
    alongside the chunks, search returns new content while retrieval returns the
    old file, which is worse than either being stale on its own.
    """
    prefix = docs_prefix()
    uploaded = deleted = 0

    remote: dict[str, str] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".md"):
                remote[obj["Key"]] = obj["ETag"].strip('"')

    for key, text in sources.items():
        body = text.encode("utf-8")
        if remote.get(key) == hashlib.md5(body, usedforsecurity=False).hexdigest():
            continue
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="text/markdown; charset=utf-8",
        )
        uploaded += 1

    for key in remote:
        if key not in sources:
            s3.delete_object(Bucket=bucket, Key=key)
            deleted += 1

    if uploaded or deleted:
        log(f"source documents: {uploaded} uploaded, {deleted} deleted")

    return {"uploaded": uploaded, "deleted": deleted}


def sync(
    session: boto3.Session,
    docs: ContentSource | pathlib.Path,
    dry_run: bool,
    link: dict | None = None,
) -> dict:
    s3 = session.client("s3")
    bucket = CFG["docs_bucket"]

    previous = load_manifest(s3, bucket)
    current, bodies, sources, links = build_state_and_links(docs, link)
    if not current:  # a warning, never a failure (sync/publish_mcp.py content_checks)
        log("WARN content: 0 documents: the server has nothing to answer from. Add markdown files")

    prev_flat = {k: v for doc in previous.values() for k, v in doc.items()}
    cur_flat = {k: v for doc in current.values() for k, v in doc.items()}

    diff = ast.diff_manifests(prev_flat, cur_flat)
    log(f"chunks: {diff.summary()}")

    if dry_run:
        for key in diff.needs_embedding[:10]:
            log(f"  would upload {key}")
        for key in diff.removed[:10]:
            log(f"  would delete {key}")
        log(f"  would also refresh {len(sources)} source document(s) under {docs_prefix()}")
        return {
            "uploaded": 0,
            "deleted": 0,
            "unchanged": len(diff.unchanged),
            "would_upload": len(diff.needs_embedding),
            "would_delete": len(diff.removed),
        }

    for key in diff.needs_embedding:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=bodies[key].encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
    if diff.needs_embedding:
        log(f"uploaded {len(diff.needs_embedding)} changed chunk(s)")

    for key in diff.removed:
        s3.delete_object(Bucket=bucket, Key=key)
    if diff.removed:
        log(f"deleted {len(diff.removed)} stale chunk(s)")

    source_stats = sync_source_documents(s3, bucket, sources)
    # Every run: line ranges can move while no chunk changed.
    save_links(s3, bucket, links)
    save_manifest(s3, bucket, current)

    return {
        "uploaded": len(diff.needs_embedding),
        "deleted": len(diff.removed),
        "unchanged": len(diff.unchanged),
        "source_documents_uploaded": source_stats["uploaded"],
        "source_documents_deleted": source_stats["deleted"],
        "embedded_tokens": sum(ast.estimate_tokens(bodies[k]) for k in diff.needs_embedding),
        "total_tokens": sum(ast.estimate_tokens(b) for b in bodies.values()),
    }


def resolve_targets(session: boto3.Session) -> tuple[str, str]:
    """Find the knowledge base and chunk data source ids.

    outputs.json is convenient locally but is gitignored, so CI resolves by name
    instead. Names are stable configuration; ids are deployment artefacts.
    """
    # Pipelines pass the ids explicitly, so the sync role needs no
    # bedrock:ListKnowledgeBases (which cannot be scoped to one resource).
    if CFG.get("knowledge_base_id") and CFG.get("data_source_id"):
        return CFG["knowledge_base_id"], CFG["data_source_id"]

    outputs_file = ROOT / "deploy" / "outputs.json"
    if outputs_file.is_file():
        outputs = json.loads(outputs_file.read_text())
        kb_id = outputs.get("knowledge_base_id")
        ds_id = outputs.get("chunk_data_source_id") or outputs.get("data_source_id")
        if kb_id and ds_id:
            return kb_id, ds_id

    agent = session.client("bedrock-agent")

    kb_id = None
    for page in agent.get_paginator("list_knowledge_bases").paginate():
        for kb in page.get("knowledgeBaseSummaries", []):
            if kb["name"] == CFG["kb_name"]:
                kb_id = kb["knowledgeBaseId"]
                break
    if not kb_id:
        raise SystemExit(f"knowledge base {CFG['kb_name']} not found")

    wanted = CFG.get("chunk_data_source_name", "ast-chunks")
    sources = agent.list_data_sources(knowledgeBaseId=kb_id).get("dataSourceSummaries", [])
    for src in sources:
        if src["name"] == wanted:
            log(f"resolved {CFG['kb_name']}/{wanted} by name")
            return kb_id, src["dataSourceId"]

    raise SystemExit(
        f"data source '{wanted}' not found on {CFG['kb_name']}; "
        "run deploy/provision_chunk_source.py"
    )


def ingest(session: boto3.Session, wait: bool, poll: float = 5) -> dict | None:
    """Start an ingestion job; with wait, return the finished job (status, seconds).

    A job already running for this data source may have started before our
    upload, so it is waited out and a new job started, never skipped.
    """
    kb_id, ds_id = resolve_targets(session)

    agent = session.client("bedrock-agent")
    deadline = time.time() + 900
    while True:
        try:
            job = agent.start_ingestion_job(
                knowledgeBaseId=kb_id,
                dataSourceId=ds_id,
                description="Structure-aware chunk sync",
            )["ingestionJob"]
            break
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConflictException" or time.time() > deadline:
                raise
            log("an ingestion job is already running; waiting to start ours")
            time.sleep(poll)

    job_id = job["ingestionJobId"]
    started = time.time()
    log(f"started ingestion job {job_id}")
    if not wait:
        return None

    status = "TIMEOUT"
    while time.time() < deadline:
        time.sleep(poll)
        current = agent.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
        )["ingestionJob"]
        if current["status"] in ("COMPLETE", "FAILED", "STOPPED"):
            status = current["status"]
            log(f"ingestion {status}: {json.dumps(current.get('statistics', {}), default=str)}")
            for reason in current.get("failureReasons", []) or []:
                log(f"failure: {reason}")
            break
    else:
        log("ingestion did not finish within 15 minutes")
    return {"job_id": job_id, "status": status, "seconds": round(time.time() - started)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", default=str(ROOT / "knowledge-base"))
    add_arguments(parser)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-ingest", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument(
        "--summary-file",
        help="write the JSON summary here, for scripted consumption",
    )
    parser.add_argument(
        "--source-id",
        default=CFG.get("source_id", ""),
        help="namespace this repository under knowledge-base/<id>/ and chunks/<id>/",
    )
    parser.add_argument(
        "--link-to",
        default="branch",
        choices=["branch", "commit", "none"],
        help="search results link to the newest page (branch), the synced commit, or none",
    )
    args = parser.parse_args()

    global SOURCE_ID
    SOURCE_ID = args.source_id.strip().strip("/")
    if SOURCE_ID:
        log(f"source namespace: {SOURCE_ID}")

    session = make_session()
    source = source_from_parsed(args)
    log(f"content source: {source.name}")
    link = None
    if args.link_to != "none":
        link = getattr(source, "link", None) or source_links.detect(args.docs_dir)
        link = {**link, "link_to": args.link_to} if link else None
    log(f"source links: {link['base'] if link else 'none'}")
    result = sync(session, source, args.dry_run, link)
    print(json.dumps(result, indent=2))
    if args.summary_file:
        pathlib.Path(args.summary_file).write_text(json.dumps(result, indent=2))

    if not args.dry_run and not args.no_ingest and (result["uploaded"] or result["deleted"]):
        ingest(session, wait=not args.no_wait)

    return 0


if __name__ == "__main__":
    sys.exit(main())
