#!/usr/bin/env python3
"""Check the deploy's usage report against the smoke test's questions.

    python -m reports.usage --since 1h ... --format json > usage.json
    python infra/check_usage_report.py usage.json

Passes when every off-topic smoke question is a no-answer question, no sample
question is, and exactly STALE_DOCUMENTS documents are stale. Exit 1 otherwise.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

# sample-content keeps exactly 2 documents stale on purpose.
STALE_DOCUMENTS = 2


def problems(
    report: dict[str, Any], off_topic: list[str], answered: list[str], stale: int
) -> list[str]:
    """What is wrong with the report; empty when it matches the smoke test."""
    flagged = {q["query"] for q in report["no_answer_questions"]}
    found = [f"not flagged as no answer: {q}" for q in off_topic if q not in flagged]
    found += [f"flagged as no answer: {q}" for q in answered if q in flagged]
    count = len(report["stale_documents"])
    if count != stale:
        found.append(f"stale documents {count}, expected {stale}")
    return found


def main(argv: list[str]) -> int:  # pragma: no cover - needs deployment.json
    from smoke_test import OFF_TOPIC_QUESTIONS, SAMPLE_QUESTIONS

    report = json.loads(pathlib.Path(argv[0]).read_text())
    found = problems(report, OFF_TOPIC_QUESTIONS, [q for q, _ in SAMPLE_QUESTIONS], STALE_DOCUMENTS)
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
