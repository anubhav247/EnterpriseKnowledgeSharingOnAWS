#!/usr/bin/env bash
# Install the EKS platform in this AWS account and region: one command, run
# from AWS CloudShell (or any shell with the AWS CLI and admin rights).
#
#   eks-<version>/platform/install.sh --admin-email YOUR_EMAIL
#   eks-<version>/platform/install.sh --sign-in oidc --issuer https://... --client-id ...
#   eks-<version>/platform/install.sh --delete
#
# Flags: --region <region> (default: this shell's; in CloudShell the console's),
# --yes (no "Continue?" question). Every input is checked before any AWS change.
#
# It makes the CloudFormation stack eks-platform (console, API, build runner,
# admin sign-in), copies this release into the stack's bucket, starts the
# console build and returns. The heavy work (each team) runs later in
# CodeBuild, started from the console, so CloudShell is not held open.
# Safe to run again: an update with the same release changes nothing.
# Guide: docs/platform.md.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
cd "$HERE"

STACK=eks-platform
SIGN_IN=cognito
ADMIN_EMAIL="" ISSUER="" CLIENT_ID="" ADMIN_CLAIM="" TEAM_PREFIX=eks- BUNDLE="" CFN_ROLE="" DELETE=0
REGION="" YES=0
usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }
need() { [[ -n ${2:-} ]] || { echo "$1 needs a value" >&2; exit 1; }; }
while (($#)); do
  case "$1" in
    --admin-email) need "$1" "${2:-}"; ADMIN_EMAIL=$2; shift ;;
    --sign-in) need "$1" "${2:-}"; SIGN_IN=$2; shift ;;
    --issuer) need "$1" "${2:-}"; ISSUER=${2%/}; shift ;;
    --client-id) need "$1" "${2:-}"; CLIENT_ID=$2; shift ;;
    --admin-claim) need "$1" "${2:-}"; ADMIN_CLAIM=$2; shift ;;
    --stack) need "$1" "${2:-}"; STACK=$2; shift ;;
    --team-prefix) need "$1" "${2:-}"; TEAM_PREFIX=$2; shift ;;
    --bundle) need "$1" "${2:-}"; BUNDLE=$2; shift ;;
    --cfn-role-arn) need "$1" "${2:-}"; CFN_ROLE=$2; shift ;; # CI: CloudFormation runs as this role
    --region) need "$1" "${2:-}"; REGION=$2; shift ;;
    --yes) YES=1 ;; # no "Continue? [y/N]" question (CI)
    --delete) DELETE=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown flag $1 (--help)" >&2; exit 1 ;;
  esac
  shift
done
fail() { echo "$*" >&2; exit 1; }

