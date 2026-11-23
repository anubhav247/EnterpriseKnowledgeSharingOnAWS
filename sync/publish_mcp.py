#!/usr/bin/env python3
"""Validate, compile and publish a content repository's MCP instructions and prompts.

Domain teams own two kinds of file next to their documents:

    mcp/instructions.md        how assistants should use this knowledge base
    mcp/prompts/<name>.md      one reusable prompt per file

Prompt file format:

    ---
    name: check_clause                      # optional, defaults to the file name
    description: Check a clause against policy
    arguments:
      - clause: The clause text (required)
      - jurisdiction: Governing law, if known
    ---
    Body of the prompt. Use {{clause}} and {{jurisdiction}} where values go.

This script is the only parser of these files. It writes one compiled object,
mcp/<source>/published.json, that the MCP server reads, then asks the platform's
publisher function to copy the instructions onto the gateway and refresh the
gateway's prompt and resource lists. The pipeline never calls the gateway API
itself, so it cannot change who may sign in.

    publish_mcp.py --mcp-dir mcp --validate-only          # pull-request builds
    publish_mcp.py --mcp-dir mcp --source-id analytics    # main
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys

# The gateway accepts at most 2048 characters of instructions. The version line
# and the staleness hint below are appended to whatever the domain team writes.
GATEWAY_INSTRUCTIONS_MAX = 2048
VERSION_SUFFIX = (
    "\n\nInstructions-Version: {version}\n"
    "Tool results report the current instructions_version. If it differs from "
    "the line above, read resource kb://instructions for the latest text and "
    "tell the user to reconnect this MCP server."
)
INSTRUCTIONS_MAX = GATEWAY_INSTRUCTIONS_MAX - len(VERSION_SUFFIX.format(version="x" * 10))

PROMPT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
ARG_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PLACEHOLDER = re.compile(r"\{\{\s*([^}]*?)\s*\}\}")
FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
MAX_PROMPTS = 50
MAX_PROMPT_CHARS = 20_000


class McpContentError(ValueError):
    pass


def split_front_matter(text: str) -> tuple[list[str], str]:
    match = FRONT_MATTER.match(text)
    if not match:
        return [], text.strip()
    return match.group(1).splitlines(), text[match.end():].strip()


def parse_prompt(path: pathlib.Path) -> dict:
    """Parse one prompt file. Raises McpContentError with every problem found."""
    lines, body = split_front_matter(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    meta: dict[str, str] = {}
    arguments: list[dict] = []
    in_args = False
    for line in lines:
        if not line.strip() or line.strip().startswith("#"):
            continue
        if in_args and line[:1] in (" ", "\t", "-"):
            item = line.strip()
            if not item.startswith("-"):
                errors.append(f"argument line must start with '-': {item}")
                continue
            name, _, desc = item[1:].strip().partition(":")
            desc = desc.strip()
            required = "(required)" in desc.lower()
            desc = re.sub(r"\s*\(required\)\s*", " ", desc, flags=re.IGNORECASE).strip()
            arguments.append({"name": name.strip(), "description": desc, "required": required})
            continue
        in_args = False
        key, sep, value = line.partition(":")
        if not sep:
            errors.append(f"front matter line has no ':': {line.strip()}")
            continue
        key = key.strip()
        if key == "arguments":
            in_args = True
            if value.strip():
                errors.append("list arguments on their own lines, one '- name: description' each")
            continue
        meta[key] = value.strip()

    name = meta.get("name") or path.stem
    if not PROMPT_NAME.match(name):
        errors.append(f"name '{name}' must be lowercase letters, digits, '-' or '_' (max 48)")
    if not meta.get("description"):
        errors.append("front matter needs a 'description'")
    if not body:
        errors.append("prompt body is empty")
    if len(body) > MAX_PROMPT_CHARS:
        errors.append(f"prompt body is {len(body)} chars, max {MAX_PROMPT_CHARS}")
    seen: set[str] = set()
    for arg in arguments:
        if not ARG_NAME.match(arg["name"]):
            errors.append(f"argument name '{arg['name']}' must be letters, digits, '-' or '_'")
        if arg["name"] in seen:
            errors.append(f"argument '{arg['name']}' is listed twice")
        seen.add(arg["name"])
    for used in PLACEHOLDER.findall(body):
        if used not in seen:
            errors.append(f"placeholder {{{{{used}}}}} is not a declared argument")
    if errors:
        raise McpContentError("; ".join(errors))
    return {"name": name, "description": meta["description"], "template": body,
            "arguments": arguments}


def read_instructions(mcp_dir: pathlib.Path) -> str:
    path = mcp_dir / "instructions.md"
    if not path.is_file():
        return ""
    _, body = split_front_matter(path.read_text(encoding="utf-8"))
    return body


SUMMARY_MAX = 300


def read_about(mcp_dir: pathlib.Path) -> dict:
    """Owner (front matter) and summary (first paragraph) of instructions.md.

    The server puts both into the search tool description, so an agent sees
    which team's knowledge base it is talking to.
    """
    path = mcp_dir / "instructions.md"
    if not path.is_file():
        return {"owner": "", "summary": ""}
    lines, body = split_front_matter(path.read_text(encoding="utf-8"))
    owner = next((v.strip() for k, _, v in (ln.partition(":") for ln in lines)
                  if k.strip() == "owner"), "")
    summary = " ".join(body.split("\n\n", 1)[0].split())
    if len(summary) > SUMMARY_MAX:
        summary = summary[:SUMMARY_MAX].rsplit(" ", 1)[0] + " ..."
    return {"owner": owner, "summary": summary}


def validate(mcp_dir: pathlib.Path) -> list[str]:
    errors: list[str] = []
    if not mcp_dir.is_dir():
        return errors  # the folder is optional
    instructions = read_instructions(mcp_dir)
    if (mcp_dir / "instructions.md").is_file() and not instructions:
        errors.append("instructions.md: is empty")
    if len(instructions) > INSTRUCTIONS_MAX:
        errors.append(
            f"instructions.md: {len(instructions)} chars, max {INSTRUCTIONS_MAX} "
            f"(the gateway limit is {GATEWAY_INSTRUCTIONS_MAX} including the version line)"
        )
    files = sorted((mcp_dir / "prompts").glob("*.md")) if (mcp_dir / "prompts").is_dir() else []
    if len(files) > MAX_PROMPTS:
        errors.append(f"prompts/: {len(files)} prompts, max {MAX_PROMPTS}")
    names: dict[str, str] = {}
    for path in files:
        rel = path.relative_to(mcp_dir)
        try:
            prompt = parse_prompt(path)
        except McpContentError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        if prompt["name"] in names:
            errors.append(f"{rel}: prompt name '{prompt['name']}' also used by {names[prompt['name']]}")
        names[prompt["name"]] = str(rel)
    return errors


TEMPLATE = "templates/team-content/mcp/ in the EKS repo"


def content_checks(mcp_dir: pathlib.Path, docs_dir: pathlib.Path | None = None) -> list[dict]:
    """Warnings for a team: what is missing, and what that does to agents. Never an error.

    docs_dir: the documents folder; default the knowledge-base/ beside mcp/, when there.
    get_sync_status reports the list (content_checks), so an agent sees it too.
    """
    checks = []
    if not (mcp_dir / "instructions.md").is_file():
        checks.append(("no_instructions", "no mcp/instructions.md: agents get the generic "
                       f"built-in instructions. Add one (template: {TEMPLATE})"))
    elif not read_about(mcp_dir)["owner"]:
        checks.append(("no_owner", "mcp/instructions.md has no 'owner:' in its front matter: "
                       "agents cannot tell whose knowledge base this is"))
    prompts = mcp_dir / "prompts"
    if not (prompts.is_dir() and any(prompts.glob("*.md"))):
        checks.append(("no_prompts", "0 prompts in mcp/prompts/ (optional): add one to offer "
                       f"a reusable prompt (template: {TEMPLATE})"))
    if docs_dir is None and (mcp_dir.parent / "knowledge-base").is_dir():
        docs_dir = mcp_dir.parent / "knowledge-base"
    if docs_dir is not None and not (docs_dir.is_dir() and any(docs_dir.rglob("*.md"))):
        checks.append(("no_documents", f"0 documents in {docs_dir.name}/: the server has "
                       "nothing to answer from. Add markdown files"))
    return [{"check": c, "message": m} for c, m in checks]


def warn(checks: list[dict]) -> None:
    """One WARN line per check, plus a CI annotation on GitHub Actions or Azure Pipelines."""
    for c in checks:
        print(f"WARN content: {c['message']}", flush=True)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning title=EKS content::{c['message']}", flush=True)
        if os.environ.get("TF_BUILD"):
            print(f"##vso[task.logissue type=warning]{c['message']}", flush=True)


def compile_content(mcp_dir: pathlib.Path, now: dt.datetime | None = None,
                    docs_dir: pathlib.Path | None = None) -> dict:
    """Build published.json. Call validate() first."""
    body = read_instructions(mcp_dir) if mcp_dir.is_dir() else ""
    prompts_dir = mcp_dir / "prompts"
    prompts = [parse_prompt(p) for p in sorted(prompts_dir.glob("*.md"))] if prompts_dir.is_dir() else []
    published = {
        "published_at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"),
        "prompts": prompts,
        "version": "",
        "instructions": "",
        **(read_about(mcp_dir) if mcp_dir.is_dir() else {"owner": "", "summary": ""}),
        "content_checks": content_checks(mcp_dir, docs_dir),
    }
    if body:
        version = hashlib.sha256(body.encode("utf-8")).hexdigest()[:10]
        published["version"] = version
        published["instructions"] = body + VERSION_SUFFIX.format(version=version)
    return published


def published_key(source_id: str) -> str:
    return f"mcp/{source_id}/published.json"


def publish(session, bucket: str, source_id: str, content: dict, function_name: str) -> dict:
    session.client("s3").put_object(
        Bucket=bucket,
        Key=published_key(source_id),
        Body=json.dumps(content, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    print(f"published {published_key(source_id)}: version={content['version'] or '(default)'}, "
          f"{len(content['prompts'])} prompt(s)", flush=True)
    if not function_name:
        print("no publisher function configured; gateway left unchanged", flush=True)
        return {}
    resp = session.client("lambda").invoke(FunctionName=function_name, Payload=b"{}")
    result = json.loads(resp["Payload"].read() or b"{}")
    if resp.get("FunctionError"):
        raise SystemExit(f"publisher failed: {json.dumps(result)}")
    print(f"gateway: {json.dumps(result)}", flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mcp-dir", required=True)
    parser.add_argument("--source-id", default="")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--docs-dir", default="",
                        help="documents folder, for the 0-documents warning "
                             "(default: knowledge-base/ beside --mcp-dir, when there)")
    parser.add_argument("--function-name", default="",
                        help="publisher Lambda; defaults to mcp_publisher_function in config.json")
    args = parser.parse_args()

    mcp_dir = pathlib.Path(args.mcp_dir)
    errors = validate(mcp_dir)
    if errors:
        print(f"MCP content failed validation with {len(errors)} problem(s):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    content = compile_content(mcp_dir, docs_dir=pathlib.Path(args.docs_dir) if args.docs_dir else None)
    warn(content["content_checks"])
    print(f"validated MCP content: instructions {'present' if content['version'] else 'absent'}, "
          f"{len(content['prompts'])} prompt(s)")
    if args.validate_only:
        return 0

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import sync_chunks  # config.json loading and AWS session, shared with the docs sync

    source_id = args.source_id.strip().strip("/") or sync_chunks.CFG.get("source_id", "")
    if not source_id:
        raise SystemExit("--source-id is required")
    function_name = args.function_name or sync_chunks.CFG.get("mcp_publisher_function", "")
    publish(sync_chunks.make_session(), sync_chunks.CFG["docs_bucket"], source_id, content,
            function_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
