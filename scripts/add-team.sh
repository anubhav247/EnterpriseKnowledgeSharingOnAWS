#!/usr/bin/env bash
# Add a team: one EKS stack for one team's content repo. Guide: docs/add-a-team.md.
#
#   scripts/add-team.sh --team legal --content ../legal-content [install flags]
#   scripts/add-team.sh --help
#
# Safe to run again. Uses your current AWS login: an admin in the target
# account, or the path B installer role. It checks first (docs/install-permissions.md).
set -euo pipefail
cd "$(dirname "$0")/.."
echo "EKS repo: $PWD" >&2
# shellcheck source=scripts/python.sh
source scripts/python.sh
exec "$PY" scripts/eks_add_team.py "$@"
