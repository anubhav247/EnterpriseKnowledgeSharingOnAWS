# Entra setup for one team server

What an Entra admin does so one team (here `legal`) can sign in to its EKS
server, and what to paste into the team's `deployment.json`. The two groups
(`legal-kb-readers`, `legal-kb-publishers`) also get roles on the repo host:
see [access-model.md](access-model.md). About 15 minutes
in the Entra admin center. No secret is made or shared: MCP clients are public
clients and sign in with PKCE.

| Server | Entra app | Assigned groups | Who can ask |
|---|---|---|---|
| `legal-kb` (main) | `legal-kb` | readers + publishers | everyone in either group |
| `legal-draft-kb` (draft, optional) | `legal-draft-kb` | publishers only | the people who edit |

Entra enforces this: with "Assignment required" on, Entra gives a token only
to members of an assigned group. The gateway then checks the token was made
for its own app (`aud` = the app's client id, `tid` = your tenant), so a
reader's token for `legal-kb` does not open `legal-draft-kb`. The group names
in `deployment.json` are labels for people and for the `add-team.sh` output;
EKS does not read group claims.

Needs: Application Administrator (or Cloud Application Administrator) to make
the app, and Entra ID P1 or higher to assign groups (without P1, assign users
one by one).

## 1. App registration

Entra admin center -> Identity -> Applications -> App registrations -> New
registration.

| Field | Value |
|---|---|
| Name | `legal-kb` (the server name) |
| Supported account types | Accounts in this organizational directory only (single tenant) |
| Redirect URI | platform **Public client/native (mobile & desktop)**, `http://localhost:7778/oauth/callback` |

Then Authentication -> Mobile and desktop applications -> add the other
redirect URIs, one per client:

| Client | Redirect URI |
|---|---|
| Kiro (built-in sign-in) | `http://localhost:7778/oauth/callback` |
| Kiro via `mcp-remote` (the entry `make_mcp_config.py` prints today) | `http://localhost:3334/oauth/callback` |
| Claude Code (`--callback-port 7779`) | `http://localhost:7779/callback` |
| VS Code | `http://127.0.0.1:33418` and `https://vscode.dev/redirect` |

On the same page set **Allow public client flows** to Yes. Add no client
secret and no certificate.

Manifest: set `"requestedAccessTokenVersion": 2` (under `api`). v2 tokens have
the issuer and audience the gateway checks.

## 2. Expose the API scope

App registration -> Expose an API.

1. Application ID URI: Add, keep `api://<client id>`.
2. Add a scope: name `kb.read`, who can consent **Admins and users**, display
   name "Read the knowledge base", state Enabled.
3. Authorized client applications: add the app's own client id with the
   `kb.read` scope ticked, so users get no consent prompt.

Clients ask for `api://<client id>/kb.read`. Do not put that scope in
`deployment.json`: Entra writes the short form (`scp=kb.read`) in tokens
([identity.md](identity.md#the-generic-settings)).

## 3. Assignment required, then the groups

Entra admin center -> Identity -> Applications -> Enterprise applications ->
`legal-kb`.

1. Properties: **Assignment required? = Yes**. Save.
2. Users and groups -> Add user/group: the readers group (e.g.
   `legal-kb-readers`) and the publishers group (e.g. `legal-kb-publishers`), role
   "Default Access".

## 4. The draft app (only with a draft server)

Repeat steps 1 to 3 with name `legal-draft-kb`, and in step 3 assign **only
the publishers group**. It is a second app on purpose: one app for both
servers would let every reader read the drafts.

## 5. Values for deployment.json

| Where you find it | Key in the team's `deployment.json` |
|---|---|
| `legal-kb` Overview -> Directory (tenant) ID | `identity.entra.tenantId` |
| `legal-kb` Overview -> Application (client) ID | `identity.entra.clientId` |
| readers group name | `access.readers` |
| publishers group name | `access.publishers` |
| want a draft server | `access.draftServer: true` |
| `legal-draft-kb` Overview -> Application (client) ID | `access.draftIdentity.entra.clientId` (same `tenantId`) |

```json
"gatewayAuth": "oidc",
"identity": {
  "provider": "entra",
  "entra": {"tenantId": "<tenant id>", "clientId": "<legal-kb client id>"}
},
"access": {
  "readers": "legal-kb-readers",
  "publishers": "legal-kb-publishers",
  "draftServer": true,
  "draftIdentity": {
    "provider": "entra",
    "entra": {"tenantId": "<tenant id>", "clientId": "<legal-draft-kb client id>"}
  }
}
```

The setup page ([team-setup.md](team-setup.md)) writes this block for you.
Then deploy as in [add-a-team.md](add-a-team.md).

By script instead of the portal: `identity/entra/setup_entra_app.sh` does
steps 1 to 3 for one app (Azure CLI):

```bash
APP_NAME=legal-kb GROUP_NAME="legal-kb-readers;legal-kb-publishers" identity/entra/setup_entra_app.sh
APP_NAME=legal-draft-kb GROUP_NAME="legal-kb-publishers" identity/entra/setup_entra_app.sh
```

## Check it

A member of `legal-kb-readers` signs in to `legal-kb` from Kiro and gets
answers. The same person signing in to `legal-draft-kb` is stopped by Entra
(AADSTS50105: not assigned). A member of `legal-kb-publishers` can use both.

## The other path: IAM Identity Center, no Entra app

For people who already sign in to AWS (IAM Identity Center, also when it is
fed from Entra by SCIM):

1. In `deployment.json`: `"gatewayAuth": "iam"` and
   `"gatewayUserTrustAccount": "<account the people sign in to>"`. The stack
   makes the role `<prefix>-gateway-user`, which may only call this gateway.
   A draft server makes `<prefix>-draft-gateway-user`.
2. In IAM Identity Center, a permission set for readers with one inline
   policy, assigned to the readers and publishers groups in that account:

   ```json
   {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
     "Resource": "arn:aws:iam::<stack account>:role/<prefix>-gateway-user"}]}
   ```

   A second permission set for publishers that also allows
   `<prefix>-draft-gateway-user`, assigned to the publishers group only.
3. Each person adds a role profile for `<prefix>-gateway-user` to
   `~/.aws/config`; the Kiro entry runs `mcp-proxy-for-aws` with it
   (`clients/kiro/make_mcp_config.py` prints it).

No Entra app, no redirect URIs. `access.readers` and `access.publishers` then
name the Identity Center groups.
