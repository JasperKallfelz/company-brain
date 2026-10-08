#!/usr/bin/env python3
"""One-shot administrator job for a dedicated new production stack."""

import os
from pathlib import Path
import subprocess
import sys


def main():
    subprocess.run([sys.executable, "-m", "company_brain", "init-db"], check=True)
    subprocess.run([
        sys.executable, "/ops/database-roles.py", "--expected-database", "company_brain",
        "--admin-url-env", "DATABASE_URL", "--reader-password-env", "READER_PASSWORD",
        "--ingest-password-env", "INGEST_PASSWORD",
    ], check=True)
    target = Path("/identity/company-brain-realm.json")
    source = Path("/input/realm.json").read_bytes()
    if target.exists():
        if target.read_bytes() != source:
            raise RuntimeError("Existing identity import differs; explicit migration is required")
    else:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(fd, "wb") as handle:
            handle.write(source)
        os.chown(target, 1000, 0)
    print("Schema, restricted database roles and private realm import prepared.")


if __name__ == "__main__":
    main()
