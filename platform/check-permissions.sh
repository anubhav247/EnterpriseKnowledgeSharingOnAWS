#!/usr/bin/env bash
# Can this AWS login install EKS (the platform stack and a team)? Run it in
# AWS CloudShell in the account, before platform/install.sh:
#
#   eks-<version>/platform/check-permissions.sh --region <region>
#
# It really makes one of each resource type EKS makes, then deletes them, so
# service control policies (SCPs) and permission boundaries count too; an IAM
# simulation alone misses SCPs. Small CloudFormation stacks <prefix>-<part>, one
# per part, side by side: one refusal does not hide the others. Plus a tiny
# Bedrock embedding call, the AgentCore and CDK set-up checks.
# Cost: about $0 (empty resources for a few minutes). No e-mail is sent.
# Exit 0 = everything passed, 1 = something is missing.
#
# Flags: --region <region> (default: this shell's), --prefix <name> (default
# eks-permcheck), --cfn-role-arn <arn> (CloudFormation runs as this role; CI).
# Guide: docs/install-permissions.md.
set -uo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)

PREFIX=eks-permcheck REGION="" CFN_ROLE=""
usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; }
need() { [[ -n ${2:-} ]] || { echo "$1 needs a value" >&2; exit 2; }; }
while (($#)); do
  case "$1" in
    --region) need "$1" "${2:-}"; REGION=$2; shift ;;
    --prefix) need "$1" "${2:-}"; PREFIX=$2; shift ;;
    --cfn-role-arn) need "$1" "${2:-}"; CFN_ROLE=$2; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown flag $1 (--help)" >&2; exit 2 ;;
  esac
  shift
done
fail() { echo "$*" >&2; exit 2; }
[[ $PREFIX =~ ^[a-z][a-z0-9-]{2,24}$ ]] || fail "--prefix: 3-25 lowercase letters, digits, -"
REGION=${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}}
[[ -n $REGION ]] || fail "No AWS region: add --region <region>"
[[ $REGION =~ ^[a-z]{2}(-[a-z]+)+-[0-9]$ ]] || fail "--region: e.g. ap-south-1, not '$REGION'"
export AWS_REGION=$REGION AWS_DEFAULT_REGION=$REGION AWS_PAGER=""
# The regions install.sh names, from install.sh itself.
SUPPORTED=$(sed -n 's/^SUPPORTED="\(.*\)"$/\1/p' "$HERE/platform/install.sh" 2>/dev/null)

WORK=$(mktemp -d)
RESULTS=() FAILS=0 PASSES=0
# row STATUS WHAT DETAIL [WHERE]: one short line now; the summary groups the
# FAIL and NOT TRIED rows with the same cause and counts the PASS rows.
row() {
  local detail=${3:-}
  [[ $1 == FAIL ]] && detail=$(short "$detail")
  printf '%-9s %-42s %s\n' "$1" "$2" "$detail"
  RESULTS+=("$1"$'\t'"$2"$'\t'"$detail"$'\t'"${4:-}")
  [[ $1 == FAIL ]] && FAILS=$((FAILS + 1))
  [[ $1 == PASS ]] && PASSES=$((PASSES + 1))
  return 0
}
# The AWS CLI's error, on one line, word for word.
oneline() { tr '\n' ' ' <"$1" | sed 's/^ *//; s/ *$//'; }
# One short line from an AWS refusal: its own words for the action and why,
# without request ids and links. Anything else: the first 200 characters.
DENIED='not authorized to perform: [A-Za-z0-9-]+:[A-Za-z0-9]+'
# Why: no policy allows it, or an explicit deny (a service control policy, a
# permissions boundary, ...): kept, it says who has to act.
BECAUSE='(because no [a-z-]+ policy allows|(because )?(with )?an explicit deny in an? [a-z -]+ policy)'
short() {
  local out
  if [[ $1 =~ $DENIED ]]; then
    out=${BASH_REMATCH[0]}
    [[ $1 =~ $BECAUSE ]] && out+=" (${BASH_REMATCH[0]})"
    [[ $1 == *"; needed for the Azure DevOps pipeline key" ]] && out+="; needed for the Azure DevOps pipeline key"
    echo "$out"
    return
  fi
  sed -E 's/Resource handler returned message: //g; s/ ?\(Service: [^)]*\)//g; s/ ?\(RequestToken: [^)]*\)//g;
    s#https?://[^ ]+##g; s/"+//g' <<<"$1" | cut -c1-200
}
role_args=()
[[ -n $CFN_ROLE ]] && role_args=(--role-arn "$CFN_ROLE")

