#!/usr/bin/env python3
"""Load a local markdown folder into the deployed knowledge base.

Use it for the first load, or to sync without a pipeline. It runs the same code
the Azure DevOps pipeline runs (sync/sync_chunks.py), with targets taken from
the stack outputs.

    python infra/load_content.py                        # sample-content/knowledge-base
    python infra/load_content.py --docs-dir path/to/knowledge-base [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import boto3

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "sync"))

import source_links  # noqa: E402

import sync_chunks  # noqa: E402
from content_sources import LocalFolderSource  # noqa: E402
from settings import REPO, deployment, outputs, sources  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--docs-dir", default=str(REPO / "sample-content" / "knowledge-base"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source-id", help="namespace to load (default: the first source)")
    parser.add_argument("--summary-file", help="write the sync and ingestion result as JSON")
    args = parser.parse_args()

    cfg, out = deployment(), outputs()
    session = boto3.Session(region_name=cfg.get("region", "us-east-1"))

    sync_chunks.CFG.update(
        {
            "region": session.region_name,
            "docs_bucket": out["DocsBucketName"],
            "docs_prefix": out["DocsPrefix"],
            "knowledge_base_id": out["KnowledgeBaseId"],
            "data_source_id": out["DataSourceId"],
        }
    )
    sync_chunks.CHUNK_PREFIX = out["ChunkPrefix"]
    sync_chunks.SOURCE_ID = args.source_id or sources(cfg)[0]["id"]

    path = pathlib.Path(args.docs_dir).resolve()
    if not path.is_dir():
        raise SystemExit(f"not a directory: {path}")
    print(f"source {sync_chunks.SOURCE_ID}: {path}")
    # Page links from the git checkout the folder is in; none for a plain folder.
    entry = next((s for s in sources(cfg) if s["id"] == sync_chunks.SOURCE_ID), {})
    link = source_links.detect(str(path), entry)
    print(f"source links: {source_links.url(link, '') if link else 'none'}")
    result = sync_chunks.sync(session, LocalFolderSource(path), args.dry_run, link)
    print(json.dumps(result, indent=2))

    job = None
    if not args.dry_run and (result["uploaded"] or result["deleted"]):
        job = sync_chunks.ingest(session, wait=True)
        print(json.dumps({"ingestion": job}))
    if args.summary_file:
        pathlib.Path(args.summary_file).write_text(json.dumps({**result, "ingestion": job}))
    return 0 if job is None or job["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    sys.exit(main())
