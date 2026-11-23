## GitHub: steps for team {{team}}

EKS never calls GitHub. You do these steps in GitHub yourself.

1. Make the repo `{{repo}}` from the template: copy `templates/team-content/`
   of the EKS repo into it, `.github/` included (its README has the steps).
2. Get the repo ids (the deploy role trusts them):
   `gh api repos/{{repo}} --jq '"\(.owner.login)@\(.owner.id)/\(.name)@\(.id)"'`.
   Pass them to the command as `--github-repo-ids`.
3. Run the command. It makes the deploy role in AWS and prints its ARN.
4. Settings -> Secrets and variables -> Actions -> Variables, add:
   - `EKS_TEAM` = `{{team}}`
   - `EKS_DEPLOY_ROLE_ARN` = `{{roleArn}}`
   - `AWS_REGION` = `{{region}}`
   - `EKS_REPO` = `{{eksRepo}}`
   - `EKS_REF` = `{{eksRef}}`
5. If the EKS repo is private: add secret `EKS_REPO_TOKEN`, a read-only token
   for `{{eksRepo}}`.
6. Commit the `deployment.json` the command wrote (it holds region and sign-in),
   so a push deploys with the same values.
7. Settings -> Collaborators and teams: `{{readers}}` Read,
   `{{publishers}}` Write.
8. Settings -> Branches: protect `main` (pull request and one review).

Check: edit a document on `main`; the `eks-deploy` workflow goes green.
