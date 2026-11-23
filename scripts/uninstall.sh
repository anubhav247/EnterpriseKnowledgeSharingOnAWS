#!/usr/bin/env bash
# Remove what scripts/install.sh made, then list anything left with the prefix.
# Exit code 1 when something is left. Guide: docs/install.md.
#
#   scripts/uninstall.sh
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/python.sh
source scripts/python.sh
exec "$PY" scripts/eks_install.py uninstall "$@"
