# Azure DevOps in one script: `ado-setup-<team>.sh`

EKS never calls Azure DevOps. For an Azure DevOps team, the team install
makes everything AWS needs, and gives you one script for the Azure DevOps
side. You run it on your laptop with your own Azure DevOps PAT.

| Step | Where | What |
|---|---|---|
| 1. Add team | EKS console | team stack, its pipeline user and key, the filled script |
| 2. Download | team page | **Download Azure DevOps setup script**: `ado-setup-<team>.sh` |
| 3. Key | team page | **Show pipeline key (once)**: copy the secret |
| 4. Check | laptop | `bash ado-setup-<team>.sh check` |
| 5. Set up | laptop | `bash ado-setup-<team>.sh` |

The script needs `bash` and `python3`. The PAT is asked at a hidden prompt
(or env `ADO_PAT`); it is never printed or stored. No Azure CLI, no Azure
DevOps extension, and no AWS login on the laptop.

## What the team install makes

- In the team stack: an IAM user (path `/eks-ado/`) that may only write this
  team's documents and chunks, start and read ingestion of its knowledge
  base, and run its publisher. A secret `<stack>-ado-sync-key`.
- After the deploy: a new access key for that user. Its secret goes into the
  secret; the console shows it once. Old keys are deleted. Running the team
  build again makes a new key.
- `ado-setup-<team>.sh`: `platform/ado-setup.sh` with the team's values
  filled in (org, project and repo from the Add team form, region, bucket,
  source id, knowledge base id, data source id, publisher, key id, and the
  pinned EKS release with its sha256), plus the sample documents. Also in
  `repo-files.zip`.

Deleting the team deletes the user, its keys and the secret.

## 4. Check your rights (2-5 min)

`bash ado-setup-<team>.sh check` makes a repo and a pipeline named
`ci-check-<random>`, and deletes both. It prints PASS or FAIL per right:

| Line | If it fails |
|---|---|
| PAT and organization | a new PAT for this org |
| project read | ask to be added to the project |
| Code: create repo / push / delete repo | PAT scope Code (Read & write); project right Create repository |
| Build: create pipeline / secret variable / delete pipeline | PAT scope Build (Read & execute); project right Edit build pipeline |
| Build: run pipeline | the run's error and log tail are printed |
| hosted parallel job (org-wide, once) | a brand-new org may have none: [request the free grant](https://aka.ms/azpipelines-parallelism-request), or use your own agent (`--pool <name>`) |
| INFO create projects | only for `--new-project` |

## 5. Set up

```bash
bash ado-setup-<team>.sh                 # new repo from the sample (the form's repo name)
bash ado-setup-<team>.sh --repo <name>   # an existing repo: adds azure-pipelines.yml by pull request
bash ado-setup-<team>.sh --dry-run       # the planned calls only
```

It asks for the pipeline key's secret at a hidden prompt, then:

1. Makes the repo with the sample documents and `azure-pipelines.yml`, or
   checks an existing one for `knowledge-base/` and `mcp/instructions.md` and
   adds `azure-pipelines.yml` (pull request on `eks-setup`, or
   `--commit-to main`). The repo holds documents and that one file only.
2. Makes the pipeline, sets its variables (`AWS_REGION`, `DOCS_BUCKET`,
   `SOURCE_ID`, `KB_ID`, `DS_ID`, `MCP_PUBLISHER`, `AWS_ACCESS_KEY_ID`) and
   the secret variable `AWS_SECRET_ACCESS_KEY`, and allows the agent pool.
3. Runs it and waits. The pipeline downloads the EKS release pinned in
   `azure-pipelines.yml`, checks its sha256, and runs the real sync
   (`sync/sync_chunks.py`, `verify_kb_state.py`, `publish_mcp.py`) with plain
   script steps. Green means the documents are searchable now.
4. Every later push to `knowledge-base/` or `mcp/` on `main` runs it again.
   `--demo-edit "<sentence>"` shows that: it adds the sentence to a document
   and waits for the run it starts.

Other flags: `--pool <name>` (your own agent: it needs `python3`, `curl` and
the `aws` CLI), `--new-project`, `--project`, `--org`, `--delete` (pipeline
and repo, asks first). Re-run any time: each step is skipped when done.

Risk: the pipeline holds a long-lived AWS key, limited to this team's source.
Anyone who can edit the pipeline can use it. Delete the team, or run its
build again for a new key, when the demo ends.

## Proof

`.github/workflows/ado-setup-test.yml` runs `check` against the org and
project in repo variables `EKS_ADO_ORG_URL` and `EKS_ADO_PROJECT`, with repo
secret `ADO_TEST_PAT`. Unit tests: `tests/test_ado_setup.py`,
`tests/test_platform.py`. shortcut: the end-to-end CI job (Add team, the
script, pipeline, search, a document change) is a follow-up.
