#!/usr/bin/env python3
"""Fast content path: a push to main that only edits knowledge base documents.

Used by .github/workflows/sync-content.yml (docs/content-sync.md):

    python infra/content_sync.py scope --before SHA --after SHA   # content-only?
    python infra/content_sync.py outputs                          # infra/outputs.json
    python infra/content_sync.py smoke --before SHA --after SHA \\
        --load-summary FILE --pushed-at EPOCH                     # new text is found

`smoke` asks, for each changed document, the longest line the push added, and
passes when the top search hit is that document and holds the new text.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from settings import OUTPUTS_FILE, REPO, deployment, sources, stack_name  # noqa: E402

CONTENT_DIR = "sample-content/knowledge-base/"
ZERO_SHA = "0" * 40
FRONT_MATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)


# ------------------------------------------------------------------ pure parts


def is_content_only(paths: list[str]) -> bool:
    """True when every changed path is a knowledge base document."""
    return bool(paths) and all(p.startswith(CONTENT_DIR) for p in paths)


def normalize(text: str) -> str:
    """Markdown line to plain words: drop table pipes, emphasis and extra spaces."""
    text = re.sub(r"[|*_`#>]", " ", text)
    text = re.sub(r"^\s*(?:[-+]|\d+\.)\s+", " ", text)
    return " ".join(text.split()).lower()


def added_lines(diff: str) -> list[str]:
    """The '+' lines of a unified diff, without the file headers."""
    return [
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def probe(diff: str, new_text: str) -> str | None:
    """The longest added line that is body text (not front matter, not a table rule)."""
    body = {normalize(line) for line in FRONT_MATTER.sub("", new_text).splitlines()}
    candidates = [
        n
        for n in (normalize(line) for line in added_lines(diff))
        if n in body and len(n.split()) >= 2 and re.search(r"[a-z0-9]", n)
    ]
    return max(candidates, key=len) if candidates else None


def text_rank(hits: dict, doc_id: str, text: str) -> int:
    """1-based rank of the first hit from the document that holds the text; 0 if none."""
    for rank, hit in enumerate(hits.get("results") or [], 1):
        if hit.get("document_id") == doc_id and text in normalize(hit.get("snippet", "")):
            return rank
    return 0


# --------------------------------------------------------------------- git, gh


def git(*args: str) -> str:
    # Fixed argv, no shell; the arguments are commit ids and repo paths.
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,  # noqa: S607
    ).stdout


def changed(before: str, after: str) -> list[str]:
    if not before or before == ZERO_SHA:
        return []
    return [p for p in git("diff", "--name-only", f"{before}..{after}").splitlines() if p]


def notice(title: str, message: str, level: str = "notice") -> None:
    print(f"{title}: {message}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title={title}::{message.replace(chr(10), ' ')}")


def set_output(**values: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            for k, v in values.items():
                fh.write(f"{k}={v}\n")


# ----------------------------------------------------------------- subcommands


def cmd_scope(args: argparse.Namespace) -> int:
    paths = changed(args.before, args.after)
    only = is_content_only(paths)
    print(f"changed: {paths}")
    notice(
        "Content sync scope",
        "content-only: fast sync"
        if only
        else "not content-only: deploy-main does the full deploy and load",
    )
    set_output(content_only=str(only).lower())
    return 0


def cmd_outputs(_: argparse.Namespace) -> int:
    """Stack outputs from CloudFormation, in the cdk --outputs-file shape."""
    import boto3

    name = stack_name()
    cfn = boto3.client("cloudformation", region_name=deployment().get("region"))
    stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
    values = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    OUTPUTS_FILE.write_text(json.dumps({name: values}, indent=2))
    print(f"wrote {len(values)} outputs of {name} to {OUTPUTS_FILE}")
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    import uuid

    import smoke_test
    from settings import outputs

    arn = outputs()["RuntimeArnKb"]
    source_id = sources(deployment())[0]["id"]
    session = str(uuid.uuid4()) + "-content-sync"
    load = json.loads(pathlib.Path(args.load_summary).read_text())
    job = load.get("ingestion") or {}
    notice(
        "Ingestion seconds",
        f"{job.get('seconds', 0)} ({job.get('status', 'none')}) | chunks uploaded "
        f"{load.get('uploaded', 0)}, deleted {load.get('deleted', 0)}",
    )

    failures = checked = 0
    for path in changed(args.before, args.after):
        file = REPO / path
        if not file.is_file():
            notice("Smoke", f"{path} deleted: nothing to ask")
            continue
        diff = git("diff", f"{args.before}..{args.after}", "--", path)
        text = probe(diff, file.read_text())
        if text is None:
            notice("Smoke", f"{path}: no new body text to ask about")
            continue
        doc_id = f"{source_id}/{path[len(CONTENT_DIR) :]}"
        # Pass: the new text is in one of the document's top 3 passages (search
        # scoped to the document, so other documents' ranking cannot hide it).
        # Info: where that passage ranks in a search of the whole knowledge base.
        query = {"query": text, "snippet_chars": 4000}
        rank = overall = 0
        doc_hits: dict = {}
        for _ in range(args.tries):  # the index can lag the job a little
            doc_hits = smoke_test.tool(
                arn,
                session,
                "search_knowledge_base",
                {**query, "document_id": doc_id, "max_results": 3},
            )
            rank = text_rank(doc_hits, doc_id, text)
            if rank:
                break
            time.sleep(5)
        whole = smoke_test.tool(arn, session, "search_knowledge_base", {**query, "max_results": 10})
        overall = text_rank(whole, doc_id, text)
        top = (whole.get("results") or [{}])[0]
        snippet = normalize(((doc_hits.get("results") or [{}])[0]).get("snippet", ""))
        checked += 1
        failures += 0 if rank else 1
        notice(
            "Smoke " + ("PASS" if rank else "FAIL"),
            f"{doc_id} | asked: {text[:200]} | in doc: passage {rank or 'none'} of 3"
            f" | whole KB: rank {overall or '>10'}, top {top.get('document_id')}"
            + ("" if rank else f" | doc top passage: {snippet[:300]}"),
            "notice" if rank else "error",
        )

    if args.pushed_at:
        total = round(time.time() - float(args.pushed_at))
        notice("Push to searchable seconds", f"{total} ({checked} doc(s) checked)")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("scope", "smoke"):
        p = sub.add_parser(name)
        p.add_argument("--before", required=True)
        p.add_argument("--after", required=True)
    sub.choices["smoke"].add_argument("--load-summary", required=True)
    sub.choices["smoke"].add_argument("--pushed-at", default="")
    sub.choices["smoke"].add_argument("--tries", type=int, default=6)
    sub.add_parser("outputs")
    args = parser.parse_args(argv)
    return {"scope": cmd_scope, "outputs": cmd_outputs, "smoke": cmd_smoke}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
