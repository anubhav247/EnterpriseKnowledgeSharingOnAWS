# IT request packs: when IT owns sign-in, AWS or Azure DevOps

In a large company the person who installs EKS is often not the identity
admin, the AWS account admin or the Azure DevOps admin. Those changes go
through an IT ticket (ServiceNow or any other tool). A request pack is the
text for those tickets: what to create, with the exact values, a filled
script where one exists, the values to send back, and how to check them.
EKS talks to no ticket system: you paste the pack into your ticket.

![IT request step on the setup page](images/it-request.png)

## The flow

1. Setup page ([team-setup.md](team-setup.md)), tab **Get started**. Per
   area, choose **I do it** or **IT does it**:
   - Repository step (Azure DevOps only): who makes the project and repo.
   - Sign-in step (Entra ID or other OIDC): who makes the sign-in app and groups.
   - Team and AWS step: who sets up AWS.
2. Any **IT does it** adds the step **IT request**. Fill your e-mail, the
   group members and (AWS) the account id. The pack shows on the page.
   **Download request pack (.md)** saves it as one file. The page also
   prints the same pack as a CLI command.
3. Send the pack to IT. When IT answers, paste the values back on the same
   step (below), and go on to **Run it**: one `add-team.sh` command with
   those values in it.

The CLI writes the same files from the same inputs:

```bash
scripts/add-team.sh --request-pack ./it-request-pack --team legal \
  --it sign-in,aws,ado --owner ana@example.com \
  --reader-members 'ben@example.com cy@example.com' --publisher-members ana@example.com \
  --account 123456789012 --region us-east-1 \
  --installer-principal-arn arn:aws:iam::123456789012:role/legal-admin \
  --ado-org contoso --ado-project 'EKS Legal' --ado-repo legal-docs
```

It writes `it-request-pack.md` (all parts) and one file per part:
`sign-in.md`, `aws.md`, `azure-devops.md`. It changes nothing anywhere.
Empty member lists mean the owner only.

## What each part asks for

| Part | IT creates | Values to send back | How you check |
|---|---|---|---|
| Sign-in (any OIDC provider) | one public client app (PKCE, no secret), the MCP clients' loopback redirect URIs, one scope `kb.read`, assignment required, groups `<team>-kb-readers` and `<team>-kb-publishers` with members and owners | issuer URL, client id, audience, scope value | `scripts/add-team.sh --check-oidc` (below) |
| AWS (path B of [install-permissions.md](install-permissions.md)) | stack `eks-installer` from `factory/infra/customer/eks-installer-role.yaml`, then EKS's own CDK set-up (`kbmcp-cdk-toolkit`, never `CDKToolkit`) with its execution policy; lets you assume `eks-installer` | `InstallerRoleArn`, bootstrap done | `aws sts get-caller-identity --profile eks-installer` |
| Azure DevOps | AWS Toolkit extension, project, empty repo, you in Project Administrators, groups in Readers / Contributors, Basic access group rule, a hosted parallel job | repo URL | `az repos show ... --query webUrl` |

The sign-in sheet names no provider: it lists what the gateway needs, not
where to click. Use your provider's own guide. Using Entra ID and you are
its admin? Skip IT: choose **I do it** and run
`identity/entra/setup_entra_app.sh` ([entra-setup.md](entra-setup.md)).

Azure DevOps: the OIDC service connection `aws-kb-sync` needs the AWS role
the install makes, so you make it after the install (as Project
Administrator), not IT. EKS never calls Azure DevOps: the install prints the
steps ([templates/host-setup/azure-devops.md](../templates/host-setup/azure-devops.md)).

## Paste back, then run

| You paste | The command gets |
|---|---|
| issuer URL `https://login.microsoftonline.com/<tenant>/v2.0` + client id | `--auth entra --entra-tenant-id <tenant> --entra-client-id <client id>` (the Entra preset also pins the tenant) |
| any other issuer URL + client id (+ audience) | `--auth oidc --oidc-discovery-url <issuer>/.well-known/openid-configuration --oidc-audience <audience, default the client id>` |
| `InstallerRoleArn` | `--ci-trust-role-arn <ARN> --permissions-boundary eks-role-boundary`; use an AWS profile for that role, e.g. `eks-installer` |
| Azure DevOps repo URL | `--content <URL>` (copy `templates/team-content` into the repo first) |

## Check the sign-in values

```bash
scripts/add-team.sh --check-oidc --issuer <issuer URL> --client-id <client id> \
  --audience <audience> --scope kb.read
```

Read-only. It reads `<issuer>/.well-known/openid-configuration` (its
`issuer` must match), the signing keys (JWKS, at least one RSA or EC key)
and PKCE S256 support, then prints what the gateway will enforce (discovery
URL, audience, client claim, Entra's `tid`) and the install flags. Exit 0:
all pass; 1: a check failed.

With a test access token it also checks the token's `iss`, `aud`, scope,
Entra `tid` and expiry. Pipe it in, so it stays out of your shell history:

```bash
pbpaste | scripts/add-team.sh --check-oidc --issuer ... --client-id ... --scope kb.read --token -
```

It does not check the token's signature (the gateway does) and prints no
token. The scope is what MCP clients request; the gateway gates on issuer,
audience and signature.

## For maintainers

The ticket text is in `templates/it-request-packs/*.md`, with `{{name}}`
fields. `scripts/eks_request_pack.py` (CLI) and
`ui/team-setup/src/requestPack.js` (page) fill the same templates; the
redirect URIs come from `identity/entra/setup_entra_app.sh` and the CDK
version from `factory/toolchain/versions.env`. Both tests compare with
`tests/fixtures/it-request-pack/` (`tests/test_request_pack.py`,
`ui/team-setup/tests/it-request.spec.js`). Changed a template? Regenerate
the fixture with the CLI line in `ARGV` of `tests/test_request_pack.py`.
