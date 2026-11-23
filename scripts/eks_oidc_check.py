"""Check the OIDC values IT sends back, before any deploy. Read-only.

    scripts/add-team.sh --check-oidc --issuer <URL> --client-id <id> \\
        [--audience <aud>] [--scope <scope>] [--token -]

1. Reads <issuer>/.well-known/openid-configuration: its "issuer" must equal
   the one given, its jwks_uri must be https.
2. Reads the JWKS: at least one signing key (RSA or EC) with a kid.
3. Prints what the gateway will enforce, from the same presets the stack uses
   (identity/entra/preset.py for an Entra issuer, else identity/oidc.py).
4. --token -: a test ACCESS token on stdin. Checks iss, aud, scope and expiry.
   It does not check the signature (the gateway does), and prints no token.

Exit 0 all pass, 1 a check failed. Guide: docs/it-request-packs.md.
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import re
import sys
import time
import urllib.request
from typing import Any, Callable

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from identity.entra import preset as entra  # noqa: E402
from identity.oidc import OidcSettings  # noqa: E402

ENTRA_ISSUER = re.compile(r"^https://login\.microsoftonline\.com/([0-9a-f-]{36})/v2\.0$")
Fetch = Callable[[str], dict[str, Any]]


def fetch_json(url: str) -> dict[str, Any]:
    if not url.startswith("https://"):
        raise ValueError(f"{url}: not https")
    with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310 (https only, above)
        return json.load(r)


def settings(issuer: str, client_id: str, audience: str) -> OidcSettings:
    """What the stack builds from the values: install flags --auth entra or --auth oidc."""
    m = ENTRA_ISSUER.match(issuer)
    if m:
        return entra.oidc_settings(m.group(1), client_id)
    return OidcSettings(
        discovery_url=f"{issuer}/.well-known/openid-configuration",
        allowed_audiences=(audience or client_id,),
    )


def install_flags(issuer: str, client_id: str, audience: str) -> list[str]:
    m = ENTRA_ISSUER.match(issuer)
    if m:
        return ["--auth", "entra", "--entra-tenant-id", m.group(1), "--entra-client-id", client_id]
    return [
        "--auth",
        "oidc",
        "--oidc-discovery-url",
        f"{issuer}/.well-known/openid-configuration",
        "--oidc-audience",
        audience or client_id,
    ]


def claims(token: str) -> dict[str, Any]:
    """The payload of a JWT, unverified."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT (header.payload.signature)")
    pad = "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(parts[1] + pad))


def check(
    issuer: str,
    client_id: str,
    audience: str = "",
    scope: str = "",
    token: str = "",
    fetch: Fetch | None = None,
    now: float | None = None,
) -> tuple[bool, list[str]]:
    fetch = fetch or fetch_json
    issuer = issuer.rstrip("/")
    lines: list[str] = []
    ok = True

    def res(passed: bool, name: str, detail: str) -> None:
        nonlocal ok
        ok = ok and passed
        lines.append(f"[{'PASS' if passed else 'FAIL'}] {name:<22} {detail}")

    s = settings(issuer, client_id, audience)
    try:
        disc = fetch(s.discovery_url)
        res(True, "discovery", s.discovery_url)
    except Exception as e:  # noqa: BLE001 (any failure is the answer)
        res(False, "discovery", f"{s.discovery_url}: {e}")
        return False, lines
    got = str(disc.get("issuer", "")).rstrip("/")
    res(got == issuer, "issuer", got if got == issuer else f"discovery says {got!r}")
    jwks_uri = str(disc.get("jwks_uri", ""))
    try:
        keys = [
            k
            for k in fetch(jwks_uri).get("keys", [])
            if k.get("kty") in ("RSA", "EC") and k.get("kid") and k.get("use", "sig") == "sig"
        ]
        res(bool(keys), "signing keys (JWKS)", f"{len(keys)} key(s) at {jwks_uri}")
    except Exception as e:  # noqa: BLE001
        res(False, "signing keys (JWKS)", f"{jwks_uri}: {e}")
    if "authorization_endpoint" in disc:
        pkce = "S256" in (disc.get("code_challenge_methods_supported") or ["S256"])
        res(pkce, "PKCE S256", "supported" if pkce else "not in code_challenge_methods_supported")

    if token:
        try:
            c = claims(token)
        except ValueError as e:
            res(False, "test token", str(e))
        else:
            aud = c.get("aud")
            auds = aud if isinstance(aud, list) else [aud]
            res(str(c.get("iss", "")).rstrip("/") == issuer, "token iss", str(c.get("iss")))
            res(any(a in s.allowed_audiences for a in auds), "token aud", str(aud))
            got_scopes = str(c.get("scp") or c.get("scope") or "").split()
            if isinstance(c.get("scp"), list):
                got_scopes = c["scp"]
            if scope:
                res(scope in got_scopes, "token scope", " ".join(got_scopes) or "none")
            for name, value in s.required_claims:
                res(str(c.get(name)) == value, f"token {name}", str(c.get(name)))
            exp = float(c.get("exp") or 0)
            res(
                exp > (now or time.time()),
                "token not expired",
                time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp)),
            )

    lines += [
        "",
        "the gateway will enforce (CUSTOM_JWT authorizer):",
        f"  discovery URL   {s.discovery_url}",
        f"  issuer          {issuer} (from discovery), signature by its JWKS",
        f"  audience        {', '.join(s.allowed_audiences)}",
        f"  calling app     {', '.join(s.allowed_clients) or 'any'} (claim {s.client_claim})",
    ]
    lines += [f"  claim           {n} = {v}" for n, v in s.required_claims]
    if scope:
        lines.append(
            f"  scope           {scope}: MCP clients request it (not enforced by the gateway)"
        )
    lines += ["", "install flags:  " + " ".join(install_flags(issuer, client_id, audience))]
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="add-team.sh --check-oidc",
        description=__doc__,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--check-oidc", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--issuer", required=True, help="issuer URL, as IT sent it")
    p.add_argument("--client-id", required=True)
    p.add_argument("--audience", default="", help="default: the client id")
    p.add_argument("--scope", default="", help="the scope value IT sent, e.g. kb.read")
    p.add_argument("--token", default="", help="'-': read a test access token from stdin")
    a = p.parse_args(argv)
    if not a.issuer.startswith("https://"):
        p.error("--issuer must be an https URL")
    if a.token not in ("", "-"):
        p.error("--token: give '-' and pipe the token in, so it stays out of shell history")
    token = sys.stdin.read() if a.token else ""
    ok, lines = check(a.issuer, a.client_id, a.audience, a.scope, token)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