# ---------------------------------------------------------------- 1. who
if ! who=$(aws sts get-caller-identity --query '[Account,Arn]' --output text 2>"$WORK/err"); then
  echo "No AWS login in this shell: $(oneline "$WORK/err")" >&2
  exit 1
fi
read -r ACCOUNT ARN <<<"$who"
echo "EKS permission check"
echo "  login:   $ARN"
echo "  account: $ACCOUNT"
echo "  region:  $REGION"
echo "  cost:    about \$0 (empty test resources for a few minutes, one tiny Bedrock call); no e-mail is sent"
echo

# ------------------------------------------------- 2. the probe templates
# One small template per part. Types are the ones platform/eks-platform.yaml
# and a team's CDK app (infra/stacks/) make. Names come from the stack name.
# vectors: S3 Vectors alone, so a login without IAM rights still shows it; kb
# makes its own index for the knowledge base, which needs an IAM role.
# adouser: the Azure DevOps pipeline's own IAM user, its group (path /eks-ado/)
# and an access key, made and deleted, as a team stack with Azure DevOps does.
PARTS=(iam s3 lambda api signin codebuild cloudfront secrets logs monitoring vectors kb gateway adouser cdk)
LAMBDA_TRUST='{Version: "2012-10-17", Statement: [{Effect: Allow, Principal: {Service: lambda.amazonaws.com}, Action: "sts:AssumeRole"}]}'
tpl() {
  echo 'AWSTemplateFormatVersion: "2010-09-09"'
  echo "Description: EKS permission check ($1). Safe to delete."
  echo 'Resources:'
  case "$1" in
    iam) cat <<EOF
  Role:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument: $LAMBDA_TRUST
      Policies:
        - PolicyName: inline
          PolicyDocument: {Version: "2012-10-17", Statement: [{Effect: Allow, Action: "logs:CreateLogStream", Resource: "*"}]}
  Policy:
    Type: AWS::IAM::Policy
    Properties:
      PolicyName: attached
      Roles: [!Ref Role]
      PolicyDocument: {Version: "2012-10-17", Statement: [{Effect: Allow, Action: "logs:PutLogEvents", Resource: "*"}]}
EOF
      ;;
    s3) cat <<'EOF'
  Bucket:
    Type: AWS::S3::Bucket
    Properties:
      PublicAccessBlockConfiguration: {BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true}
  BucketPolicy:
    Type: AWS::S3::BucketPolicy
    Properties:
      Bucket: !Ref Bucket
      PolicyDocument:
        Version: "2012-10-17"
        Statement:
          - {Effect: Deny, Principal: "*", Action: "s3:*", Resource: [!GetAtt Bucket.Arn, !Sub "${Bucket.Arn}/*"], Condition: {Bool: {"aws:SecureTransport": "false"}}}
EOF
      ;;
    lambda) cat <<EOF
  Role:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument: $LAMBDA_TRUST
  Function:
    Type: AWS::Lambda::Function
    Properties:
      Runtime: python3.12
      Handler: index.handler
      Role: !GetAtt Role.Arn
      Code: {ZipFile: "def handler(event, context):\n    return 'ok'\n"}
  Permission:
    Type: AWS::Lambda::Permission
    Properties:
      FunctionName: !Ref Function
      Action: lambda:InvokeFunction
      Principal: apigateway.amazonaws.com
      SourceAccount: !Ref AWS::AccountId
EOF
      ;;
    api) cat <<'EOF'
  Api:
    Type: AWS::ApiGatewayV2::Api
    Properties: {Name: !Ref AWS::StackName, ProtocolType: HTTP}
  Stage:
    Type: AWS::ApiGatewayV2::Stage
    Properties: {ApiId: !Ref Api, StageName: $default, AutoDeploy: true}
