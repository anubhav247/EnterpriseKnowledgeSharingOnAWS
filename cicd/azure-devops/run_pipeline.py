#!/usr/bin/env python3
"""Queue the sync pipeline on a branch, wait, and print stages, checks and errors.

    python cicd/azure-devops/run_pipeline.py [--branch refs/heads/main]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "infra"))
from settings import deployment  # noqa: E402


def az(*args):
    r = subprocess.run(["az", *args, "-o", "json", "--only-show-errors"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(r.stderr.strip()[:500])
    return json.loads(r.stdout) if r.stdout.strip() else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ado = deployment().get("azureDevOps") or {}
    ap.add_argument("--org", default=ado.get("orgUrl"))
    ap.add_argument("--project", default=ado.get("project"))
    ap.add_argument("--pipeline", default="kb-content-sync")
    ap.add_argument("--branch", default="refs/heads/main")
    a = ap.parse_args()
    common = ["--org", a.org, "--project", a.project]

    run = az("pipelines", "run", "--name", a.pipeline, "--branch", a.branch, *common)
    run_id = run["id"]
    print(f"run {run_id} on {a.branch}", flush=True)
    while True:
        time.sleep(10)
        b = az("pipelines", "runs", "show", "--id", str(run_id), *common)
        if b["status"] == "completed":
            break
    print(f"result: {b['result']}")

    timeline = az("devops", "invoke", "--org", a.org, "--area", "build", "--resource", "timeline",
                  "--route-parameters", f"project={a.project}", f"buildId={run_id}",
                  "--api-version", "7.1")
    for r in sorted(timeline.get("records", []), key=lambda r: r.get("startTime") or ""):
        if r["type"] in ("Stage", "Checkpoint", "Task") and r.get("result"):
            print(f"  {r['type']:10} {r['name'][:50]:50} {r['result']}")
        for issue in r.get("issues") or []:
            print(f"      ! {issue.get('message', '')[:300]}")
    return 0 if b["result"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
