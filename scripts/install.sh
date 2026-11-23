#!/usr/bin/env bash
# One-command install into your AWS account. Guide: docs/install.md.
#
#   scripts/install.sh --prefix kb --region us-east-1 --auth iam --content sample-content
#   scripts/install.sh --help
#
# Safe to run again. Uses your current AWS login (admin in the target account).
set -euo pipefail
cd "$(dirname "$0")/.."
echo "EKS repo: $PWD" >&2
# shellcheck source=scripts/python.sh
source scripts/python.sh
exec "$PY" scripts/eks_install.py install "$@"
