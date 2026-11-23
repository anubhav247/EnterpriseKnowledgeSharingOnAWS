
## Azure DevOps: project and repo for team {{team}}

### Ticket text

Please set up Azure DevOps for the EKS knowledge base of team {{team}}, in
organization {{orgUrl}}. Rights needed: Project Collection Administrator.

- Organization: connected to our directory (Organization settings ->
  Microsoft Entra), so the groups below can be added. Without it, make
  Azure DevOps groups with the same names.
- Extension: AWS Toolkit for Azure DevOps (`AmazonWebServices.aws-vsts-tools`),
  installed in the organization.
- Project: `{{project}}` (private). Repo: `{{repo}}` (Git, may be empty).
- {{owner}}: member of the project's Project Administrators. The team admin
  then does the rest by hand (EKS never calls Azure DevOps): the OIDC service
  connection `aws-kb-sync` (it needs the AWS role the install makes, so it
  cannot be made first), the pipeline `kb-content-sync`, its checks, and the
  branch policies (`templates/host-setup/azure-devops.md` in the EKS repo).
- Permissions: group `{{readers}}` in the project's Readers;
  `{{publishers}}` in Contributors.
- Users: a group rule for `{{readers}}` and `{{publishers}}` with Basic access.
- Pipelines: one Microsoft-hosted parallel job (a new organization has none:
  request the free grant or buy one).

### Script IT can run

With `az login` and `az extension add --name azure-devops`:

```bash
ORG={{orgUrl}}
az devops extension install --org "$ORG" \
  --publisher-id AmazonWebServices --extension-id aws-vsts-tools
az devops project create --org "$ORG" --name {{projectQ}} --visibility private
az repos create --org "$ORG" --project {{projectQ}} --name {{repoQ}}
ADMINS=$(az devops security group list --org "$ORG" --project {{projectQ}} \
  --query "graphGroups[?displayName=='Project Administrators'].descriptor | [0]" -o tsv)
az devops security group membership add --org "$ORG" --group-id "$ADMINS" --member-id {{owner}}
```

Readers, Contributors, the group rule and the parallel job are clicks in the
web portal (Project settings -> Permissions; Organization settings -> Users;
Organization settings -> Parallel jobs).

### Values to send back

| Value | Where to find it |
|---|---|
| repo URL | `{{repoUrl}}` (Repos -> Clone -> HTTPS, without the user name) |
| parallel job | granted yes / no |

### How to verify

```bash
az repos show --org {{orgUrl}} --project {{projectQ}} --repository {{repoQ}} --query webUrl -o tsv
az devops extension show --org {{orgUrl}} \
  --publisher-id AmazonWebServices --extension-id aws-vsts-tools --query extensionName -o tsv
```

Expected: the repo URL above, and `aws-vsts-tools`.
