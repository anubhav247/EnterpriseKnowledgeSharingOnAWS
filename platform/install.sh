#!/usr/bin/env bash
# Install the EKS platform in this AWS account and region: one command, run
# from AWS CloudShell (or any shell with the AWS CLI and admin rights).
#
#   eks-<version>/platform/install.sh --admin-email you@example.com
#   eks-<version>/platform/install.sh --sign-in oidc --issuer https://... --client-id ...
#   eks-<version>/platform/install.sh --delete
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
usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }
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
    --delete) DELETE=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown flag $1 (--help)" >&2; exit 1 ;;
  esac
  shift
done
[[ $STACK =~ ^[a-z][a-z0-9-]{2,30}$ ]] || { echo "--stack: 3-31 lowercase letters, digits, -" >&2; exit 1; }

REGION=${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}
[[ -n $REGION ]] || { echo "No AWS region: export AWS_REGION=<region>" >&2; exit 1; }
export AWS_REGION=$REGION AWS_DEFAULT_REGION=$REGION
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
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

case "$SIGN_IN" in
  cognito) [[ -n $ADMIN_EMAIL ]] || { echo "--admin-email: who gets the first console sign-in" >&2; exit 1; } ;;
  oidc) [[ -n $ISSUER && -n $CLIENT_ID ]] || { echo "--sign-in oidc needs --issuer and --client-id" >&2; exit 1; } ;;
  *) echo "--sign-in: cognito or oidc" >&2; exit 1 ;;
esac

# The release this script came from: eks-<version>/ next to eks-<version>.tar.gz.
VERSION=$(python3 -c 'import json; print(json.load(open("RELEASE.json"))["version"])')
BUNDLE=${BUNDLE:-$(dirname "$HERE")/eks-$VERSION.tar.gz}
[[ -f $BUNDLE ]] || { echo "Release file not found: $BUNDLE (--bundle <path>)" >&2; exit 1; }
if [[ -f $BUNDLE.sha256 ]]; then
  (cd "$(dirname "$BUNDLE")" && sha256sum -c --quiet "$(basename "$BUNDLE").sha256")
fi
echo "EKS $VERSION -> account $ACCOUNT, region $REGION, stack $STACK"

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

echo "4/4 Console page build (CodeBuild, about 3 min; this shell is free now)"
build=$(aws codebuild start-build --project-name "$(out RunnerProject)" \
  --environment-variables-override name=ACTION,value=platform,type=PLAINTEXT \
  --query build.id --output text)

url=$(out ConsoleUrl)
cat <<EOF

EKS console: $url
  ready in about 3 minutes (build $build)
EOF
if [[ $SIGN_IN == cognito ]]; then
  echo "  sign in as $ADMIN_EMAIL: Cognito e-mails you a temporary password (check spam)."
else
  echo "  sign in with your company account. Your app's redirect URI must be $url"
fi
echo "Then: Add team. Remove everything later: delete the teams in the console, then $0 --delete"
