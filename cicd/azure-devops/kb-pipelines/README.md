# Knowledge base pipelines (platform team)

The approved sync pipeline that content repositories extend.

| Path | Purpose |
|---|---|
| `templates/kb-sync.yml` | Validate on every run; on `main`, sync changed sections, ingest, verify |
| `config/kb-target.yml` | Bucket, knowledge base and service connection. Platform-owned. |
| `sync/` | Chunker, sync, validator and verifier scripts |

## Security model

- The `aws-kb-sync` service connection uses OIDC. No AWS keys are stored.
- AWS trusts only the token subject `sc://<org>/<project>/aws-kb-sync`. The role can
  write only its source's prefixes and start ingestion on one knowledge base.
- That token does not identify the branch or the YAML, so two checks on the
  service connection carry that part:
  - **Branch control:** only `refs/heads/main`.
  - **Required template:** only pipelines extending
    `templates/kb-sync.yml` from this repository's `main`.
- Changes to this repository need platform-team review, because it decides what
  every content pipeline is allowed to do with AWS credentials.
