# Install permissions: who needs what

The install makes IAM roles, buckets, a Bedrock knowledge base, an AgentCore
runtime and gateway, through CloudFormation (CDK). In a customer account the
person who runs it is often not an admin. This page says which AWS rights each
person needs, and how the install checks them before it changes anything.

## Who needs what

| Person | Path A: admin does it all | Path B: split |
|---|---|---|
| Cloud admin (account owner) | nothing | once per account and region: deploy [`factory/infra/customer/eks-installer-role.yaml`](../factory/infra/customer/eks-installer-role.yaml), then run `cdk bootstrap` with its execution policy (10 min) |
| Team admin (runs `add-team.sh` / `install.sh`) | `AdministratorAccess`, or equal | may assume the role `eks-installer`; no other AWS rights |
| Team admin, other tools | `az` / `gh` / `glab` login for the team repo | same |
| Publishers and readers | no AWS rights (they use the repo and Kiro) | same |

A GitHub repo URL (`--content https://github.com/...`) also makes a GitHub deploy role in AWS
(`cicd/github/deploy-role.yaml`). That needs IAM rights, so in path B the
cloud admin deploys it, or the team uses Azure DevOps or GitLab
.

## Step 0: the permissions check

`scripts/add-team.sh` and `scripts/install.sh` run
[`infra/permissions.py`](../infra/permissions.py) first, before anything
changes in AWS or on the Git host. It is read-only:

1. `sts:GetCallerIdentity`: who you are.
2. `iam:GetRole` on your own role (an SSO role has a path), then
   `iam:SimulatePrincipalPolicy`: may you do each action the install will do?
   This takes your policies, permission boundary and SCPs into account.
3. It prints one line per need and exits.

```text
AWS permissions check for arn:aws:sts::123456789012:assumed-role/ReadOnly/me in 123456789012 us-east-1 (read-only)
[PASS] read checks                             infra/preflight.py and the bootstrap check
[FAIL] CDK bootstrap: CloudFormation           missing cloudformation:CreateChangeSet, cloudformation:ExecuteChangeSet
[FAIL] CDK bootstrap: IAM roles                missing iam:AttachRolePolicy, iam:CreateRole, iam:PutRolePolicy, iam:TagRole
...
[FAIL] CDK deploy (assume bootstrap roles)     missing sts:AssumeRole
[PASS] load content: docs bucket               infra/load_content.py lists the documents
[FAIL] load content: documents                 missing s3:DeleteObject, s3:PutObject
...
22 action(s) missing. Nothing was changed.
Ask your AWS cloud admin for the missing actions, or to set up path B (one-time stack factory/infra/customer/eks-installer-role.yaml). See docs/install-permissions.md.
```

| Exit | Means |
|---|---|
| 0 | every need passes; the install goes on |
| 1 | something is missing: the list says what; ask your cloud admin, or use path B |
| 2 | could not check: your login may not call `iam:SimulatePrincipalPolicy` (some SSO roles). It then runs read-only probes (CloudFormation, Bedrock, IAM reads, `sts:AssumeRole` on the CDK roles) and marks every write `UNKNOWN`. It never guesses a pass. Ask for `iam:SimulatePrincipalPolicy` and `iam:GetRole` on your own role, or rerun with `--skip-permissions` if you know you are an admin. |

What it checks: the reads preflight does; `cdk bootstrap` (only when the
region has no `CDKToolkit` stack); `sts:AssumeRole` on the four CDK roles
(deploy, file-publishing, image-publishing, lookup), which is how `cdk deploy`
works; the GitHub deploy role stack (a GitHub repo URL); and the content load:
either `sts:AssumeRole` on the stack's content role (path B) or the S3,
ingestion and smoke-test calls (path A).

Run it alone: `python infra/permissions.py` (reads `deployment.json`).

## Path A: admin

The team admin has `AdministratorAccess` (or the actions the check lists).
Nothing to set up. `cdk bootstrap` runs with the default execution policy,
which is `AdministratorAccess` for CloudFormation.