EOF
      ;;
    # The user is made with MessageAction SUPPRESS (AdminCreateUser, no e-mail).
    signin) cat <<'EOF'
  Pool:
    Type: AWS::Cognito::UserPool
    Properties: {UserPoolName: !Ref AWS::StackName}
  Client:
    Type: AWS::Cognito::UserPoolClient
    Properties: {UserPoolId: !Ref Pool, GenerateSecret: false}
  Domain:
    Type: AWS::Cognito::UserPoolDomain
    Properties: {UserPoolId: !Ref Pool, Domain: !Sub "${AWS::StackName}-${AWS::AccountId}"}
  User:
    Type: AWS::Cognito::UserPoolUser
    Properties: {UserPoolId: !Ref Pool, Username: permcheck-probe, MessageAction: SUPPRESS}
EOF
      ;;
    codebuild) cat <<'EOF'
  Role:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument: {Version: "2012-10-17", Statement: [{Effect: Allow, Principal: {Service: codebuild.amazonaws.com}, Action: "sts:AssumeRole"}]}
  Project:
    Type: AWS::CodeBuild::Project
    Properties:
      ServiceRole: !GetAtt Role.Arn
      Source: {Type: NO_SOURCE, BuildSpec: "version: 0.2\nphases:\n  build:\n    commands: [echo ok]\n"}
      Artifacts: {Type: NO_ARTIFACTS}
      Environment: {Type: LINUX_CONTAINER, ComputeType: BUILD_GENERAL1_SMALL, Image: "aws/codebuild/amazonlinux-x86_64-standard:5.0"}
EOF
      ;;
    # A distribution takes 15+ minutes to make and delete: the origin access
    # control (also CloudFront, seconds) stands in for it.
    cloudfront) cat <<'EOF'
  Oac:
    Type: AWS::CloudFront::OriginAccessControl
    Properties:
      OriginAccessControlConfig: {Name: !Sub "${AWS::StackName}-${AWS::Region}", OriginAccessControlOriginType: s3, SigningBehavior: always, SigningProtocol: sigv4}
EOF
      ;;
    secrets) cat <<'EOF'
  Secret:
    Type: AWS::SecretsManager::Secret
    Properties: {Description: EKS permission check, GenerateSecretString: {}}
EOF
      ;;
    logs) cat <<'EOF'
  LogGroup:
    Type: AWS::Logs::LogGroup
    Properties: {RetentionInDays: 1}
EOF
      ;;
    monitoring) cat <<'EOF'
  Topic:
    Type: AWS::SNS::Topic
  Rule:
    Type: AWS::Events::Rule
    Properties: {ScheduleExpression: rate(1 day), State: DISABLED}
  Alarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      Namespace: AWS/SNS
      MetricName: NumberOfMessagesPublished
      Dimensions: [{Name: TopicName, Value: !GetAtt Topic.TopicName}]
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      AlarmActions: [!Ref Topic]
  Dashboard:
    Type: AWS::CloudWatch::Dashboard
    Properties:
      DashboardName: !Sub "${AWS::StackName}-${AWS::Region}"
      DashboardBody: '{"widgets":[{"type":"text","x":0,"y":0,"width":6,"height":2,"properties":{"markdown":"EKS permission check"}}]}'
EOF
      ;;
    vectors) cat <<'EOF'
  VectorBucket:
    Type: AWS::S3Vectors::VectorBucket
    Properties: {VectorBucketName: !Sub "${AWS::StackName}-${AWS::AccountId}"}
  Index:
    Type: AWS::S3Vectors::Index
    Properties:
      VectorBucketArn: !GetAtt VectorBucket.VectorBucketArn
      IndexName: probe
      DataType: float32
      Dimension: 1024
      DistanceMetric: cosine