# 1. Every input, before any AWS call: a typo must not leave anything behind.
# Patterns match platform/eks-platform.yaml's parameters.
[[ $STACK =~ ^[a-z][a-z0-9-]{2,30}$ ]] || fail "--stack: 3-31 lowercase letters, digits, -"
[[ $TEAM_PREFIX =~ ^[a-z][a-z0-9-]{1,14}-$ ]] || fail "--team-prefix: lowercase, ends with -, e.g. eks-"
[[ -z $ADMIN_CLAIM || $ADMIN_CLAIM =~ ^[A-Za-z0-9_.:-]+=[^[:space:]]+$ ]] || fail "--admin-claim: name=value"
if ((!DELETE)); then
  case "$SIGN_IN" in
    cognito)
      [[ -n $ADMIN_EMAIL ]] || fail "--admin-email: your e-mail; Cognito sends the first password there"
      [[ $ADMIN_EMAIL =~ ^[^@[:space:]]+@[^@[:space:]]+\.[A-Za-z]{2,}$ ]] ||
        fail "--admin-email: '$ADMIN_EMAIL' is not an e-mail address. Put your own e-mail in the command."
      # The placeholders in docs/platform.md, and the example domains: nobody gets that mail.
      if [[ ${ADMIN_EMAIL,,} =~ @example\.(com|org|net)$ ]]; then
        fail "--admin-email: '$ADMIN_EMAIL' is the example from the guide. Put your own e-mail in the command."
      fi
      ;;
    oidc)
      [[ -n $ISSUER && -n $CLIENT_ID ]] || fail "--sign-in oidc needs --issuer and --client-id"
      [[ $ISSUER =~ ^https://[^[:space:]?#]+$ ]] || fail "--issuer: an https URL"
      [[ $CLIENT_ID =~ ^[A-Za-z0-9_.:/@-]+$ ]] || fail "--client-id: the app's client id, never a secret"
      ;;
    *) fail "--sign-in: cognito or oidc" ;;
  esac
  # The release this script came from: eks-<version>/ next to eks-<version>.tar.gz.
  VERSION=$(python3 -c 'import json; print(json.load(open("RELEASE.json"))["version"])' 2>/dev/null || echo dev)
  BUNDLE=${BUNDLE:-$(dirname "$HERE")/eks-$VERSION.tar.gz}
  [[ -f $BUNDLE ]] || fail "Release file not found: $BUNDLE (--bundle <path>)"
  if [[ -f $BUNDLE.sha256 ]]; then
    (cd "$(dirname "$BUNDLE")" && sha256sum -c --quiet "$(basename "$BUNDLE").sha256")
  fi
fi

# 2. Region: --region, else the shell's (in CloudShell: the console's region).
REGION=${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}}
[[ -n $REGION ]] || fail "No AWS region: add --region <region>"
[[ $REGION =~ ^[a-z]{2}(-[a-z]+)+-[0-9]$ ]] || fail "--region: e.g. ap-south-1, not '$REGION'"
export AWS_REGION=$REGION AWS_DEFAULT_REGION=$REGION

