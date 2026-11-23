#!/usr/bin/env python3
"""Validate knowledge base markdown before it reaches the knowledge base.

Catches problems that silently degrade retrieval quality: missing front matter,
empty documents, and duplicate titles. Runs in CI on merge requests.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from content_sources import (  # noqa: E402
    ContentSource,
    add_arguments,
    as_source,
    source_from_parsed,
)

REQUIRED_KEYS = ("title", "category", "owner", "tags")
FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
MIN_BODY_CHARS = 200


def parse_front_matter(text: str) -> dict[str, str] | None:
    match = FRONT_MATTER.match(text)
    if not match:
        return None
    data: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip()
    return data


def validate(docs: ContentSource | pathlib.Path) -> list[str]:
    errors: list[str] = []
    source = as_source(docs)
    files = source.list_documents()

    if not files:
        return [f"no markdown files found under {source.name}"]

    titles: dict[str, str] = {}

    for rel in files:
        text = source.fetch(rel)

        meta = parse_front_matter(text)
        if meta is None:
            errors.append(f"{rel}: missing YAML front matter block")
            continue

        for key in REQUIRED_KEYS:
            if not meta.get(key):
                errors.append(f"{rel}: front matter missing required key '{key}'")

        body = FRONT_MATTER.sub("", text).strip()
        if len(body) < MIN_BODY_CHARS:
            errors.append(
                f"{rel}: body is only {len(body)} chars, "
                f"needs at least {MIN_BODY_CHARS} to retrieve usefully"
            )

        if not re.search(r"^#\s+\S", body, re.MULTILINE):
            errors.append(f"{rel}: no level-1 markdown heading in body")

        title = meta.get("title", "")
        if title:
            if title in titles:
                errors.append(f"{rel}: duplicate title '{title}' also in {titles[title]}")
            else:
                titles[title] = str(rel)

    return errors


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", default=str(root / "knowledge-base"))
    add_arguments(parser)
    args = parser.parse_args()

    source = source_from_parsed(args)
    errors = validate(source)
    count = len(source.list_documents())

    if errors:
        print(f"validation failed with {len(errors)} problem(s):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"validated {count} markdown document(s), no problems found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
