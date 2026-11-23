## Azure DevOps: steps for team {{team}}

EKS never calls Azure DevOps. You do these steps in Azure DevOps yourself
(rights: Project Administrator of `{{project}}`).

1. Make the repo `{{repo}}` in project `{{project}}` from the template: copy
   `templates/team-content/` of the EKS repo into it, without `.github/` (its
   README has the steps).
2. Get the organization id: open `{{orgUrl}}/_apis/connectionData` in a
   signed-in browser and copy `instanceId`. Pass it to the command as
   `--ado-org-id`.
3. Run the command. It deploys AWS, with a role that trusts service
   connection `{{serviceConnection}}` of `{{project}}` only, and prints its
   ARN. It writes the pipeline files to `{{adoFiles}}/`.
4. Organization settings -> Extensions: install AWS Toolkit for Azure DevOps
   (`AmazonWebServices.aws-vsts-tools`).
5. Repos -> New repository `kb-pipelines` in `{{project}}`. Push the files in
   `{{adoFiles}}/kb-pipelines/` to its `main`. Only platform admins may push.
6. Copy `{{adoFiles}}/azure-pipelines.yml` and the `deployment.json` the
   command wrote into `{{repo}}`, and commit both to `main`.
7. Project settings -> Service connections -> New -> AWS:
   name `{{serviceConnection}}`, "Use OIDC" on, role to assume `{{roleArn}}`.
   No access keys.
8. Pipelines -> New pipeline -> Azure Repos Git -> `{{repo}}` -> existing
   YAML `/azure-pipelines.yml`. Name it `kb-content-sync`.
9. Service connection `{{serviceConnection}}` -> Security: allow only
   pipeline `kb-content-sync`. Approvals and checks: Branch control
   (`refs/heads/main` only) and Required template
   (`{{project}}/kb-pipelines`, `templates/kb-sync.yml`, `refs/heads/main`).
10. Repos -> Branches -> `main` of `{{repo}}`: one reviewer, and build
    validation with `kb-content-sync`. `main` of `kb-pipelines`: one reviewer.
11. Project settings -> Permissions: `{{readers}}` in Readers,
    `{{publishers}}` in Contributors. Organization settings -> Users ->
    Group rules: both groups, Basic access.
12. A new organization has no hosted parallel job: request the free grant
    (Organization settings -> Parallel jobs).

Check: edit a document on `main`; pipeline `kb-content-sync` goes green.
