# Team content repo (EKS template)

This folder is a starting point. Copy it into a repo your team already has (or
makes itself) on GitHub, Azure DevOps or GitLab. EKS never creates repos or
projects on your git host: you own the repo, EKS only reads it.

## Copy it into your repo

Work in a checkout of the EKS repo. Replace `<your repo>` with your clone URL.

**Any host, with git** (GitHub, Azure DevOps, GitLab):

```bash
git clone <your repo> my-docs
cp -R templates/team-content/. my-docs/
cd my-docs
rm -rf .github   # Azure DevOps and GitLab only: it is a GitHub Actions workflow
git add -A && git commit -m "EKS team content from templates/team-content"
git push origin main
```

**GitHub, in the browser**: make the repo, then Add file -> Upload files, and
drop in the contents of this folder, `.github/` included.

**Azure DevOps, in the browser**: Repos -> Files -> Upload file(s) into an
empty repo, without `.github/`. Or Repos -> Import repository, from a GitHub
copy of this folder.

**GitLab, in the browser**: New project -> Import project -> Repository by
URL, from a GitHub copy of this folder; then delete `.github/`.

Then run the command the setup page (Get started tab) gives you. It deploys
AWS and prints the steps left on your git host
(`templates/host-setup/<host>.md` in the EKS repo):

```bash
scripts/add-team.sh --team legal --content <your repo URL or folder>
```

## What each part does

| Path | What it is | Needed? |
|---|---|---|
| `knowledge-base/` | Your documents, markdown with front matter (title, category, owner, tags). Replace `example-policy.md`. | Yes. With 0 documents the server has nothing to answer from. |
| `mcp/instructions.md` | What assistants read when they connect: what the server holds, how to answer. `owner:` in its front matter names your team. At most about 1,900 characters. | Strongly advised. Without it agents get generic text. |
| `mcp/prompts/*.md` | Reusable prompts, one per file. | Optional. |
| `deployment.json` | Your stack: region, sign-in, names. `scripts/add-team.sh` adds the host keys; commit what it writes. | Yes (the defaults work). |
| `smoke.json` | Questions every deploy asks: some your documents must answer, some they must not. | Optional, advised. |
| `.github/workflows/eks-deploy.yml` | GitHub only: deploys and syncs on every push to main. | GitHub only. Delete it elsewhere. |

Every sync prints a `WARN content:` line for a missing part, and the
server's `get_sync_status` tool lists them (`content_checks`). A warning never
fails the sync.