## Path B: split

### Cloud admin, once per account and region

```bash
aws cloudformation deploy --stack-name eks-installer \
  --template-file factory/infra/customer/eks-installer-role.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides InstallerPrincipalArn=<team admin role or user ARN>
aws cloudformation describe-stacks --stack-name eks-installer \
  --query 'Stacks[0].Outputs' --output table
# then the BootstrapCommand output, for example:
npx aws-cdk@2.1149.0 bootstrap aws://<account>/<region> \
  --cloudformation-execution-policies arn:aws:iam::<account>:policy/eks-cfn-execution
```

The stack makes three things:

| Part | Name | What it allows |
|---|---|---|
| Execution policy | `eks-cfn-execution` | what CloudFormation may do for an EKS stack. It replaces `AdministratorAccess` on the CDK execution role. |
| Role boundary | `eks-role-boundary` | the most any role an EKS stack makes can ever do |
| Installer role | `eks-installer` | assume the CDK roles and the stack's content roles; reads; check itself; clean up its prefix on uninstall |

Parameters: `ResourcePrefix` (default `eks-`: every team prefix must start
with it; `add-team.sh` uses `eks-<team>`), `InstallerRoleName`,
`InstallerPrincipalArn` (empty = any principal of the account whose own policy
allows it), `CdkQualifier`.

The account is already bootstrapped with the default policy? Run the
bootstrap command above again: it updates the execution role in place.

### Team admin

The cloud admin is in another team? The setup page's **IT does it** for AWS
writes this section as a ticket ([it-request-packs.md](it-request-packs.md)).

Sign in as the installer role (for example a profile with
`role_arn = <InstallerRoleArn>`), then add two flags to the command the setup
page prints:

```bash
AWS_PROFILE=eks-installer scripts/add-team.sh --team legal ... \
  --ci-trust-role-arn <InstallerRoleArn> --permissions-boundary eks-role-boundary
```

`--ci-trust-role-arn` makes the stack's content role trust the installer, so
the install loads documents and runs the smoke test as that role.
`--permissions-boundary` puts `eks-role-boundary` on every role the stack
makes; the execution policy refuses a role without it.

## The policy, and its trade-offs

How it was made: from what the stacks create (`cdk synth` of every option:
Bedrock knowledge base and data source, S3 and S3 Vectors buckets, AgentCore
runtime, gateway and target, Lambda functions, log groups, alarms, dashboard,
EventBridge rule, SNS topic, Cognito test pool, Secrets Manager secret, IAM
roles and the Azure DevOps OIDC provider).

| Part | Scope | Trade-off |
|---|---|---|
| IAM roles | only `role/<ResourcePrefix>*`; create and change only with the boundary attached; boundary removal denied; the installer role cannot be changed | the tight part. A stack cannot make a role stronger than the boundary. |
| S3, S3 Vectors, Lambda, EventBridge, SNS | only names that start with `<ResourcePrefix>` | other teams' stacks in the account with the same prefix root are reachable |
| Bedrock, AgentCore, Cognito, CloudWatch, Logs | whole service (`*`) | their ARNs use ids, not names, so a name scope is not possible. A crafted stack could change another knowledge base, gateway or user pool in the account. |
| Secrets Manager | create, update, delete; no resource policy | a stack cannot open someone else's secret; roles read only secrets tagged `project=<ResourcePrefix>*` |
| Boundary | Bedrock, AgentCore, Cognito, Logs: whole service; S3, S3 Vectors, Lambda: prefix | a stack role may call any Bedrock model (cost) |

Who can do what in path B: the team admin can deploy any CloudFormation
template through the CDK deploy role, but CloudFormation then has only the
execution policy. So the execution policy is the real limit of the team
admin's power. Keep the account for EKS only when that matters.

Not proven yet: path B against a real account. The policy comes from the
synthesized templates; a service may need an action it does not list. The
error then names the action: add it to the template and run the stack
update again
.
