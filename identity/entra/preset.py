"""Entra ID preset: tenant id + app client id -> generic OIDC settings.

Entra v2 tokens: issuer https://login.microsoftonline.com/<tenant>/v2.0 and
audience = the app's client id. The gate is that audience (a token issued for
THIS server) plus the tenant (tid). Any calling app (azp) is fine: a token
another app gets for this API still names this API as its audience. Pin
callers with the optional `allowedClients` (checked against azp; one entry,
see identity/oidc.py).

No allowed scopes. They would both publish scopes_supported (what Kiro requests
from Entra) and require that exact string in the token, but Entra writes the
short form (scp=kb.read), never api://<id>/kb.read. Setting them makes every
Entra token fail with insufficient_scope. Clients send the scope themselves
(mcp-remote, clients/kiro/make_mcp_config.py).
"""

from __future__ import annotations

from typing import Any

from identity.oidc import OidcSettings


def oidc_settings(
    tenant_id: str, client_id: str, allowed_clients: list[str] | None = None
) -> OidcSettings:
    if not tenant_id or not client_id:
        raise ValueError("Entra preset needs identity.entra.tenantId and clientId")
    return OidcSettings(
        discovery_url=(
            f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"
        ),
        allowed_audiences=(client_id,),
        allowed_clients=tuple(allowed_clients or ()),
        client_claim="azp",
        required_claims=(("tid", tenant_id),),
        label="Entra ID",
    )


def from_config(section: dict[str, Any]) -> OidcSettings:
    """The `identity.entra` section: {"tenantId", "clientId", "allowedClients"?}."""
    return oidc_settings(
        section.get("tenantId", ""),
        section.get("clientId", ""),
        list(section.get("allowedClients") or []),
    )
