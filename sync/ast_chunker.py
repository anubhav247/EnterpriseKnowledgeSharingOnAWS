"""Structure-aware markdown chunking and section-level change detection.

Replaces blind token-window chunking with chunks that follow the document's own
structure, and lets the sync path re-embed only the sections that changed
instead of every chunk in a modified file.

Two capabilities:

1. `chunk_document` walks the markdown syntax tree and emits one chunk per
   section, keeping tables, code blocks, and definitions intact. Oversized
   sections are split at block boundaries rather than mid-row or mid-statement.

2. `diff_manifests` compares per-section content hashes between two versions of
   a document, so a typo fix in one section does not re-embed its siblings.

Uses markdown-it-py for parsing. It is CommonMark compliant and returns a token
stream with nesting levels and source line maps, which is what we need to
reassemble exact source text for a section.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

try:
    from markdown_it import MarkdownIt

    _HAVE_MDIT = True
except ImportError:  # fall back to a heading regex so sync still works
    _HAVE_MDIT = False

# Approximate tokens per character for English prose plus code. Bedrock counts
# real tokens; this only decides when to split, so an estimate is sufficient.
CHARS_PER_TOKEN = 4

DEFAULT_MAX_TOKENS = 1500
DEFAULT_MIN_TOKENS = 40

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def content_hash(text: str) -> str:
    """Stable hash of section content, insensitive to trailing whitespace."""
    normalised = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


@dataclass
class Section:
    """One structural region of a markdown document."""

    heading: str
    level: int
    body: str
    start_line: int
    breadcrumb: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Section content including its heading, as it appeared in the source."""
        if not self.heading:
            return self.body.strip()
        return f"{'#' * self.level} {self.heading}\n\n{self.body.strip()}".strip()

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.text)

    @property
    def path(self) -> str:
        """Heading trail, so a chunk carries its place in the document."""
        return " > ".join([*self.breadcrumb, self.heading]) if self.heading else "(preamble)"


@dataclass
class Chunk:
    """An embeddable unit of text with the metadata needed to cite it."""

    text: str
    section_path: str
    heading: str
    part: int = 1
    of_parts: int = 1

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.text)

    @property
    def content_hash(self) -> str:
        return content_hash(self.text)


def strip_front_matter(markdown: str) -> tuple[dict[str, str], str]:
    """Split YAML front matter from the body, returning both."""
    match = _FRONT_MATTER.match(markdown)
    if not match:
        return {}, markdown

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.strip().startswith("#"):
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, markdown[match.end():]


def _heading_tokens(markdown: str) -> list[tuple[int, int, str]]:
    """Return (line_index, level, heading_text) for every ATX heading.

    Uses the markdown AST when available so that '#' inside a fenced code block
    is not mistaken for a heading. Falls back to a line scan that tracks fences.
    """
    if _HAVE_MDIT:
        md = MarkdownIt("commonmark")
        tokens = md.parse(markdown)
        headings: list[tuple[int, int, str]] = []
        for i, token in enumerate(tokens):
            if token.type != "heading_open":
                continue
            level = int(token.tag[1])
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            text = (inline.content if inline is not None else "").strip()
            headings.append((token.map[0] if token.map else 0, level, text))
        return headings

    headings = []
    in_fence = False
    for idx, line in enumerate(markdown.splitlines()):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _ATX_HEADING.match(line)
        if match:
            headings.append((idx, len(match.group(1)), match.group(2).strip()))
    return headings


def parse_sections(markdown: str) -> list[Section]:
    """Split a document into sections at heading boundaries."""
    _, body = strip_front_matter(markdown)
    lines = body.splitlines()
    headings = _heading_tokens(body)

    if not headings:
        return (
            [Section(heading="", level=0, body=body, start_line=0)]
            if body.strip()
            else []
        )

    sections: list[Section] = []

    # Content before the first heading, if any.
    first_line = headings[0][0]
    preamble = "\n".join(lines[:first_line]).strip()
    if preamble:
        sections.append(Section(heading="", level=0, body=preamble, start_line=0))

    trail: list[tuple[int, str]] = []
    for pos, (line_idx, level, text) in enumerate(headings):
        end = headings[pos + 1][0] if pos + 1 < len(headings) else len(lines)
        section_body = "\n".join(lines[line_idx + 1 : end])

        # Maintain the ancestor heading trail for this level.
        while trail and trail[-1][0] >= level:
            trail.pop()

        sections.append(
            Section(
                heading=text,
                level=level,
                body=section_body,
                start_line=line_idx,
                breadcrumb=[t for _, t in trail],
            )
        )
        trail.append((level, text))

    return sections


def _split_blocks(text: str) -> list[str]:
    """Split section body into atomic blocks that must not be broken apart.

    Fenced code blocks and contiguous table rows are kept whole, because a
    fragment of either is useless in isolation and actively misleading if
    retrieved.
    """
    lines = text.splitlines()
    blocks: list[str] = []
    buffer: list[str] = []
    in_fence = False
    fence_marker = ""

    def flush() -> None:
        if buffer:
            chunk = "\n".join(buffer).strip()
            if chunk:
                blocks.append(chunk)
            buffer.clear()

    for line in lines:
        stripped = line.strip()

        if in_fence:
            buffer.append(line)
            if stripped.startswith(fence_marker):
                in_fence = False
                flush()
            continue

        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush()
            fence_marker = stripped[:3]
            in_fence = True
            buffer.append(line)
            continue

        # Blank line ends a paragraph, but not a table (tables have no blanks).
        if not stripped:
            flush()
            continue

        buffer.append(line)

    if in_fence:  # unterminated fence; keep what we have
        flush()
    else:
        flush()

    return blocks


