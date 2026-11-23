# EKS platform: install once, then add teams in the console

No laptop setup. A cloud admin runs one command in AWS CloudShell. It makes
the EKS console in your account. Team admins then add teams in the console;
each team installs inside your account (AWS CodeBuild).

| Step | Who | Where | Time |
|---|---|---|---|
| 1. Install the platform | cloud admin (AWS admin rights) | AWS CloudShell | about 6 min, once per account and region |
| 2. Sign in to the console | console admin | browser | 1 min |
| 3. Add team | console admin | browser | 10-15 min per team (CodeBuild runs it) |
| 4. Steps on the Git host | the team's repo admin | Azure DevOps, GitHub or GitLab | 10 min |

EKS never calls your Git host's API. The console tells you what to do there.

## 0. Check your AWS rights (optional, 5 min)

In a company account, service control policies (SCPs) or permission
boundaries may block parts of the install. In the same CloudShell, after the
download below (or with only the `tar -xzf` part), run:

```bash
eks-$V/platform/check-permissions.sh --region <region>
```

It makes one of each resource type EKS makes, deletes them again, and prints
PASS or FAIL per type with AWS's own refusal text. Exit 0 = you can install.
Cost about $0, no e-mail is sent. Details:
[install-permissions.md](install-permissions.md#the-cloudshell-check-real-create-and-delete).

## 1. Install the platform (AWS CloudShell)

**Switch the AWS console to your region first** (top right), then open
[AWS CloudShell](https://console.aws.amazon.com/cloudshell/home). CloudShell is
per region: the platform goes where CloudShell runs, unless you add
`--region <region>`. The region needs Bedrock knowledge bases, Titan Text
Embeddings V2, AgentCore Runtime and Gateway, and S3 Vectors. On 2026-11-23
these had all four: `us-east-1`, `us-east-2`, `us-west-2`, `ca-central-1`,
`eu-central-1`, `eu-west-1`, `eu-west-2`, `eu-west-3`, `eu-north-1`,
`ap-south-1`, `ap-southeast-2`, `ap-northeast-1`, `ap-northeast-2` (we test in
`ap-south-1`). The script checks this before it changes anything.

Take `VERSION` and `SHA256` from the
[release page](https://github.com/anubhav247/EnterpriseKnowledgeSharingOnAWS/releases),
put **your own e-mail** in place of `YOUR_EMAIL`, then:

```bash
V=VERSION; S=SHA256; R=https://github.com/anubhav247/EnterpriseKnowledgeSharingOnAWS/releases/download/$V
curl -fsSLO $R/eks-$V.tar.gz && echo "$S  eks-$V.tar.gz" | sha256sum -c - \
  && tar -xzf eks-$V.tar.gz && eks-$V/platform/install.sh --admin-email YOUR_EMAIL
```

It checks every input first (a placeholder e-mail is refused), then the
region, prints `account <id>, region <region>` and asks `Continue? [y/N]`.
Nothing in AWS changes before you answer `y` (`--yes` skips the question).
Then it checks the download, sets up the CDK once (first time in the account and
region), makes the stack `eks-platform`, copies the release into the stack's
bucket, runs the console build (about 3 min) and returns. It prints:

```text
EKS console: https://d1234abcd.cloudfront.net/
  ready (build eks-platform-runner:...)
  sign in as ana@acme.com: Cognito e-mails you a temporary password now (check spam).
```

The invite e-mail is sent only after the console page is live, so its link
works. The password is never printed: only Cognito's e-mail has it. Teams
(the long builds) run later in CodeBuild, from the console.

**CloudShell closed, or the script stopped?** Run the same command again. It
waits for a stack still being made, skips what is done, and waits for a
console build already running. With a published release the stack itself
fetches the release and finishes the console, so a closed CloudShell does not
stop it. Before the page is ready the link shows "The EKS console is being
set up".

**The console link shows XML `AccessDenied`?** The page is not in its bucket
yet: CloudFront asks S3 for `index.html`, and S3 answers a missing file with
`AccessDenied`. Read-only checks in CloudShell:

```bash
S=eks-platform; o() { aws cloudformation describe-stacks --stack-name $S --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
aws s3 ls "s3://$(aws cloudformation describe-stack-resource --stack-name $S --logical-resource-id ConsoleBucket --query StackResourceDetail.PhysicalResourceId --output text)/"   # index.html, console.json?
aws codebuild list-builds-for-project --project-name "$(o RunnerProject)" --query 'ids[:3]' --output text   # newest first
aws codebuild batch-get-builds --ids <build id> --query 'builds[0].[buildStatus,currentPhase]'   # SUCCEEDED?
aws s3 ls "s3://$(o Bucket)/release/"   # eks.tar.gz there?
```

No `index.html`: re-run the install command; it runs the console build again
and prints its last log lines if it fails.

Company sign-in instead of the Cognito admin pool:

```bash
eks-$V/platform/install.sh --sign-in oidc \
  --issuer https://login.microsoftonline.com/<tenant id>/v2.0 --client-id <app client id> \
  --admin-claim groups=<admin group object id>
```

The app is a single-page app (public client, PKCE), with the console URL as
redirect URI. `--admin-claim` (optional) lets only tokens with that claim in.

CloudShell blocked in your company? The same template,
`platform/eks-platform.yaml`, works as a CloudFormation quick-create link with
`ReleaseUrl` and `ReleaseSha256` set; the stack then fetches the release
itself. The account needs `cdk bootstrap` once before the first team.

## 2-3. Add a team in the console

Sign in, then **Add team**:

| Field | What |
|---|---|
| Team name | `legal` gives server `legal-kb`, stack `eks-legal-stack`, groups `legal-kb-readers` / `legal-kb-publishers` |
| Docs repo URL | Your copy of `templates/team-content` (public https URL). Empty: the sample documents |
| Sign-in | AWS IAM, Microsoft Entra ID (tenant id, client id), other OIDC (issuer, client id), or Cognito |
| GitHub repo ids / Azure DevOps organization id | For the role the repo's pipeline uses (shown for that host) |
| Monitoring | On or off; alert e-mail optional |

**Create team** starts the install in CodeBuild. The page shows each step
(`==> 5 cdk deploy` ... `<== ... ok`). When it is done it shows the gateway
URL, the Kiro `mcp.json` entry, the team `deployment.json` to commit, and the
steps left on the Git host (`templates/host-setup/<host>.md`; for Azure
DevOps also a zip of the pipeline files).

**Delete team** removes the team's stack, buckets and roles, and its records.
The team's repo is not touched.

## Remove everything

Delete every team in the console, then in CloudShell:

```bash
eks-$V/platform/install.sh --delete
```

The stack refuses to delete while teams exist, so no team stack is left
without a console to remove it. What stays: the `CDKToolkit` stack, which
other CDK apps share.

A stray CDK setup (a first run stopped after `CDK bootstrap`, or in the wrong
region) is the `CDKToolkit` stack and its bucket `cdk-hnb659fds-assets-<account>-<region>`.
If nothing else in that account and region uses the CDK: empty the bucket in
the S3 console (it keeps versions: **Empty** removes them all), then delete the
`CDKToolkit` stack in CloudFormation, in that region.

## What the platform makes, and who may do what

| Part | AWS | Rights |
|---|---|---|
| Console page | S3 bucket (private) + CloudFront (HTTPS) | CloudFront reads the bucket; nobody else |
| Sign-in | Cognito user pool (admins only, no self sign-up), or your OIDC app | |
| API | API Gateway HTTP API, JWT authorizer, Lambda `eks-platform-api` | Reads and writes team records, starts and reads runner builds. No rights on team stacks |
| Runner | CodeBuild `eks-platform-runner`, no Docker | Same as the path B installer role ([install-permissions.md](install-permissions.md)): assumes the CDK roles and the team content roles, reads, cleans up names starting with `eks-`; plus the GitHub deploy role stack and its own bucket |
| Records | S3 bucket: `release/eks.tar.gz`, `teams/<team>/` | |
| Logs | API calls (who, route, team), API access log, each build | 90 days |

Trade-offs, on purpose:

- The runner deploys through the CDK roles. With default `cdk bootstrap`
  those may do anything in CloudFormation, as for any CDK app. To narrow
  them, bootstrap with the path B execution policy
  ([install-permissions.md](install-permissions.md)).
- The console page itself is public (no data in it); everything it shows comes
  from the API, which needs a signed-in admin token.
- Team stacks ship as code zips (`--artifact code`): no Docker in CodeBuild.

Not yet: a private docs repo (the runner clones with plain git and no
credentials; use a public repo or the sample documents for now), and one
Cognito pool per team for the console (all console admins see all teams).
