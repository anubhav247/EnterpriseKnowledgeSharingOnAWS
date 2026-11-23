"""Generic OIDC sign-in settings for the MCP gateway. Names no provider.

The gateway's CUSTOM_JWT authorizer is built only from these settings
(infra/stacks/kb_mcp_stack.py). Provider presets in identity/<provider>/ map
their own settings to this one shape; docs/identity.md shows how.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The claim AgentCore's allowedClients setting checks natively.
NATIVE_CLIENT_CLAIM = "client_id"


@dataclass(frozen=True)
class OidcSettings:
    discovery_url: str
    allowed_audiences: tuple[str, ...] = ()
    # Empty = any calling app. The gate is then audience and/or scope: a token
    # issued FOR this server, whichever MCP client asked for it.
    allowed_clients: tuple[str, ...] = ()
    # Scopes the token must carry (Cognito: <prefix>-api/kb.read). Also what
    # the gateway advertises, so MCP clients request them.
    allowed_scopes: tuple[str, ...] = ()
    # Which token claim names the calling app: client_id (Cognito, Auth0),
    # azp (Entra ID, Keycloak, Google), cid (Okta).
    client_claim: str = NATIVE_CLIENT_CLAIM
    # Extra claims that must equal a fixed value, e.g. (("tid", "<tenant>"),).
    required_claims: tuple[tuple[str, str], ...] = ()
    # Only for the gateway description.
    label: str = "OIDC"

    def __post_init__(self) -> None:
        if not self.discovery_url.startswith("https://"):
            raise ValueError(f"discoveryUrl must be an https URL, not {self.discovery_url!r}")
        if not (self.allowed_audiences or self.allowed_scopes or self.allowed_clients):
            raise ValueError("set allowedAudiences or allowedScopes (allowedClients optional)")
        if not self.client_claim:
            raise ValueError("clientClaim must not be empty")
        # shortcut: a non-native client claim is pinned with one EQUALS check,
        # so it takes one client; use CONTAINS_ANY when a deployment needs more.
        if self.client_claim != NATIVE_CLIENT_CLAIM and len(self.allowed_clients) > 1:
            raise ValueError(
                f"clientClaim {self.client_claim!r} takes one allowed client, "
                f"got {len(self.allowed_clients)}"
            )
        for name, value in self.required_claims:
            if not name or not value:
                raise ValueError(f"required claim needs a name and a value: {name}={value}")


def parse_required_claims(items: list[str]) -> tuple[tuple[str, str], ...]:
    """["tid=abc", "token_use=access"] -> (("tid", "abc"), ("token_use", "access"))."""
    out = []
    for item in items:
        name, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"requiredClaims entry must be name=value, not {item!r}")
        out.append((name.strip(), value.strip()))
    return tuple(out)


def from_config(section: dict[str, Any]) -> OidcSettings:
    """The generic `identity.oidc` section of deployment.json."""
    return OidcSettings(
        discovery_url=section.get("discoveryUrl", ""),
        allowed_audiences=tuple(section.get("allowedAudiences") or ()),
        allowed_clients=tuple(section.get("allowedClients") or ()),
        allowed_scopes=tuple(section.get("allowedScopes") or ()),
        client_claim=section.get("clientClaim") or NATIVE_CLIENT_CLAIM,
        required_claims=parse_required_claims(section.get("requiredClaims") or []),
    )