def chunk_document(
    markdown: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    min_tokens: int = DEFAULT_MIN_TOKENS,
) -> list[Chunk]:
    """Chunk a markdown document at structural boundaries.

    Sections that fit within max_tokens become exactly one chunk. Oversized
    sections split at block boundaries. Very small sections merge into the next
    one, since a two-line chunk carries too little context to retrieve well.
    """
    sections = parse_sections(markdown)
    chunks: list[Chunk] = []
    pending: Section | None = None

    for section in sections:
        if pending is not None:
            # Merge an undersized section forward. The combined chunk is labelled
            # with the merged section's own path so that citations name the
            # section a reader would actually look for, and both headings stay in
            # the text. Labelling it with the predecessor would misattribute the
            # content, which matters because the label becomes the citation.
            merged_body = f"{pending.text}\n\n{section.text}".strip()
            section = Section(
                heading=section.heading or pending.heading,
                level=section.level or pending.level,
                body="",
                start_line=pending.start_line,
                breadcrumb=section.breadcrumb or pending.breadcrumb,
            )
            # Body is pre-rendered, so bypass Section.text heading synthesis.
            chunks.append(
                Chunk(
                    text=merged_body,
                    section_path=section.path,
                    heading=section.heading,
                )
            )
            pending = None
            continue

        if section.token_estimate < min_tokens and section is not sections[-1]:
            pending = section
            continue

        if section.token_estimate <= max_tokens:
            chunks.append(
                Chunk(text=section.text, section_path=section.path, heading=section.heading)
            )
            continue

        # Oversized: split at block boundaries, repeating the heading so each
        # part remains self-describing when retrieved on its own.
        header = f"{'#' * section.level} {section.heading}" if section.heading else ""
        parts: list[str] = []
        current: list[str] = []
        current_tokens = estimate_tokens(header)

        for block in _split_blocks(section.body):
            block_tokens = estimate_tokens(block)
            if current and current_tokens + block_tokens > max_tokens:
                parts.append("\n\n".join(current))
                current = [block]
                current_tokens = estimate_tokens(header) + block_tokens
            else:
                current.append(block)
                current_tokens += block_tokens
        if current:
            parts.append("\n\n".join(current))

        for i, part in enumerate(parts, start=1):
            body = f"{header}\n\n{part}".strip() if header else part
            chunks.append(
                Chunk(
                    text=body,
                    section_path=section.path,
                    heading=section.heading,
                    part=i,
                    of_parts=len(parts),
                )
            )

    if pending is not None:  # trailing undersized section
        chunks.append(
            Chunk(text=pending.text, section_path=pending.path, heading=pending.heading)
        )

    return chunks


def build_manifest(markdown: str, **kwargs: Any) -> dict[str, str]:
    """Map chunk key to content hash, for section-level change detection."""
    manifest: dict[str, str] = {}
    for index, chunk in enumerate(chunk_document(markdown, **kwargs)):
        key = f"{index:03d}|{chunk.section_path}"
        if chunk.of_parts > 1:
            key = f"{key}#part{chunk.part}"
        manifest[key] = chunk.content_hash
    return manifest


@dataclass
class ManifestDiff:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def needs_embedding(self) -> list[str]:
        return [*self.added, *self.changed]

    @property
    def total(self) -> int:
        return len(self.added) + len(self.changed) + len(self.removed) + len(self.unchanged)

    def summary(self) -> str:
        return (
            f"{len(self.added)} added, {len(self.changed)} changed, "
            f"{len(self.removed)} removed, {len(self.unchanged)} unchanged"
        )


def diff_manifests(old: dict[str, str], new: dict[str, str]) -> ManifestDiff:
    """Compare two chunk manifests to find what actually needs re-embedding."""
    diff = ManifestDiff()
    for key, digest in new.items():
        if key not in old:
            diff.added.append(key)
        elif old[key] != digest:
            diff.changed.append(key)
        else:
            diff.unchanged.append(key)
    diff.removed = [k for k in old if k not in new]
    return diff


def analyze(markdown: str, **kwargs: Any) -> dict[str, Any]:
    """Chunking statistics for a document, used for cost reporting."""
    chunks = chunk_document(markdown, **kwargs)
    tokens = [c.token_estimate for c in chunks]
    return {
        "chunk_count": len(chunks),
        "total_tokens": sum(tokens),
        "mean_tokens": round(sum(tokens) / len(tokens), 1) if chunks else 0,
        "max_tokens": max(tokens, default=0),
        "min_tokens": min(tokens, default=0),
        "split_sections": sum(1 for c in chunks if c.of_parts > 1),
    }


def simulate_token_windows(
    markdown: str, child_tokens: int = 300, overlap_tokens: int = 60
) -> dict[str, Any]:
    """Model the current hierarchical child-chunk count for comparison.

    Bedrock embeds child chunks with overlap, so effective stride is
    child_tokens minus overlap. This estimates the vector count that produces.
    """
    _, body = strip_front_matter(markdown)
    total = estimate_tokens(body)
    stride = max(1, child_tokens - overlap_tokens)
    count = max(1, -(-total // stride))  # ceiling division
    return {
        "chunk_count": count,
        "total_tokens": total,
        "embedded_tokens": count * min(child_tokens, total),
    }
