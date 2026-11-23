# propose_change: writers change documents from chat

A writer asks their agent: "update the NDA term to 3 years". The agent reads
the document, edits it, and calls `propose_change`. The server opens a pull
request (GitHub, Azure DevOps) or merge request (GitLab) into the team's draft
branch. A publisher reviews and merges it. Nothing changes before that.

```text
writer -> agent -> draft server: propose_change(document_id, new_text, summary)
                     -> new branch off draftBranch, one commit, PR/MR into draftBranch
publisher reviews -> merges -> draft server shows it -> later merge to main
```

## Turn it on

In the team's `deployment.json`:

```json
"access": {"publishers": "legal-kb-publishers", "draftServer": true, "proposals": true}
```

Then deploy the draft server: `scripts/add-team.sh --team legal --content ... --draft`.
It works out, per source, the repo, the folder and the draft branch, and
writes them to `proposals` in the draft server's `deployment.json`. The main
server never gets the tool, even with the flag on.

Only the draft server takes proposals because only publishers can reach it:
its sign-in app is assigned to publishers only (`access.draftIdentity`, see
[entra-setup.md](entra-setup.md)). The gateway does not pass the caller's
groups to the server, so this assignment is the access check.

## The bot token

The stack makes one Secrets Manager secret (output `ProposalsTokenSecretArn`).
Put the team's bot token in it, from a file, never pasted:

```bash
aws secretsmanager put-secret-value --secret-id <ProposalsTokenSecretArn> \
  --secret-string file://token.txt
```

| Host | Token | Needs |
|---|---|---|
| GitHub | fine-grained token of a bot account, or a GitHub App token | Contents and Pull requests: read and write, this repo only |
| GitLab | project access token | `api` scope, role Developer |
| Azure DevOps | PAT of a bot user | Code: read and write |

The server reads the secret (cached 5 minutes), never takes a token from the
caller, and never logs it. API errors name the step and the HTTP status only.

## Limits

| Limit | Value |
|---|---|
| Documents | existing `.md` documents of a source with a draft branch; unknown ids are refused |
| Change | the whole new text; empty text (a delete) is refused |
| Size | 200 000 bytes (`PROPOSALS_MAX_BYTES`) |
| Rate | 10 per caller per hour (`PROPOSALS_PER_HOUR`), per server instance |
| Who asked | the caller id the runtime sees, written in the PR body. Through the IAM gateway this is the MCP session id, not the person (#15) |

## Proof

`deploy-team-demo.yml` runs `infra/propose_smoke.py` in its own job,
`propose-live`, after the deploys. It calls `kb___propose_change` through the
gateway, checks that the PR into `crew/mate/legal-demo-draft` is open, then
closes it and deletes its branch. All job tokens stay read-only, and the repo
does not let Actions open or approve PRs. The bot token is the repo secret
`PROPOSALS_TOKEN`: a fine-grained PAT for this repo only, with Contents and
Pull requests read/write and a short expiry. The job puts it in the stack's
secret and uses it for the checks. Without it, the job fails at its first
step and says what to add.

Tests with faked GitHub, GitLab and Azure DevOps APIs: `tests/test_proposals.py`.
