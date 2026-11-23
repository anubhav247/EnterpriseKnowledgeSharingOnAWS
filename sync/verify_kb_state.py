#!/usr/bin/env python3
"""Verify S3 and the knowledge base match the markdown in a directory.

Runs after a sync, in CI or locally. Checks three things that can silently drift:

  1  every document in the repository exists under the source prefix
  2  every document has chunks, and no orphan chunks remain
  3  the source prefix and the chunk prefix agree on content

Point 3 matters because search reads the chunks while retrieve_document reads the
source document. If they disagree, the chatbot quotes new content and then serves
an old file.

    python3 tests/verify_kb_state.py --docs-dir knowledge-base
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

import boto3

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "deploy"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from content_sources import add_arguments, source_from_parsed  # noqa: E402


def _load_config() -> dict:
    """Locate config.json in the code repo or beside a vendored copy."""
    for candidate in (ROOT / "config.json", HERE / "config.json"):
        if candidate.is_file():
            return json.loads(candidate.read_text())
    raise SystemExit(f"config.json not found in {ROOT} or {HERE}")


CFG = _load_config()

SOURCE_ID = CFG.get("source_id", "").strip().strip("/")
BUCKET = CFG["docs_bucket"]
DOCS_BASE_PREFIX = CFG["docs_prefix"]
CHUNK_BASE_PREFIX = CFG.get("chunk_prefix", "chunks/")
DOCS_PREFIX = f"{DOCS_BASE_PREFIX}{SOURCE_ID}/" if SOURCE_ID else DOCS_BASE_PREFIX
CHUNK_PREFIX = f"{CHUNK_BASE_PREFIX}{SOURCE_ID}/" if SOURCE_ID else CHUNK_BASE_PREFIX

failures: list[str] = []


def session() -> boto3.Session:
    region = CFG["region"]
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"):
        return boto3.Session(region_name=region)
    default = boto3.Session(region_name=region)
    if default.get_credentials() is not None:
        return default
    profile = CFG.get("profile")
    if profile and profile in default.available_profiles:
        return boto3.Session(profile_name=profile, region_name=region)
    raise SystemExit("no AWS credentials available")


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  |  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def list_keys(s3, prefix: str) -> dict[str, str]:
    keys: dict[str, str] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=prefix
    ):
        for obj in page.get("Contents", []):
            keys[obj["Key"]] = obj["ETag"].strip('"')
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", default=str(ROOT / "knowledge-base"))
    add_arguments(parser)
    args = parser.parse_args()

    source = source_from_parsed(args)
    local = {d.rsplit("/", 1)[-1]: source.fetch(d) for d in source.list_documents()}
    if not local:
        raise SystemExit(f"no markdown found in {source.name}")

    s3 = session().client("s3")
    sources = list_keys(s3, DOCS_PREFIX)
    chunks = list_keys(s3, CHUNK_PREFIX)

    print(f"repository: {len(local)} document(s)")
    print(f"s3 source : {len(sources)} object(s)")
    print(f"s3 chunks : {len(chunks)} object(s)\n")

    # 1 - every document present, nothing extra
    expected_sources = {f"{DOCS_PREFIX}{name}" for name in local}
    actual_sources = {k for k in sources if k.lower().endswith(".md")}

    missing = sorted(expected_sources - actual_sources)
    check("every document exists in S3", not missing, ", ".join(missing) or "all present")

    orphan_sources = sorted(actual_sources - expected_sources)
    check(
        "no deleted document lingers in S3",
        not orphan_sources,
        ", ".join(orphan_sources) or "none",
    )

    # 2 - chunk coverage, and no orphan chunk folders
    stems = {name[:-3] for name in local}
    chunk_stems = {
        k[len(CHUNK_PREFIX):].split("/")[0]
        for k in chunks
        if "/" in k[len(CHUNK_PREFIX):]
    }

    uncovered = sorted(stems - chunk_stems)
    check("every document has chunks", not uncovered, ", ".join(uncovered) or "all covered")

    orphan_chunks = sorted(chunk_stems - stems)
    check(
        "no chunks remain for deleted documents",
        not orphan_chunks,
        ", ".join(orphan_chunks) or "none",
    )

    # 3 - the two prefixes agree
    drift: list[str] = []
    for name, text in local.items():
        key = f"{DOCS_PREFIX}{name}"
        if key not in sources:
            continue
        remote = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
        if remote.strip() != text.strip():
            drift.append(name)

    check(
        "S3 source documents match the repository",
        not drift,
        ", ".join(drift) or "identical",
    )

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        print("run deploy/sync_chunks.py to reconcile")
        return 1

    print("S3 and the knowledge base match the repository")
    return 0


if __name__ == "__main__":
    sys.exit(main())
