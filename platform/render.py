#!/usr/bin/env python3
"""Copy platform/api.py and platform/kickoff.py into eks-platform.yaml.

The template carries its two functions inline (ZipFile), so it deploys as one
file: from CloudShell, or as a quick-create link, with no bucket first. The
code is written, linted and tested as .py files; this puts it in place.

    python3 platform/render.py           # rewrite the template
    python3 platform/render.py --check   # exit 1 when the template is stale

A line `# code: platform/<file>.py` right above `ZipFile: |` marks a block.
"""

from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
TEMPLATE = HERE / "eks-platform.yaml"
MARK = re.compile(r"^(\s*)# code: platform/(\w+\.py)\s*$")


def render(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = MARK.match(line)
        i += 1
        if not m:
            continue
        zip_line = lines[i]
        if zip_line.strip() != "ZipFile: |":
            raise SystemExit(f"line {i + 1}: expected 'ZipFile: |' after {line.strip()}")
        out.append(zip_line)
        i += 1
        indent = len(zip_line) - len(zip_line.lstrip()) + 2
        while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) >= indent):
            i += 1  # the old block
        code = (HERE / m.group(2)).read_text().rstrip("\n").splitlines()
        out += [(" " * indent + c) if c.strip() else "" for c in code]
        if i < len(lines):
            out.append("")  # the blank line the old block's end took
    return "\n".join(out) + "\n"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def main(argv: list[str]) -> int:
    old = TEMPLATE.read_text()
    new = render(old)
    if "--check" in argv:
        if new != old:
            print(f"{TEMPLATE}: stale, run python3 platform/render.py")
            return 1
        return 0
    TEMPLATE.write_text(new)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
