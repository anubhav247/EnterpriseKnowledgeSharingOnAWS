#!/usr/bin/env bash
# The EKS platform's build runner. CodeBuild runs it from the release bundle
# the platform stack keeps in its bucket (platform/eks-platform.yaml, buildspec).
#
#   ACTION=platform  put the console page and its settings in the console bucket
#   ACTION=create    TEAM=<team>: install or update the team (add-team.sh, --artifact code)
#   ACTION=delete    TEAM=<team>: remove the team stack (uninstall.sh), then its records
#
# Env from the CodeBuild project: BUCKET, CONSOLE_BUCKET, DISTRIBUTION_ID,
# CONSOLE_SETTINGS (JSON for the page), TEAM_PREFIX, RUNNER_ROLE_ARN, AWS_REGION.
# Records per team in s3://$BUCKET/teams/<team>/: request.json (from the
# console), team.json (last action), deployment.json + outputs.json (what
# uninstall needs), result.json (what the console shows), repo-files.zip
# (what the team commits to its repo).
# Guide: docs/platform.md.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${ACTION:?}" "${BUCKET:?}"
echo "EKS $(python3 -c 'import json; print(json.load(open("RELEASE.json"))["version"])' 2>/dev/null || echo dev): $ACTION ${TEAM:-}"

venv() {
  # The team install needs Python 3.12 with infra/requirements.txt and pip
  # (the code artifact is bundled with pip). CodeBuild's runtime has both.
  python3 -m venv /tmp/eks-venv
  /tmp/eks-venv/bin/python -m pip install -q --disable-pip-version-check -r infra/requirements.txt
  export PYTHON=/tmp/eks-venv/bin/python
}

case "$ACTION" in
  platform)
    : "${CONSOLE_BUCKET:?}" "${CONSOLE_SETTINGS:?}"
    dist=ui/team-setup/dist
    [[ -f $dist/index.html ]] || { echo "no $dist in the bundle" >&2; exit 1; }
    printf '%s\n' "$CONSOLE_SETTINGS" >"$dist/console.json"
    # The page and its settings change with each release: no long caching.
    # setup.html is the kickoff's "being set up" page: kept.
    aws s3 sync --delete --exclude setup.html --only-show-errors "$dist" "s3://$CONSOLE_BUCKET/" \
      --cache-control "no-cache"
    if [[ -n "${DISTRIBUTION_ID:-}" ]]; then
      inv=$(aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" --paths "/*" \
        --query Invalidation.Id --output text)
      aws cloudfront wait invalidation-completed --distribution-id "$DISTRIBUTION_ID" --id "$inv"
    fi
    echo "console page published"
    ;;

  create)
    : "${TEAM:?}" "${RUNNER_ROLE_ARN:?}"
    rec="s3://$BUCKET/teams/$TEAM"
    aws s3 cp --only-show-errors "$rec/request.json" /tmp/request.json
    # Through a file: a refused request must stop the build here.
    python3 platform/team.py args /tmp/request.json >/tmp/args.txt
    mapfile -t args </tmp/args.txt
    # A repo team starts from the template, never a clone (platform/team.py).
    mkdir -p .eks/teams
    if [[ ! -d .eks/teams/$TEAM ]]; then
      cp -R templates/team-content ".eks/teams/$TEAM"
      # Its one example document is a placeholder: the smoke test checks the
      # server, tools and search, not answers (the team's pipeline brings those).
      printf '%s\n' '{"answered": [], "unanswered": [], "instructionsContain": "knowledge base"}' \
        >".eks/teams/$TEAM/smoke.json"
    fi
    venv
    # A re-run updates the same stack: start from the last deployment.json.
    aws s3 cp --only-show-errors "$rec/deployment.json" deployment.json 2>/dev/null || true
    rc=0
    scripts/add-team.sh "${args[@]}" --summary /tmp/install.json || rc=$?
    # Whatever happened, keep what uninstall needs.
    [[ -f deployment.json ]] && aws s3 cp --only-show-errors deployment.json "$rec/deployment.json"
    [[ -f infra/outputs.json ]] && aws s3 cp --only-show-errors infra/outputs.json "$rec/outputs.json"
    [[ -f /tmp/install.json ]] && aws s3 cp --only-show-errors /tmp/install.json "$rec/install.json"
    ((rc == 0)) || exit "$rc"
    # Azure DevOps team: a new key for its pipeline user (the secret goes into the
    # stack's secret; the console shows it once), and the release its pipeline pins.
    EKS_KEY_ID=$("$PYTHON" platform/team.py key /tmp/request.json)
    EKS_TOOLS_SHA256=$(sha256sum /tmp/eks.tar.gz | cut -d' ' -f1)
    export EKS_KEY_ID EKS_TOOLS_SHA256
    if [[ -n $EKS_KEY_ID ]]; then
      printf '{"keyId": "%s", "shown": false}\n' "$EKS_KEY_ID" >/tmp/key.json
      aws s3 cp --only-show-errors /tmp/key.json "$rec/key.json"
    fi
    # The files the team commits (pipeline, deployment.json), before result.json
    # so the console never shows a download that is not there yet.
    files=$("$PYTHON" platform/team.py files /tmp/request.json /tmp)
    if [[ -n $files ]]; then
      (cd /tmp && "$PYTHON" -m zipfile -c /tmp/repo-files.zip repo-files)
      aws s3 cp --only-show-errors /tmp/repo-files.zip "$rec/repo-files.zip"
      echo "repo files: ${files//$'\n'/, }"
    fi
    if [[ -f /tmp/repo-files/ado-setup-$TEAM.sh ]]; then
      aws s3 cp --only-show-errors "/tmp/repo-files/ado-setup-$TEAM.sh" "$rec/ado-setup.sh"
    fi
    "$PYTHON" platform/team.py result /tmp/request.json /tmp/result.json
    aws s3 cp --only-show-errors /tmp/result.json "$rec/result.json"
    echo "team $TEAM ready"
    ;;

  delete)
    : "${TEAM:?}"
    rec="s3://$BUCKET/teams/$TEAM"
    if aws s3 cp --only-show-errors "$rec/deployment.json" deployment.json 2>/dev/null; then
      venv
      aws s3 cp --only-show-errors "$rec/outputs.json" infra/outputs.json 2>/dev/null || true
      scripts/uninstall.sh --summary /tmp/uninstall.json
    else
      echo "no deployment.json for $TEAM: nothing was deployed"
    fi
    aws s3 rm --only-show-errors --recursive "$rec/"
    echo "team $TEAM removed"
    ;;

  *)
    echo "ACTION: platform, create or delete" >&2
    exit 1
    ;;
esac
