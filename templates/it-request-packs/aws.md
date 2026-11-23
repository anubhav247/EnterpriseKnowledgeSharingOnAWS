
## AWS: installer role in account {{account}} ({{region}}) for team {{team}}

### Ticket text

Please deploy the one-time EKS installer stack in AWS account {{account}},
region {{region}}, then bootstrap CDK with its execution policy (path B of
docs/install-permissions.md). Once per account and region; later teams reuse
it. It needs an account admin (IAM rights).

- Stack name: `eks-installer`
- Template: `factory/infra/customer/eks-installer-role.yaml` (EKS repo)
- Parameters:
  - `ResourcePrefix` = `eks-` (team {{team}} uses prefix `eks-{{team}}`)
  - `InstallerRoleName` = `eks-installer`
  - `InstallerPrincipalArn` = {{installerPrincipal}}
  - `CdkQualifier` = `kbmcp`
- It makes: execution policy `eks-cfn-execution`, role boundary
  `eks-role-boundary`, role `eks-installer`. No other change.
- Then EKS's own CDK set-up (`cdk bootstrap --qualifier kbmcp`, stack
  `kbmcp-cdk-toolkit`) with `--cloudformation-execution-policies` set to the
  `eks-cfn-execution` policy. An existing `CDKToolkit` stack (another team's)
  is not touched.
- Please let {{owner}} assume `eks-installer` (for example a permission set
  or a role in IAM Identity Center that allows `sts:AssumeRole` on it).

### Script IT can run

From a checkout of the EKS repo, signed in as an admin of {{account}}:

```bash
export AWS_REGION={{region}}
aws cloudformation deploy --stack-name eks-installer \
  --template-file factory/infra/customer/eks-installer-role.yaml \
  --capabilities CAPABILITY_NAMED_IAM{{installerOverride}}
aws cloudformation describe-stacks --stack-name eks-installer \
  --query 'Stacks[0].Outputs' --output table
npx aws-cdk@{{cdkVersion}} bootstrap aws://{{account}}/{{region}} \
  --qualifier kbmcp --toolkit-stack-name kbmcp-cdk-toolkit \
  --tags kbmcp:managed=true --tags kbmcp:stack=kbmcp-cdk-toolkit \
  --cloudformation-execution-policies arn:aws:iam::{{account}}:policy/eks-cfn-execution
```

### Values to send back

| Value | Where to find it |
|---|---|
| `InstallerRoleArn` | stack output, `arn:aws:iam::{{account}}:role/eks-installer` |
| bootstrap done | yes / no |

### How to verify

The team admin adds a profile and checks who they are:

```ini
# ~/.aws/config
[profile eks-installer]
role_arn = <InstallerRoleArn>
source_profile = <your own profile>
region = {{region}}
```

```bash
aws sts get-caller-identity --profile eks-installer
aws cloudformation describe-stacks --stack-name kbmcp-cdk-toolkit --profile eks-installer \
  --query 'Stacks[0].StackStatus'
```

Expected: an `assumed-role/eks-installer/...` ARN in {{account}}, and
`CREATE_COMPLETE` or `UPDATE_COMPLETE`. The install then checks the rest
itself, read-only, before it changes anything.
