#!/usr/bin/env bash
# Create (or update) the Entra ID app registration developers sign in through.
#
# Re-runnable. Needs an Azure CLI login with rights to create app registrations,
# groups and app role assignments (Application Administrator + Groups Administrator,
# or Global Administrator). Group-based assignment needs Entra ID P1.
#
#   APP_NAME     display name                 (default tfc-kb-mcp)
#   GROUP_NAME   group(s) allowed to sign in, ";" between (default "KB Developers");
#                docs/entra-setup.md: readers;publishers, or publishers only for a draft app
#   MEMBER_UPNS  space-separated users to add (default: the signed-in user)
#   REDIRECT_URIS space-separated loopback redirect URIs for MCP clients
#
# Prints the tenant id and client id the CDK stack needs.
set -euo pipefail

APP_NAME="${APP_NAME:-tfc-kb-mcp}"
GROUP_NAME="${GROUP_NAME:-KB Developers}"
SCOPE_NAME="kb.read"
# Entra ignores the port on http://localhost redirects for public clients, so
# one entry per path covers every port an MCP client picks.
# Kiro 7778, Claude Code 7779 and VS Code as well, as docs/entra-setup.md lists them.
REDIRECT_URIS="${REDIRECT_URIS:-http://localhost http://localhost/oauth/callback http://127.0.0.1/oauth/callback http://localhost:7778/oauth/callback http://localhost:7779/callback http://127.0.0.1:33418 https://vscode.dev/redirect}"

TENANT_ID=$(az account show --query tenantId -o tsv)
echo "tenant: $TENANT_ID"

# ------------------------------------------------------------- app registration
APP_ID=$(az ad app list --display-name "$APP_NAME" --query '[0].appId' -o tsv)
if [[ -z "$APP_ID" ]]; then
  APP_ID=$(az ad app create --display-name "$APP_NAME" \
    --sign-in-audience AzureADMyOrg --query appId -o tsv)
  echo "created app: $APP_ID"
else
  echo "app exists: $APP_ID"
fi
APP_OBJECT_ID=$(az ad app show --id "$APP_ID" --query id -o tsv)

# Keep the scope id stable across re-runs.
SCOPE_ID=$(az ad app show --id "$APP_ID" \
  --query "api.oauth2PermissionScopes[?value=='$SCOPE_NAME'].id | [0]" -o tsv)
[[ -z "$SCOPE_ID" ]] && SCOPE_ID=$(uuidgen | tr 'A-Z' 'a-z')

REDIRECTS_JSON=$(printf '%s\n' $REDIRECT_URIS | python3 -c 'import json,sys;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')

# Public client (no secret, PKCE), v2 access tokens, one delegated scope, and
# the app pre-authorised to request its own scope so users get no consent prompt
# for it. v2 tokens make iss match the v2.0 discovery URL and aud the client id.
BODY=$(cat <<JSON
{
  "identifierUris": ["api://$APP_ID"],
  "isFallbackPublicClient": true,
  "publicClient": {"redirectUris": $REDIRECTS_JSON},
  "api": {
    "requestedAccessTokenVersion": 2,
    "oauth2PermissionScopes": [{
      "id": "$SCOPE_ID",
      "value": "$SCOPE_NAME",
      "type": "User",
      "isEnabled": true,
      "adminConsentDisplayName": "Read the knowledge base",
      "adminConsentDescription": "Search and read documents in the enterprise knowledge base over MCP.",
      "userConsentDisplayName": "Read the knowledge base",
      "userConsentDescription": "Search and read documents in the knowledge base on your behalf."
    }]
  },
  "requiredResourceAccess": [{
    "resourceAppId": "$APP_ID",
    "resourceAccess": [{"id": "$SCOPE_ID", "type": "Scope"}]
  }]
}
JSON
)
az rest --method PATCH \
  --url "https://graph.microsoft.com/v1.0/applications/$APP_OBJECT_ID" \
  --headers "Content-Type=application/json" --body "$BODY" >/dev/null
echo "configured scope api://$APP_ID/$SCOPE_NAME, v2 tokens, public client"

# Pre-authorise needs the scope to exist first, so it is a second PATCH.
az rest --method PATCH \
  --url "https://graph.microsoft.com/v1.0/applications/$APP_OBJECT_ID" \
  --headers "Content-Type=application/json" \
  --body "{\"api\":{\"preAuthorizedApplications\":[{\"appId\":\"$APP_ID\",\"delegatedPermissionIds\":[\"$SCOPE_ID\"]}]}}" >/dev/null

# ------------------------------------------------------------ service principal
SP_ID=$(az ad sp list --filter "appId eq '$APP_ID'" --query '[0].id' -o tsv)
if [[ -z "$SP_ID" ]]; then
  SP_ID=$(az ad sp create --id "$APP_ID" --query id -o tsv)
  echo "created service principal: $SP_ID"
fi
# Only assigned users and groups may get a token for this app.
az ad sp update --id "$SP_ID" --set appRoleAssignmentRequired=true
echo "assignment required: on"

# ------------------------------------------------------------------ the groups
MEMBERS="${MEMBER_UPNS:-$(az ad signed-in-user show --query id -o tsv)}"
IFS=';' read -r -a GROUPS_LIST <<<"$GROUP_NAME"
for GROUP in "${GROUPS_LIST[@]}"; do
  GROUP_ID=$(az ad group list --display-name "$GROUP" --query '[0].id' -o tsv)
  if [[ -z "$GROUP_ID" ]]; then
    NICK=$(echo "$GROUP" | tr -cd 'A-Za-z0-9')
    GROUP_ID=$(az ad group create --display-name "$GROUP" --mail-nickname "$NICK" \
      --description "May sign in to the knowledge base MCP server" --query id -o tsv)
    echo "created group: $GROUP_ID"
  fi

  for m in $MEMBERS; do
    MID=$(az ad user show --id "$m" --query id -o tsv)
    if [[ "$(az ad group member check --group "$GROUP_ID" --member-id "$MID" --query value -o tsv)" != "true" ]]; then
      az ad group member add --group "$GROUP_ID" --member-id "$MID"
      echo "added member: $m"
    fi
  done

  # Assign the group to the app with the default access role.
  ASSIGNED=$(az rest --method GET \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
    --query "value[?principalId=='$GROUP_ID'] | length(@)" -o tsv)
  if [[ "$ASSIGNED" == "0" ]]; then
    az rest --method POST \
      --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
      --headers "Content-Type=application/json" \
      --body "{\"principalId\":\"$GROUP_ID\",\"resourceId\":\"$SP_ID\",\"appRoleId\":\"00000000-0000-0000-0000-000000000000\"}" >/dev/null
    echo "assigned group '$GROUP' to the app"
  fi
done

cat <<OUT

Done. Put these in "identity": {"provider": "entra", "entra": {...}} in deployment.json:
  tenantId = $TENANT_ID
  clientId = $APP_ID
MCP clients request scope:  api://$APP_ID/$SCOPE_NAME
OUT
