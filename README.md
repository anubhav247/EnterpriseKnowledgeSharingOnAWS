# Enterprise Knowledge Sharing (EKS) v0.1.3-rc3

EKS is an MCP server over your business documents. People ask it questions
from Kiro, Claude or any MCP client; it answers with the source documents.
Each team gets its own server, built from the team's own documents repo.

Everything runs in your own AWS account. EKS never calls your Git host's API:
you copy one template into a repo you already have.

## Start here

- Install the EKS platform (one command in AWS CloudShell), then add teams in
  the EKS console: [docs/platform.md](docs/platform.md).
- Add a team from a terminal instead: [docs/add-a-team.md](docs/add-a-team.md).
- Who may do what: [docs/access-model.md](docs/access-model.md),
  [docs/install-permissions.md](docs/install-permissions.md).

## What is in this release

| Folder | What |
|---|---|
| `platform/` | The EKS platform stack: console, its API, the build runner |
| `infra/`, `mcp_server/`, `sync/`, `identity/` | One team's stack, the MCP server, content sync, sign-in |
| `scripts/` | `add-team.sh`, `install.sh`, `uninstall.sh` |
| `templates/` | The team content repo template and the per-host setup steps |
| `ui/team-setup/dist/` | The EKS console page, built |
| `cicd/` | The team repo's pipelines (GitHub Actions, Azure Pipelines) |

Release v0.1.3-rc3 of [anubhav247/EnterpriseKnowledgeSharingOnAWS](https://github.com/anubhav247/EnterpriseKnowledgeSharingOnAWS). Check a download
against its `.sha256` file before you run it.
