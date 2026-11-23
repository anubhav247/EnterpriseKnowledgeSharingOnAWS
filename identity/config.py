"""Read the gateway sign-in settings from deployment.json.

    "gatewayAuth": "iam"                  AWS IAM (SigV4); no identity section
    "gatewayAuth": "oidc", "identity": {
        "provider": "entra"   + "entra":   {"tenantId", "clientId",
                                            "allowedClients": [] (optional)}
        "provider": "cognito" + "cognito": {"create": true}  (stack makes a pool)
                                        or {"userPoolId", "allowedScopes": [...],
                                            "clientIds": [] (optional)}
        "provider": "oidc"    + "oidc":    {"discoveryUrl", "allowedAudiences",
                                            "allowedScopes", "allowedClients",
                                            "clientClaim",
                                            "requiredClaims": ["name=value"]}
    allowedClients / clientIds empty or absent = any MCP client.
        "debugErrors": false }
    no gatewayAuth and no identity        no gateway

Deprecated: a top-level "entra" section (with gatewayAuth absent or "entra")
still works; migrate() turns it into the shape above and prints a note.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from typing import Any

from identity.cognito import preset as cognito
from identity.entra import preset as entra
from identity.oidc import OidcSettings, from_config as generic

MODES = ("none", "iam", "oidc")
PROVIDERS = ("entra", "cognito", "oidc")


@dataclass(frozen=True)
class GatewayAuth:
    mode: str  # "none" | "iam" | "oidc"
    oidc: OidcSettings | None = None
    # oidc mode with no settings yet: the stack creates a Cognito pool and
    # builds the settings from it.
    cognito_create: bool = False
    debug_errors: bool = False


def migrate(cfg: dict[str, Any]) -> dict[str, Any]:
    """Old shape (top-level "entra") -> new shape. Returns a copy."""
    cfg = copy.deepcopy(cfg)
    old = cfg.get("entra") or {}
    if (
        "identity" in cfg
        or cfg.get("gatewayAuth") not in (None, "entra")
        or not (old.get("tenantId") and old.get("clientId"))
    ):
        return cfg
    print(
        'deployment.json: the top-level "entra" section is deprecated. Use '
        '"gatewayAuth": "oidc" and "identity": {"provider": "entra", "entra": {...}} '
        "(docs/identity.md).",
        file=sys.stderr,
    )
    cfg.pop("entra")
    cfg["gatewayAuth"] = "oidc"
    cfg["identity"] = {
        "provider": "entra",
        "entra": {"tenantId": old["tenantId"], "clientId": old["clientId"]},
        "debugErrors": bool(old.get("debugErrors", False)),
    }
    return cfg


def provider(cfg: dict[str, Any]) -> str:
    ident = cfg.get("identity") or {}
    name = ident.get("provider") or next((p for p in PROVIDERS if p in ident), "oidc")
    if name not in PROVIDERS:
        raise ValueError(f"identity.provider must be one of {PROVIDERS}, not {name!r}")
    return name


def resolve(cfg: dict[str, Any], region: str) -> GatewayAuth:
    cfg = migrate(cfg)
    ident = cfg.get("identity") or {}
    mode = cfg.get("gatewayAuth") or ("oidc" if ident else "none")
    if mode not in MODES:
        raise ValueError(f'gatewayAuth must be "iam" or "oidc", not {mode!r}')
    if mode != "oidc":
        return GatewayAuth(mode)
    debug = bool(ident.get("debugErrors", False))
    name = provider(cfg)
    section = ident.get(name) or {}
    if name == "entra":
        return GatewayAuth(mode, entra.from_config(section), debug_errors=debug)
    if name == "cognito":
        if section.get("create"):
            return GatewayAuth(mode, cognito_create=True, debug_errors=debug)
        return GatewayAuth(mode, cognito.from_config(section, region), debug_errors=debug)
    return GatewayAuth(mode, generic(section), debug_errors=debug)