EOF
      ;;
    kb) cat <<'EOF'
  VectorBucket:
    Type: AWS::S3Vectors::VectorBucket
    Properties: {VectorBucketName: !Sub "${AWS::StackName}-${AWS::AccountId}"}
  Index:
    Type: AWS::S3Vectors::Index
    Properties:
      VectorBucketArn: !GetAtt VectorBucket.VectorBucketArn
      IndexName: probe
      DataType: float32
      Dimension: 1024
      DistanceMetric: cosine
      MetadataConfiguration: {NonFilterableMetadataKeys: [AMAZON_BEDROCK_TEXT, AMAZON_BEDROCK_METADATA]}
  Role:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement: [{Effect: Allow, Principal: {Service: bedrock.amazonaws.com}, Action: "sts:AssumeRole", Condition: {StringEquals: {"aws:SourceAccount": !Ref AWS::AccountId}}}]
  RolePolicy:
    Type: AWS::IAM::Policy
    Properties:
      PolicyName: kb
      Roles: [!Ref Role]
      PolicyDocument:
        Version: "2012-10-17"
        Statement:
          - {Effect: Allow, Action: "bedrock:InvokeModel", Resource: !Sub "arn:${AWS::Partition}:bedrock:${AWS::Region}::foundation-model/amazon.titan-embed-text-v2:0"}
          - {Effect: Allow, Action: ["s3vectors:GetIndex", "s3vectors:QueryVectors", "s3vectors:PutVectors", "s3vectors:GetVectors", "s3vectors:DeleteVectors", "s3vectors:ListVectors"], Resource: !GetAtt Index.IndexArn}
  KnowledgeBase:
    Type: AWS::Bedrock::KnowledgeBase
    DependsOn: RolePolicy
    Properties:
      Name: !Ref AWS::StackName
      RoleArn: !GetAtt Role.Arn
      KnowledgeBaseConfiguration:
        Type: VECTOR
        VectorKnowledgeBaseConfiguration:
          EmbeddingModelArn: !Sub "arn:${AWS::Partition}:bedrock:${AWS::Region}::foundation-model/amazon.titan-embed-text-v2:0"
          EmbeddingModelConfiguration: {BedrockEmbeddingModelConfiguration: {Dimensions: 1024, EmbeddingDataType: FLOAT32}}
      StorageConfiguration: {Type: S3_VECTORS, S3VectorsConfiguration: {IndexArn: !GetAtt Index.IndexArn}}
EOF
      ;;
    # EKS's own CDK set-up (stack kbmcp-cdk-toolkit, made by install.sh): one
    # of each of its resource types, with cdk-kbmcp-* names. Step 3 also
    # updates this stack's tags, as a re-run of the set-up would.
    cdk) cat <<'EOF'
  Bucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Sub "cdk-kbmcp-permcheck-${AWS::AccountId}-${AWS::Region}"
      VersioningConfiguration: {Status: Enabled}
      PublicAccessBlockConfiguration: {BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true}
  Repo:
    Type: AWS::ECR::Repository
    Properties: {RepositoryName: !Sub "cdk-kbmcp-permcheck-${AWS::AccountId}-${AWS::Region}", EmptyOnDelete: true}
  Version:
    Type: AWS::SSM::Parameter
    Properties: {Name: /cdk-bootstrap/kbmcp/permcheck, Type: String, Value: "1"}
  Role:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub "cdk-kbmcp-permcheck-role-${AWS::AccountId}-${AWS::Region}"
      AssumeRolePolicyDocument: {Version: "2012-10-17", Statement: [{Effect: Allow, Principal: {AWS: !Ref AWS::AccountId}, Action: "sts:AssumeRole"}]}
EOF
      ;;
    adouser) cat <<'EOF'
  Group:
    Type: AWS::IAM::Group
    Properties: {Path: /eks-ado/}
  User:
    Type: AWS::IAM::User
    Properties: {Path: /eks-ado/, Groups: [!Ref Group]}
  Key:
    Type: AWS::IAM::AccessKey
    Properties: {UserName: !Ref User}
