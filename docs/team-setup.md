# Team setup page

A static page an admin fills in once per team (legal, procurement, ...). Two tabs:

- **Get started** (the default): a wizard. Team name, where the documents
  live (GitHub, Azure DevOps, GitLab or a local folder) and the URL of the repo
  you already have (a copy of `templates/team-content`; GitHub: repo ids,
  optional; Azure DevOps: the org id), sign-in (Entra, Cognito, IAM, other OIDC), AWS
  profile and region. It prints ONE `scripts/add-team.sh` command that only
  deploys AWS, the team `deployment.json` that command writes, and the steps
  you do on the git host yourself (`templates/host-setup/<host>.md`). EKS
  never creates repos and never calls the git host
  ([add-a-team.md](add-a-team.md#get-started-one-command)).
  Per area (sign-in, AWS, Azure DevOps) you choose **I do it** or **IT does
  it**; IT gets a request pack and you paste back what IT sends
  ([it-request-packs.md](it-request-packs.md)).
- **Team config**: the full form for the team's `deployment.json` (several
  sources, any OIDC provider, draft server). The deploy runs in GitHub Actions
  from that file ([add-a-team.md](add-a-team.md), [deploy.md](deploy.md)).

Neither tab asks for a password, token or client secret, and the page calls no API.

![Get started: Azure DevOps, existing repo, Entra](images/get-started.png)

![The setup page with a legal team, Entra ID sign-in and two sources](images/team-setup.png)

## Run it

Run it on your own computer. It needs no AWS resource, no sign-in and no
secret: everything it writes is public ids, and the file goes to Git anyway.

```bash
cd ui/team-setup
npm ci
npm run dev        # http://127.0.0.1:5174
```

`npm run build` writes a static site to `ui/team-setup/dist/` if you want to
host it somewhere yourself.

## Team config: what you enter

| Part | Fields | Goes to |
|---|---|---|
| Team | name, AWS account id, region | `prefix` `eks-<team>`, `serverName` `<team>-kb`, `account`, `region` |
| Sign-in | provider preset, issuer URL, client id (audience for generic OIDC) | `gatewayAuth: "oidc"`, `identity` |
| Access | readers group, publishers group, draft server on/off, draft client id (draft audience for generic OIDC) | `access` (`readers`, `publishers`, `draftServer`, `draftIdentity`) |
| Content sources | provider (GitHub, GitLab, Azure DevOps), repository, folder, branch, host | `sources` |

The first source is the team repo itself: it holds `mcp/`, `smoke.json` and
this `deployment.json`, so its entry has no `repo`. Every other source gets
its git URL in `repo` and a namespace `id` from the repository name. That is
the `sources` list of [add-a-team.md](add-a-team.md#more-than-one-repo).

Sign-in presets: Entra ID and Cognito map to their own presets in
`identity/`; Okta, Auth0, Keycloak, Google and "Other" use the generic OIDC
core with that provider's client claim ([identity.md](identity.md)). There is
no client secret field: the gateway only checks tokens.

Access: the groups are what the Entra admin assigns to the sign-in apps
([entra-setup.md](entra-setup.md)). A draft server needs a second app, for
publishers only; its client id must differ from the main one
([add-a-team.md](add-a-team.md#draft-server)).

**Create team config** checks every field and shows the file. Then:

- **Download deployment.json**, or
- **Open pull request on GitHub** (when the team repo is on GitHub): opens
  GitHub's new-file page in the team repo with the file filled in. You commit it to a new branch and open
  the pull request with your own GitHub sign-in. The page holds no token.

Then `eks-deploy.yml` in the team repo deploys it, as in
[add-a-team.md](add-a-team.md#3-let-pushes-deploy-it).

The file's shape is written down in `schemas/team-deployment.schema.json`.
The Python side reads it with `infra/settings.py` `sources()`,
`identity/config.py` (sign-in) and `sync/content_sources.py` `from_config`
(an adapter per source, [content-sources.md](content-sources.md)).

## Test

`ui/team-setup/tests/get-started.spec.js` (Playwright) runs the wizard for
Azure DevOps (Entra), GitHub (IAM), GitLab (Cognito) and a local folder, and
checks the command, the file and the host steps; the Azure DevOps file must
equal `tests/fixtures/get-started-ado.deployment.json`, which
`tests/test_get_started.py` gets from the command itself. The host steps come
from `templates/host-setup/`, the same files the command prints.
`ui/team-setup/tests/setup.spec.js` fills a legal team with
Entra ID sign-in, readers, publishers, a draft server and two sources (GitHub
and GitLab), checks the file against
the schema and against `tests/fixtures/team-legal.deployment.json`, and
saves the screenshot above. `tests/test_content_sources.py` feeds the same
fixture to the Python readers.

```bash
CHECK_UI=1 factory/check.sh     # layer "ui"; runs by itself when ui/ or schemas/ change
```

On Amazon Linux 2023 the headless browser needs libraries the OS lacks;
`ui/team-setup/browser-libs.sh` unpacks them without root.
