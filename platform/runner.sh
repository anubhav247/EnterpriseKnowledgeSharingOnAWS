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
# uninstall needs), result.json (what the console shows), ado-files.zip.
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
    aws s3 sync --delete --only-show-errors "$dist" "s3://$CONSOLE_BUCKET/" \
      --cache-control "no-cache"
    if [[ -n "${DISTRIBUTION_ID:-}" ]]; then
      inv=$(aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" --paths "/*" \
        --query Invalidation.Id --output text)
      aws cloudfront wait invalidation-completed --distribution-id "$DISTRIBUTION_ID" --id "$inv"
    fi
    echo "console page published"
    # Cognito sign-in: invite the first admin now that the page is live (once).
    # Cognito e-mails the temporary password; nothing here sees it.
    if [[ -n "${USER_POOL_ID:-}" && -n "${ADMIN_EMAIL:-}" ]]; then
      if aws cognito-idp admin-get-user --user-pool-id "$USER_POOL_ID" --username "$ADMIN_EMAIL" >/dev/null 2>&1; then
        echo "admin $ADMIN_EMAIL already invited"
      else
        aws cognito-idp admin-create-user --user-pool-id "$USER_POOL_ID" --username "$ADMIN_EMAIL" \
          --user-attributes Name=email,Value="$ADMIN_EMAIL" Name=email_verified,Value=true \
          --desired-delivery-mediums EMAIL --query User.UserStatus --output text
        echo "admin $ADMIN_EMAIL invited: Cognito e-mails the temporary password"
      fi
    fi
    ;;

  create)
    : "${TEAM:?}" "${RUNNER_ROLE_ARN:?}"
    rec="s3://$BUCKET/teams/$TEAM"
    aws s3 cp --only-show-errors "$rec/request.json" /tmp/request.json
    mapfile -t args < <(python3 platform/team.py args /tmp/request.json)
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
    "$PYTHON" platform/team.py result /tmp/request.json /tmp/result.json
    aws s3 cp --only-show-errors /tmp/result.json "$rec/result.json"
    if [[ -d .eks/teams/$TEAM-ado ]]; then
      (cd ".eks/teams/$TEAM-ado" && python3 -m zipfile -c /tmp/ado-files.zip ./*)
      aws s3 cp --only-show-errors /tmp/ado-files.zip "$rec/ado-files.zip"
    fi
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
