"""Amazon Cognito preset: user pool + this server's scope -> generic OIDC settings.

Cognito access tokens carry no aud claim, so the gate is the scope: the token
must hold this server's resource-server scope (<prefix>-api/kb.read). Any app
client in the pool that may request that scope works, whichever MCP client
uses it. Another app's scope in the same pool is refused. clientIds is an
optional extra pin (empty = any client). token_use=access keeps id tokens out.
"""

from __future__ import annotations

from typing import Any

from identity.oidc import OidcSettings


def discovery_url(region: str, user_pool_id: str) -> str:
    return (
        f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        "/.well-known/openid-configuration"
    )


def oidc_settings(
    region: str,
    user_pool_id: str,
    scopes: list[str],
    client_ids: list[str] | None = None,
) -> OidcSettings:
    if not user_pool_id or not (scopes or client_ids):
        raise ValueError(
            "Cognito preset needs identity.cognito.userPoolId and allowedScopes "
            "(this server's scope, e.g. <prefix>-api/kb.read)"
        )
    return OidcSettings(
        discovery_url=discovery_url(region, user_pool_id),
        allowed_scopes=tuple(scopes),
        allowed_clients=tuple(client_ids or ()),
        client_claim="client_id",
        required_claims=(("token_use", "access"),),
        label="Amazon Cognito",
    )


def from_config(section: dict[str, Any], region: str) -> OidcSettings:
    """The `identity.cognito` section for an existing pool (not `create`)."""
    return oidc_settings(
        section.get("region") or region,
        section.get("userPoolId", ""),
        list(section.get("allowedScopes") or []),
        list(section.get("clientIds") or []),
    )
