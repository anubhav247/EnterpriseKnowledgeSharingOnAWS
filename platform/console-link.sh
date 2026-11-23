#!/usr/bin/env bash
# Print a five-minute, one-use EKS console link for this AWS identity.
set -euo pipefail

STACK=eks-platform
REGION=""
usage() {
  echo "Usage: $0 [--stack NAME] [--region REGION]"
}
need() { [[ -n ${2:-} ]] || { echo "$1 needs a value" >&2; exit 1; }; }
while (($#)); do
  case "$1" in
    --stack) need "$1" "${2:-}"; STACK=$2; shift ;;
    --region) need "$1" "${2:-}"; REGION=$2; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown flag $1 (--help)" >&2; exit 1 ;;
  esac
  shift
done

[[ $STACK =~ ^[a-z][a-z0-9-]{2,30}$ ]] || { echo "--stack: 3-31 lowercase letters, digits, -" >&2; exit 1; }
REGION=${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}}
[[ $REGION =~ ^[a-z]{2}(-[a-z]+)+-[0-9]$ ]] || { echo "--region: e.g. ap-south-1, not '$REGION'" >&2; exit 1; }
export AWS_REGION=$REGION AWS_DEFAULT_REGION=$REGION

endpoint=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ConsoleMintUrl'].OutputValue | [0]" --output text)
[[ $endpoint == https://* ]] || { echo "Stack $STACK has no console link endpoint" >&2; exit 1; }

# curl signs the request. AWS CLI exports its same short-lived credentials,
# including CloudShell and SSO role credentials.
eval "$(aws configure export-credentials --format env)"
headers=()
[[ -n ${AWS_SESSION_TOKEN:-} ]] && headers=(-H "x-amz-security-token: $AWS_SESSION_TOKEN")
body=$(curl --silent --show-error --fail-with-body --request POST \
  --aws-sigv4 "aws:amz:$REGION:execute-api" \
  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" "${headers[@]}" "$endpoint")
url=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])' <<<"$body")
[[ $url == https://*#code=* ]] || { echo "Endpoint returned no console link" >&2; exit 1; }

printf '%s\nvalid 5 min, once\n' "$url"
