## GitLab: steps for team {{team}}

EKS never calls GitLab. You do these steps in GitLab yourself.

1. Make the project `{{repo}}` from the template: copy `templates/team-content/`
   of the EKS repo into it, without `.github/` (its README has the steps).
2. Run the command. It deploys AWS and loads the documents.
3. Commit the `deployment.json` the command wrote (it holds region and sign-in).
4. Manage -> Members: `{{readers}}` Reporter, `{{publishers}}` Developer.
5. Settings -> Repository -> Protected branches: protect `main`.

A push does not deploy yet: run the command again after a change.
