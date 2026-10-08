#!/usr/bin/env python3
"""Generate generic private configuration; no network, deployment or account changes."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit

from prepare_oauth import make_realm, public_https, write_private


ROOT = Path(__file__).resolve().parents[1]


def prepare(output, brain, identity, callbacks):
    brain = public_https(brain, origin=True)
    identity = public_https(identity, origin=True)
    for origin in (brain, identity):
        parsed = urlsplit(origin)
        if (parsed.port not in (None, 443) or any(char.isspace() for char in origin)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", parsed.hostname)
                or any(not label or len(label) > 63 or label.startswith("-")
                       or label.endswith("-") for label in parsed.hostname.split("."))):
            raise ValueError("Production origins require ordinary DNS names and HTTPS port 443")
    if brain == identity:
        raise ValueError("Use separate Brain and identity hostnames")
    for callback in callbacks.values():
        public_https(callback)
    realm = make_realm(brain, callbacks)
    if output.exists():
        raise ValueError("Output already exists; refusing to overwrite credentials")
    output.mkdir(parents=True, mode=0o700)
    output.chmod(0o700)
    passwords = {name: secrets.token_urlsafe(48) for name in ("admin", "reader", "ingest", "identity", "bootstrap")}
    common = {
        "BRAIN_PROFILE": "production",
        "BRAIN_BASE_URL": brain,
        "BRAIN_OAUTH_ISSUER": identity + "/realms/company-brain",
        "BRAIN_OAUTH_AUDIENCE": brain + "/mcp",
        "BRAIN_OAUTH_JWKS_URL": identity + "/realms/company-brain/protocol/openid-connect/certs",
    }
    files = {
        "compose.env": {"BRAIN_DB_PASSWORD": passwords["admin"], "KC_DB_PASSWORD": passwords["identity"]},
        "admin.env": {
            "DATABASE_URL": f"postgresql://brain:{passwords['admin']}@db:5432/company_brain",
            "READER_PASSWORD": passwords["reader"], "INGEST_PASSWORD": passwords["ingest"]},
        "brain.env": common | {"DATABASE_URL": f"postgresql://brain_reader:{passwords['reader']}@db:5432/company_brain"},
        "sync.env": common | {"DATABASE_URL": f"postgresql://brain_ingest:{passwords['ingest']}@db:5432/company_brain"},
        "keycloak.env": {
            "KC_HOSTNAME": identity, "KC_BOOTSTRAP_ADMIN_USERNAME": "brain-local-admin",
            "KC_BOOTSTRAP_ADMIN_PASSWORD": passwords["bootstrap"],
            "KC_DB_USERNAME": "brain_identity", "KC_DB_PASSWORD": passwords["identity"]},
    }
    for name, values in files.items():
        write_private(output / name, "".join(f"{key}={value}\n" for key, value in values.items()))
    write_private(output / "company-brain-realm.json", json.dumps(realm, indent=2) + "\n")
    template = (ROOT / "deploy/identity/nginx.conf.template").read_text()
    gateway = template.replace("__BRAIN_HOST__", urlsplit(brain).hostname).replace("__IDENTITY_HOST__", urlsplit(identity).hostname)
    write_private(output / "nginx.conf", gateway)
    # This file contains no secrets and must be readable by unprivileged nginx.
    os.chmod(output / "nginx.conf", 0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brain-origin", required=True)
    parser.add_argument("--identity-origin", required=True)
    parser.add_argument("--chatgpt-callback")
    parser.add_argument("--claude-callback")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/production")
    args = parser.parse_args()
    callbacks = {name: value for name, value in {
        "chatgpt": args.chatgpt_callback, "claude": args.claude_callback}.items() if value}
    try:
        prepare(args.output, args.brain_origin, args.identity_origin, callbacks)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print("Private production configuration prepared. No services or accounts were created.")


if __name__ == "__main__":
    main()