# Regions with all four (AWS docs and a live check, 2026-11-23).
SUPPORTED="us-east-1 us-east-2 us-west-2 ca-central-1 eu-central-1 eu-west-1 eu-west-2 eu-west-3 eu-north-1 ap-south-1 ap-southeast-2 ap-northeast-1 ap-northeast-2"
region_ok() {
  local missing=()
  getent hosts "bedrock-agentcore-control.$REGION.amazonaws.com" >/dev/null || missing+=("AgentCore Runtime/Gateway")
  getent hosts "bedrock-agent.$REGION.amazonaws.com" >/dev/null || missing+=("Bedrock knowledge bases")
  getent hosts "s3vectors.$REGION.api.aws" >/dev/null || missing+=("S3 Vectors")
  local err
  if ! err=$(aws bedrock get-foundation-model --model-identifier amazon.titan-embed-text-v2:0 2>&1 >/dev/null); then
    if [[ $err == *AccessDenied* ]]; then
      echo "WARN: may not read Bedrock models here, Titan Text Embeddings V2 not checked" >&2
    else
      missing+=("Titan Text Embeddings V2")
    fi
  fi
  ((${#missing[@]} == 0)) && return 0
  echo "Region $REGION lacks: $(IFS=,; echo "${missing[*]}")." >&2
  echo "Pick one of: $SUPPORTED" >&2
  echo "In CloudShell, switch the console's region first (top right), then open CloudShell again." >&2
  return 1
}

ACCOUNT=$(aws sts get-caller-identity --query Account --output text) || fail "No AWS login in this shell"
((DELETE)) || region_ok || exit 1

# 3. Say where it goes, and ask.
if ((DELETE)); then
  echo "Delete EKS platform stack $STACK: account $ACCOUNT, region $REGION"
else
  echo "Install EKS ${VERSION}: account $ACCOUNT, region $REGION, stack $STACK"
  if [[ $SIGN_IN == cognito ]]; then echo "  console admin: $ADMIN_EMAIL"; fi
fi
if ((!YES)); then
  [[ -t 0 ]] || fail "Not a terminal: add --yes to go on without the question"
  read -r -p "Continue? [y/N] " answer
  [[ $answer =~ ^[Yy]([Ee][Ss])?$ ]] || fail "Stopped. Nothing changed."
fi
role_args=()
[[ -n $CFN_ROLE ]] && role_args=(--role-arn "$CFN_ROLE")
out() { aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

if ((DELETE)); then
  # Checked here first: a refused delete leaves the stack DELETE_FAILED, which
  # works on but can not be updated until the delete is retried.
  bucket=$(out Bucket 2>/dev/null || true)
  if [[ -n $bucket && $bucket != None ]]; then
    # `aws s3 ls` exits 1 on an empty prefix: no teams is not an error.
    teams=$({ aws s3 ls "s3://$bucket/teams/" 2>/dev/null || true; } | awk '{print $2}' | tr -d / | tr '\n' ' ')
    if [[ -n ${teams// /} ]]; then
      echo "Delete these teams in the EKS console first: $teams" >&2
      exit 1
    fi
  fi
  echo "Deleting stack $STACK in $ACCOUNT $REGION..."
  aws cloudformation delete-stack --stack-name "$STACK" "${role_args[@]}"
  if ! aws cloudformation wait stack-delete-complete --stack-name "$STACK"; then
    aws cloudformation describe-stack-events --stack-name "$STACK" --max-items 5 \
      --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].ResourceStatusReason" --output text >&2
    exit 1
  fi
  echo "Deleted $STACK."
  exit 0
fi


# Team stacks deploy with the CDK. Its one-time setup needs an admin, so it
# runs here, not in the console's runner.
if ! aws cloudformation describe-stacks --stack-name CDKToolkit >/dev/null 2>&1; then
  cdk_version=$(grep -E '^AWS_CDK_CLI_VERSION=' factory/toolchain/versions.env | cut -d= -f2)
  echo "1/4 CDK bootstrap (once per account and region, about 2 min)"
  npx --yes "aws-cdk@$cdk_version" bootstrap "aws://$ACCOUNT/$REGION"
else
  echo "1/4 CDK bootstrap: already done"
fi

echo "2/4 Stack $STACK (about 5 min, most of it CloudFront)"
if ! aws cloudformation deploy --stack-name "$STACK" --template-file platform/eks-platform.yaml \
  --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset "${role_args[@]}" \
  --tags eks-platform="$STACK" \
  --parameter-overrides SignIn="$SIGN_IN" AdminEmail="$ADMIN_EMAIL" OidcIssuer="$ISSUER" \
  OidcClientId="$CLIENT_ID" AdminClaim="$ADMIN_CLAIM" TeamPrefix="$TEAM_PREFIX"; then
  echo "Stack $STACK failed. First reasons:" >&2
  aws cloudformation describe-stack-events --stack-name "$STACK" --max-items 30 \
    --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason]" \
    --output text >&2 || true
  exit 1
fi

echo "3/4 Release $VERSION into the stack's bucket"
aws s3 cp --only-show-errors "$BUNDLE" "s3://$(out Bucket)/release/eks.tar.gz"

echo "4/4 Console page build (CodeBuild, about 3 min)"
build=$(aws codebuild start-build --project-name "$(out RunnerProject)" \
  --environment-variables-override name=ACTION,value=platform,type=PLAINTEXT \
  --query build.id --output text)
url=$(out ConsoleUrl)
status=IN_PROGRESS
for _ in $(seq 60); do # up to 10 min
  status=$(aws codebuild batch-get-builds --ids "$build" --query 'builds[0].buildStatus' --output text)
  [[ $status == IN_PROGRESS ]] || break
  sleep 10
done
if [[ $status != SUCCEEDED ]]; then
  echo "Console build $build: $status. Last log lines:" >&2
  read -r group stream < <(aws codebuild batch-get-builds --ids "$build" \
    --query 'builds[0].logs.[groupName,streamName]' --output text)
  [[ -n ${stream:-} && $stream != None ]] &&
    aws logs get-log-events --log-group-name "$group" --log-stream-name "$stream" --limit 30 \
      --query 'events[].message' --output text >&2
  echo "Re-run this command to try again." >&2
  exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' "$url" || true)
[[ $code == 200 ]] || echo "WARN: $url answers HTTP $code; wait a minute and reload." >&2

cat <<EOF

EKS console: $url
  ready (build $build)
EOF
if [[ $SIGN_IN == cognito ]]; then
  echo "  sign in as $ADMIN_EMAIL: Cognito e-mails you a temporary password now (check spam)."
else
  echo "  sign in with your company account. Your app's redirect URI must be $url"
fi
echo "Then: Add team. Remove everything later: delete the teams in the console, then $0 --region $REGION --delete"
