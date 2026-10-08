#!/usr/bin/env python3
"""Prepare private Keycloak configuration after real HTTPS URLs are selected.

This only writes operator-owned files. It never starts a service, changes DNS,
creates users, or enables public access. Client secrets are never printed.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

OFFICIAL_CALLBACKS = {
    "chatgpt": "https://chatgpt.com/connector_platform_oauth_redirect",
    "claude": "https://claude.ai/api/mcp/auth_callback",
}


def subject_mapper():
    # Keycloak 26.x no longer adds access-token sub without this mapper.
    # Direct client attachment avoids adding profile/email or other scopes.
    return {"name": "company-brain-subject", "protocol": "openid-connect",
            "protocolMapper": "oidc-sub-mapper", "config": {
                "access.token.claim": "true", "introspection.token.claim": "true"}}


def public_https(value, origin=False):
    p = urlsplit(value)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment or "*" in value:
        raise ValueError("Use exact public HTTPS URLs without credentials, wildcards or fragments")
    if p.hostname in {"localhost", "example.invalid"} or p.hostname.endswith((".local", ".internal", ".invalid")):
        raise ValueError("A real public hostname is required")
    try:
        ipaddress.ip_address(p.hostname)
    except ValueError:
        pass
    else:
        raise ValueError("Use a DNS hostname, not a literal IP")
    if origin and (p.path not in {"", "/"} or p.query):
        raise ValueError("Identity and Brain origins must not contain a path or query")
    return value.rstrip("/") if origin else value


def make_realm(brain_origin, callbacks, bootstrap_only=False):
    if bootstrap_only and callbacks:
        raise ValueError("Bootstrap-only realm cannot contain external clients")
    if not callbacks and not bootstrap_only:
        raise ValueError("Provide a real callback or select bootstrap-only explicitly")
    audience = brain_origin + "/mcp"
    scopes = []
    for name in ("brain:read", "brain:propose"):
        scopes.append({"name": name, "protocol": "openid-connect", "attributes": {
            "include.in.token.scope": "true", "display.on.consent.screen": "true"},
            "protocolMappers": [{"name": "company-brain-audience", "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper", "config": {
                    "included.custom.audience": audience, "access.token.claim": "true", "id.token.claim": "false"}}]})
    clients = []
    for name, callback in callbacks.items():
        clients.append({"clientId": "company-brain-" + name, "name": "Company Brain for " + name,
                        "enabled": True, "protocol": "openid-connect", "publicClient": False,
                        "secret": secrets.token_urlsafe(48), "standardFlowEnabled": True,
                        "directAccessGrantsEnabled": False, "implicitFlowEnabled": False,
                        "serviceAccountsEnabled": False, "fullScopeAllowed": False,
                        "consentRequired": True, "redirectUris": [callback], "webOrigins": [],
                        "attributes": {"pkce.code.challenge.method": "S256"},
                        "protocolMappers": [subject_mapper()],
                        "defaultClientScopes": ["brain:read"], "optionalClientScopes": ["brain:propose"]})
    return {"realm": "company-brain", "enabled": True, "sslRequired": "external",
            "registrationAllowed": False, "resetPasswordAllowed": False,
            "loginWithEmailAllowed": True, "duplicateEmailsAllowed": False,
            "bruteForceProtected": True, "accessTokenLifespan": 300,
            "revokeRefreshToken": True, "refreshTokenMaxReuse": 0,
            "attributes": {"company-brain.bootstrap-only": str(bootstrap_only).lower()},
            "clientScopes": scopes, "clients": clients}


def write_private(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-origin", required=True)
    parser.add_argument("--brain-origin", required=True)
    parser.add_argument("--chatgpt-callback", help="Exact callback displayed by the real client")
    parser.add_argument("--claude-callback", help="Exact callback displayed by the real client")
    parser.add_argument("--official-clients", action="store_true", help="Use documented stable ChatGPT/Claude callbacks; requires verified RFC9207 issuer responses")
    parser.add_argument("--bootstrap-only", action="store_true", help="Start the issuer with no external clients or end users")
    parser.add_argument("--output", type=Path, default=Path("runtime/identity"))
    args = parser.parse_args()
    try:
        identity = public_https(args.identity_origin, origin=True)
        brain = public_https(args.brain_origin, origin=True)
        explicit = {name: public_https(value) for name, value in {
            "chatgpt": args.chatgpt_callback, "claude": args.claude_callback}.items() if value}
        if (args.official_clients and explicit) or (args.bootstrap_only and (explicit or args.official_clients)):
            raise ValueError("Choose exact callbacks, official clients, or bootstrap-only")
        callbacks = OFFICIAL_CALLBACKS if args.official_clients else explicit
        realm = make_realm(brain, callbacks, args.bootstrap_only)
        if args.output.exists():
            raise ValueError("Output directory already exists; refusing to replace identity secrets")
    except ValueError as exc:
        parser.error(str(exc))
    args.output.mkdir(parents=True, mode=0o700)
    os.chmod(args.output, 0o700)
    write_private(args.output / "company-brain-realm.json", json.dumps(realm, indent=2) + "\n")
    write_private(args.output / "keycloak.env", "\n".join([
        "KC_HOSTNAME=" + identity,
        "KC_BOOTSTRAP_ADMIN_USERNAME=brain-local-admin",
        "KC_BOOTSTRAP_ADMIN_PASSWORD=" + secrets.token_urlsafe(48),
        "KC_DB_USERNAME=brain_identity",
        "KC_DB_PASSWORD=" + secrets.token_urlsafe(48),
    ]) + "\n")
    write_private(args.output / "brain-identity.env", "\n".join([
        "BRAIN_PROFILE=production", "BRAIN_BASE_URL=" + brain,
        "BRAIN_OAUTH_ISSUER=" + identity + "/realms/company-brain",
        "BRAIN_OAUTH_AUDIENCE=" + brain + "/mcp",
        "BRAIN_OAUTH_JWKS_URL=" + identity + "/realms/company-brain/protocol/openid-connect/certs",
    ]) + "\n")
    print(json.dumps({"status": "prepared only; no network or account changes",
                      "directory": str(args.output.resolve()), "client_ids": [c["clientId"] for c in realm["clients"]]}))


if __name__ == "__main__":
    main()