EOF
      ;;
    # The AgentCore runtime needs a container image (minutes to build): the
    # gateway, the same service, stands in; step 3 lists runtimes.
    gateway) cat <<'EOF'
  Role:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument: {Version: "2012-10-17", Statement: [{Effect: Allow, Principal: {Service: bedrock-agentcore.amazonaws.com}, Action: "sts:AssumeRole"}]}
  Gateway:
    Type: AWS::BedrockAgentCore::Gateway
    Properties: {Name: !Ref AWS::StackName, RoleArn: !GetAtt Role.Arn, ProtocolType: MCP, AuthorizerType: AWS_IAM}
EOF
      ;;
  esac
}

STACKS=()
for p in "${PARTS[@]}"; do STACKS+=("$PREFIX-$p"); done
undo() {
  echo "  for s in ${STACKS[*]}; do aws cloudformation delete-stack --stack-name \$s --region $REGION; done"
}
status_of() { aws cloudformation describe-stacks --stack-name "$1" --query 'Stacks[0].StackStatus' --output text 2>/dev/null; }
# Logical ids whose delete was refused but that were never made (CREATE_FAILED,
# no CREATE_COMPLETE): e.g. the IAM role a login without IAM rights could not
# make, and so can not "delete" either. Nothing exists to keep.
never_made() {
  local id failed made
  failed=$(aws cloudformation describe-stack-resources --stack-name "$1" --output text \
    --query "StackResources[?ResourceStatus=='DELETE_FAILED'].LogicalResourceId" 2>/dev/null)
  made=$(aws cloudformation describe-stack-events --stack-name "$1" --output text \
    --query "StackEvents[?ResourceStatus=='CREATE_COMPLETE'].LogicalResourceId" 2>/dev/null)
  for id in $failed; do [[ " ${made//$'\t'/ } " == *" $id "* ]] || echo "$id"; done
}
# Delete one stack and wait; a DELETE_FAILED stack whose stuck resources were
# never made is deleted again, keeping (retaining) them. 0 = gone.
delete_stack() {
  local keep=()
  [[ -n $(status_of "$1") ]] || return 0
  aws cloudformation delete-stack --stack-name "$1" "${role_args[@]}" 2>/dev/null
  aws cloudformation wait stack-delete-complete --stack-name "$1" 2>/dev/null && return 0
  [[ $(status_of "$1") == DELETE_FAILED ]] || return 1
  mapfile -t keep < <(never_made "$1")
  ((${#keep[@]})) || return 1
  aws cloudformation delete-stack --stack-name "$1" --retain-resources "${keep[@]}" "${role_args[@]}" 2>/dev/null &&
    aws cloudformation wait stack-delete-complete --stack-name "$1" 2>/dev/null
}
# Why a stack is still there: its first DELETE_FAILED resource, in one line.
stuck_reason() {
  local r
  r=$(aws cloudformation describe-stack-events --stack-name "$1" --output text \
    --query "StackEvents[?ResourceStatus=='DELETE_FAILED'] | [0].[LogicalResourceId,ResourceStatusReason]" 2>/dev/null)
  echo "${r%%$'\t'*}: $(short "${r#*$'\t'}")"
}

# Delete every probe stack (and the ECR / SSM probes), wait. Runs on any exit,
# Ctrl-C included.
CLEANED=0
cleanup() {
  local rc=$? s left=()
  ((CLEANED)) && return
  CLEANED=1
  trap '' INT TERM
  echo
  echo "Clean-up: deleting the test stacks (1-3 min)..."
  for s in "${STACKS[@]}"; do
    [[ -n $(status_of "$s") ]] && aws cloudformation delete-stack --stack-name "$s" "${role_args[@]}" 2>/dev/null
  done
  for s in "${STACKS[@]}"; do
    delete_stack "$s" || left+=("$s")
  done
  if ((${#left[@]})); then
    for s in "${left[@]}"; do echo "NOT deleted: $s ($(stuck_reason "$s"))" >&2; done
    echo "Delete them with:" >&2
    undo >&2
    rc=1
  else
    echo "Clean-up: done, nothing left."
  fi
  rm -rf "$WORK"
  exit "$rc"
}
trap cleanup EXIT
trap 'echo; echo "Stopped (Ctrl-C)."; exit 130' INT TERM

echo "If this is stopped hard, delete the test stacks with:"
undo
echo

# An earlier, stopped run: remove its stacks first.
for s in "${STACKS[@]}"; do
  if [[ -n $(status_of "$s") ]]; then
    echo "Removing an earlier test stack $s..."
    delete_stack "$s" || echo "  could not: $(stuck_reason "$s")"
  fi
done

echo "1/3 Make and delete one of each resource type (about 3 min)"
started=()
for p in "${PARTS[@]}"; do
  tpl "$p" >"$WORK/$p.yaml"
  if aws cloudformation create-stack --stack-name "$PREFIX-$p" --template-body "file://$WORK/$p.yaml" \
    --capabilities CAPABILITY_NAMED_IAM --on-failure DO_NOTHING \
    --tags Key=eks-permcheck,Value=1 Key=kbmcp:managed,Value=true Key=kbmcp:stack,Value="$PREFIX-$p" \
    "${role_args[@]}" >/dev/null 2>"$WORK/err"; then
    started+=("$p")
  else
    row FAIL "$p: cloudformation:CreateStack" "$(oneline "$WORK/err")"
  fi
done
pending=("${started[@]}")
for _ in $(seq 120); do # up to 20 min
  ((${#pending[@]})) || break
  sleep 10
  next=()
  for p in "${pending[@]}"; do
    [[ $(status_of "$PREFIX-$p") == *_IN_PROGRESS ]] && next+=("$p")
  done
  pending=("${next[@]}")
done
for p in "${pending[@]}"; do row FAIL "$p: stack" "still creating after 20 min"; done
for p in "${started[@]}"; do
  aws cloudformation describe-stack-events --stack-name "$PREFIX-$p" --output text \
    --query "reverse(StackEvents[?ResourceType!='AWS::CloudFormation::Stack' && (ResourceStatus=='CREATE_COMPLETE' || ResourceStatus=='CREATE_FAILED')].[ResourceType,ResourceStatus,ResourceStatusReason])" \
    >"$WORK/events" 2>"$WORK/err" || { row FAIL "$p: read stack events" "$(oneline "$WORK/err")"; continue; }
  while IFS=$'\t' read -r type st reason; do
    if [[ $st == CREATE_COMPLETE && $type == AWS::Cognito::UserPoolUser ]]; then
      row PASS "$type" "AdminCreateUser, MessageAction SUPPRESS (no e-mail)"
    elif [[ $st == CREATE_COMPLETE && $p == adouser ]]; then
      row PASS "$type" "made in $PREFIX-$p; needed for the Azure DevOps pipeline key" "$p"
    elif [[ $st == CREATE_COMPLETE ]]; then
      row PASS "$type" "made in $PREFIX-$p" "$p"
    elif [[ $reason == *"creation cancelled"* ]]; then
      row "NOT TRIED" "$type" "cancelled: another resource in its stack failed" "$p"
    elif [[ $p == adouser ]]; then
      row FAIL "$type" "$reason; needed for the Azure DevOps pipeline key" "$p"
    else
      row FAIL "$type" "$reason" "$p"
    fi
  done <"$WORK/events"
done
row INFO "AWS::CloudFront::Distribution" "not made (15+ min); the origin access control stands in"
row INFO "AWS::BedrockAgentCore::Runtime" "not made (needs a container image); the gateway stands in"

echo
echo "2/3 Bedrock model and AgentCore in $REGION"
if aws bedrock-runtime invoke-model --model-id amazon.titan-embed-text-v2:0 \
  --content-type application/json --accept application/json --cli-binary-format raw-in-base64-out \
  --body '{"inputText":"EKS permission check","dimensions":256}' "$WORK/embed.json" >/dev/null 2>"$WORK/err"; then
  row PASS "Bedrock: Titan Text Embeddings V2" "one embedding call answered"
else
  row FAIL "Bedrock: Titan Text Embeddings V2" "$(oneline "$WORK/err")"
fi
if aws bedrock-agentcore-control list-agent-runtimes --max-results 1 >/dev/null 2>"$WORK/err"; then
  row PASS "AgentCore: list runtimes"
elif grep -q "Invalid choice" "$WORK/err"; then # an older AWS CLI
  if getent hosts "bedrock-agentcore-control.$REGION.amazonaws.com" >/dev/null; then
    row PASS "AgentCore: in this region" "endpoint found (this AWS CLI has no agentcore commands)"
  else
    row FAIL "AgentCore: in this region" "no endpoint bedrock-agentcore-control.$REGION.amazonaws.com"
  fi
else
  row FAIL "AgentCore: list runtimes" "$(oneline "$WORK/err")"
fi
getent hosts "s3vectors.$REGION.api.aws" >/dev/null &&
  row PASS "S3 Vectors: in this region" ||
  row FAIL "S3 Vectors: in this region" "no endpoint s3vectors.$REGION.api.aws"

echo
echo "3/3 EKS CDK set-up (team installs run cdk deploy through it)"
# Step 1 made the cdk-kbmcp-* probe (S3, ECR, SSM, IAM role); now an update,
# as a re-run of the set-up does. The delete is the clean-up's.
if [[ $(status_of "$PREFIX-cdk") == CREATE_COMPLETE ]]; then
  if aws cloudformation update-stack --stack-name "$PREFIX-cdk" --use-previous-template \
    --capabilities CAPABILITY_NAMED_IAM \
    --tags Key=eks-permcheck,Value=1 Key=kbmcp:managed,Value=true Key=kbmcp:stack,Value="$PREFIX-cdk" Key=kbmcp:probe,Value=updated \
    "${role_args[@]}" >/dev/null 2>"$WORK/err" &&
    aws cloudformation wait stack-update-complete --stack-name "$PREFIX-cdk" 2>>"$WORK/err"; then
    row PASS "EKS CDK set-up: update" "tags changed on the cdk-kbmcp-* probe"
  else
    row FAIL "EKS CDK set-up: update" "$(oneline "$WORK/err")"
  fi
else
  row FAIL "EKS CDK set-up: update" "not tried: the cdk-kbmcp-* probe was not made (above)"
fi
if own=$(status_of kbmcp-cdk-toolkit) && [[ -n $own ]]; then
  v=$(aws ssm get-parameter --name /cdk-bootstrap/kbmcp/version --query Parameter.Value --output text 2>/dev/null || echo "?")
  row INFO "EKS CDK set-up (stack kbmcp-cdk-toolkit)" "$own, version $v"
else
  row INFO "EKS CDK set-up (stack kbmcp-cdk-toolkit)" "not made yet: install.sh makes it"
fi
# Another team's CDK set-up: read, never changed.
if cdk=$(status_of CDKToolkit) && [[ -n $cdk ]]; then
  row INFO "CDKToolkit (someone else's) present" "$cdk: will not be touched"
fi

echo
echo "================ Summary: $ARN, account $ACCOUNT, $REGION"
# FAIL and NOT TRIED, one line per cause (with how many and where), then the
# PASS count. The lines above have every check.
printf '%s\n' "${RESULTS[@]}" | awk -F'\t' '
  $1 == "FAIL" || $1 == "NOT TRIED" {
    k = $1 FS $2 FS $3
    if (!(k in n)) order[++len] = k
    n[k]++
    if ($4 != "") where[k] = where[k] (where[k] == "" ? "" : ", ") $4
  }
  END {
    for (i = 1; i <= len; i++) {
      split(order[i], f, FS)
      what = f[2] (n[order[i]] > 1 ? " x" n[order[i]] : "") (where[order[i]] != "" ? " (" where[order[i]] ")" : "")
      printf "%-9s %-42s %s\n", f[1], what, f[3]
    }
  }'
echo "PASS: $PASSES checks"
if ((FAILS)); then
  echo "RESULT: $FAILS check(s) FAILED. This login can not install EKS here yet."
  [[ -n $SUPPORTED ]] && echo "Regions EKS supports: $SUPPORTED"
else
  echo "RESULT: all checks passed. This login can install EKS in $ACCOUNT $REGION."
fi
echo "Send this output to the person who sent you this command."
((FAILS == 0))
