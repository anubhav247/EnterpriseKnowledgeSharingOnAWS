"""Optional Cognito user pool that the factory owns, for sign-in tests.

On with deployment.json `identity.cognito.create: true`. It makes:

    user pool        no self sign-up, Lite plan; groups readers and publishers
                     (the EKS console adds users, platform/api.py)
    domain           hosts the token endpoint (output CognitoTokenUrl)
    resource server  scope <prefix>-api/kb.read: THIS server. The gateway
                     allows any token holding it, from any app client.
    resource server  scope <prefix>-other-api/read: "another app" in the pool
    client "test"    machine-to-machine (client credentials) with a secret, so
                     a test gets a token without a browser; has kb.read
    client "other"   same flow, only the other app's scope: refused (403)
    client "kiro"    the shared public client (no secret), authorization code +
                     PKCE. Every MCP client uses this one id: callbacks for
                     Kiro, Claude Code and VS Code (PUBLIC_REDIRECT_URIS)
    client "second"  a 2nd public client with kb.read, to prove the gateway
                     takes any client, not one list
    demo secret      (output DemoUserSecretArn): the demo user's name and a
                     generated password. CI creates the user and sets it as
                     permanent (infra/kiro_signin_test.py), so the first sign-in
                     needs no change; the password is never printed.

The client secrets are not outputs: read them with
`aws cognito-idp describe-user-pool-client`. `oidc` holds the generic
settings for the gateway, built by the Cognito preset.
"""

from __future__ import annotations

