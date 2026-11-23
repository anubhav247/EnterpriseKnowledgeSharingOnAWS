# Sourced by install.sh, add-team.sh and uninstall.sh: sets PY to a Python 3.12
# that has infra/requirements.txt and pip. Order: $PYTHON, a python3 that already
# has them (CI tools dir, built with the exact pin from versions.env), else .venv
# made with uv from any Python 3.12.x (installed, or one uv downloads).
# pip: runtimeArtifact "code" bundles with `python -m pip` (kb_mcp_stack.PipBundling),
# else CDK falls back to Docker. A uv venv has no pip unless asked for.
# shellcheck shell=bash
has_deps() { "$1" -c 'import sys, aws_cdk, boto3, awscrt, pip; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; }
is_312() { "$1" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2>/dev/null; }

if [[ -n "${PYTHON:-}" ]]; then
  PY=$PYTHON
elif command -v python3 >/dev/null && has_deps python3; then
  PY=$(command -v python3)
else
  command -v uv >/dev/null || {
    echo "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
  }
  PY=.venv/bin/python
  if ! { [[ -x $PY ]] && is_312 "$PY"; }; then
    uv venv -q --clear --python 3.12 .venv || {
      echo "No Python 3.12 for uv. Run: uv python install 3.12 (if that fails: brew upgrade uv)" >&2
      exit 1
    }
  fi
  if ! has_deps "$PY"; then
    echo "Installing Python packages (first run, about 3 min)..." >&2
    uv pip install -q --python "$PY" pip -r infra/requirements.txt
    echo "done" >&2
  fi
fi
