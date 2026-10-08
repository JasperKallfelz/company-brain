#!/usr/bin/env python3
"""Generate synthetic local-only JWT test identities, never print secret material."""
import argparse
import json
import os
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--subject", default="synthetic-pilot-user")
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    key_path = directory / "private.pem"
    if key_path.exists():
        private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    else:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public.update(kid="synthetic-smoke", use="sig", alg="RS256")
    (directory / "jwks.json").write_text(json.dumps({"keys": [public]}))
    now = int(time.time())
    claims = {"iss": "http://127.0.0.1:8011", "aud": "http://127.0.0.1:8000/mcp", "sub": args.subject,
              "iat": now, "exp": now + 3600, "scope": "brain:read brain:propose"}
    variants = {"valid": {}, "expired": {"iat": now - 7200, "exp": now - 3600},
                "wrong-audience": {"aud": "different-service"}, "wrong-issuer": {"iss": "https://example.invalid"},
                "unlisted-user": {"sub": "synthetic-unlisted-user"}}
    for label, changes in variants.items():
        token = jwt.encode(claims | changes, private, algorithm="RS256", headers={"kid": "synthetic-smoke"})
        path = directory / f"{label}.token"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
    print(json.dumps({"status": "synthetic identities generated", "directory": str(directory),
                      "public_file": "jwks.json", "subject": args.subject, "expires_in_seconds": 3600}))


if __name__ == "__main__":
    main()