from aws_cdk import (
    Aws,
    CfnOutput,
    CfnResource,
    RemovalPolicy,
    Stack,
    aws_cognito as cognito,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from identity.cognito import preset

SCOPE = "kb.read"
# Kiro listens here for the sign-in callback (mcp.json oauth.redirectUri).
# Cognito matches callback URLs exactly, so Kiro must not pick a random port.
KIRO_REDIRECT_URI = "http://localhost:7778/oauth/callback"
# Claude Code: oauth.callbackPort 7779 -> http://localhost:7779/callback.
CLAUDE_CALLBACK_PORT = 7779
CLAUDE_REDIRECT_URI = f"http://localhost:{CLAUDE_CALLBACK_PORT}/callback"
# VS Code asks for a client id when there is no client registration and
# returns through its own redirect page.
VSCODE_REDIRECT_URI = "https://vscode.dev/redirect"
PUBLIC_REDIRECT_URIS = [KIRO_REDIRECT_URI, CLAUDE_REDIRECT_URI, VSCODE_REDIRECT_URI]
# shortcut: Cursor and others need their callback added here; add on request.
DEMO_USERNAME = "demo@example.com"
GROUPS = {
    "readers": "May ask the server",
    "publishers": "Edit the documents; may ask the server and the draft server",
}


class CognitoTestIdentity(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, prefix: str, removal: RemovalPolicy):
        super().__init__(scope, construct_id)
        self.pool = pool = cognito.UserPool(
            self,
            "Pool",
            user_pool_name=f"{prefix}-test-users",
            self_sign_up_enabled=False,
            feature_plan=cognito.FeaturePlan.LITE,
            removal_policy=removal,
        )
        # The team's two groups (docs/access-model.md); the EKS console adds users to them.
        for name, about in GROUPS.items():
            cognito.CfnUserPoolGroup(
                self,
                f"Group{name.title()}",
                user_pool_id=pool.user_pool_id,
                group_name=name,
                description=about,
            )
        api_scope = cognito.ResourceServerScope(scope_name=SCOPE, scope_description="Read the KB")
        server = pool.add_resource_server("Api", identifier=f"{prefix}-api", scopes=[api_scope])
        domain = pool.add_domain(
            "Domain",
            # Globally unique per region; account id keeps it unique.
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"{prefix}-{Aws.ACCOUNT_ID}"),
        )
        oauth = cognito.OAuthSettings(
            flows=cognito.OAuthFlows(client_credentials=True),
            scopes=[cognito.OAuthScope.resource_server(server, api_scope)],
        )
        test = pool.add_client("TestClient", generate_secret=True, o_auth=oauth)
        # Another app in the same pool: valid tokens, but not for this server.
        other_scope = cognito.ResourceServerScope(scope_name="read", scope_description="Other")
        other_server = pool.add_resource_server(
            "OtherApi", identifier=f"{prefix}-other-api", scopes=[other_scope]
        )
        other = pool.add_client(
            "OtherClient",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[cognito.OAuthScope.resource_server(other_server, other_scope)],
            ),
        )
        kiro_scopes = [
            cognito.OAuthScope.OPENID,
            cognito.OAuthScope.EMAIL,
            cognito.OAuthScope.PROFILE,
            cognito.OAuthScope.resource_server(server, api_scope),
        ]
        kiro = pool.add_client(
            "KiroClient",
            user_pool_client_name=f"{prefix}-kiro",
            generate_secret=False,
            # admin_user_password lets CI sign in a test user without a browser.
            auth_flows=cognito.AuthFlow(admin_user_password=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=kiro_scopes,
                callback_urls=PUBLIC_REDIRECT_URIS,
            ),
            prevent_user_existence_errors=True,
        )
        second = pool.add_client(
            "SecondPublicClient",
            user_pool_client_name=f"{prefix}-second-public",
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=kiro_scopes,
                callback_urls=[CLAUDE_REDIRECT_URI],
            ),
            prevent_user_existence_errors=True,
        )
        self.demo_secret = secretsmanager.Secret(
            self,
            "DemoUser",
            # No fixed name: a deleted secret keeps its name for its 7-30 day
            # recovery window, so a team deleted and added again (EKS console)
            # would fail. Found by its output DemoUserSecretArn.
            description="Demo user for Kiro sign-in; permanent password, reset by each deploy",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=f'{{"username": "{DEMO_USERNAME}"}}',
                generate_string_key="password",
                password_length=20,
                exclude_characters="\"'\\`",
            ),
            removal_policy=removal,
        )
        # shortcut: AWS-managed key for a test-pool demo password; use a CMK
        # if this pool ever holds real users.
        cfn_secret = self.demo_secret.node.default_child
        assert isinstance(cfn_secret, CfnResource)  # noqa: S101  typing only
        cfn_secret.cfn_options.metadata = {
            "checkov": {"skip": [{"id": "CKV_AWS_149", "comment": "demo password, test pool"}]}
        }

        # The gate is this server's scope; no client list.
        self.oidc = preset.oidc_settings(
            Stack.of(self).region, pool.user_pool_id, [f"{prefix}-api/{SCOPE}"]
        )
        CfnOutput(scope, "CognitoUserPoolId", value=pool.user_pool_id)
        CfnOutput(scope, "CognitoTestClientId", value=test.user_pool_client_id)
        CfnOutput(scope, "CognitoOtherClientId", value=other.user_pool_client_id)
        CfnOutput(scope, "CognitoTokenUrl", value=f"{domain.base_url()}/oauth2/token")
        CfnOutput(scope, "CognitoScope", value=f"{prefix}-api/{SCOPE}")
        CfnOutput(scope, "CognitoOtherScope", value=f"{prefix}-other-api/read")
        CfnOutput(scope, "CognitoSecondClientId", value=second.user_pool_client_id)
        CfnOutput(scope, "CognitoSecondRedirectUri", value=CLAUDE_REDIRECT_URI)
        CfnOutput(scope, "CognitoKiroClientId", value=kiro.user_pool_client_id)
        CfnOutput(scope, "CognitoKiroScopes", value=" ".join(s.scope_name for s in kiro_scopes))
        CfnOutput(scope, "CognitoKiroRedirectUri", value=KIRO_REDIRECT_URI)
        CfnOutput(scope, "CognitoLoginBaseUrl", value=domain.base_url())
        CfnOutput(scope, "DemoUserSecretArn", value=self.demo_secret.secret_arn)
        CfnOutput(scope, "DemoUsername", value=DEMO_USERNAME)
