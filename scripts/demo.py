#!/usr/bin/env python3
"""Local synthetic demo; credentials stay in runtime/demo and listeners on loopback."""

import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote

import httpx
import psycopg


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "demo"


def private_write(path, data):
    """Only write regular private files; never follow a pre-existing symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(data)


def credentials(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise RuntimeError("The demo runtime directory must not be a symlink")
    directory.chmod(0o700)
    path = directory / "credentials.json"
    if path.is_symlink():
        raise RuntimeError("The demo credential file must not be a symlink")
    if path.exists():
        path.chmod(0o600)
        result = json.loads(path.read_text())
    else:
        result = {name: secrets.token_urlsafe(36) for name in ("admin", "reader", "ingest")}
        private_write(path, json.dumps(result))
    private_write(directory / "compose.env", f"BRAIN_DB_PASSWORD={result['admin']}\n")
    return result


class PublicJwksHandler(BaseHTTPRequestHandler):
    """Serve only public JWKS, never a directory containing keys or bearer tokens."""

    def __init__(self, *args, public_file, **kwargs):
        self.public_file = public_file
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path != "/jwks.json":
            self.send_error(404)
            return
        body = self.public_file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def compose(*args):
    return ["docker", "compose", "--env-file", str(RUNTIME / "compose.env"),
            "-f", str(ROOT / "deploy" / "compose.demo.yaml"), *args]


def database_url(user, password):
    return f"postgresql://{user}:{quote(password, safe='')}@127.0.0.1:54329/company_brain_demo"


def run(command, env, log):
    result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=log)
    if result.returncode:
        raise RuntimeError(f"Setup failed; inspect the private log at {RUNTIME / 'demo.log'}")


def start():
    for port in (8000, 8011):
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(f"Loopback port {port} is already occupied") from None
    secret = credentials(RUNTIME)
    private_write(RUNTIME / "demo.log", "")
    child = None
    server = None
    with (RUNTIME / "demo.log").open("a") as log:
        env = os.environ.copy()
        # Synthetic mode cannot inherit a caller's production identity configuration.
        env.update({
            "DATABASE_URL": database_url("brain", secret["admin"]),
            "BRAIN_PROFILE": "local-smoke",
            "BRAIN_BASE_URL": "http://127.0.0.1:8000",
            "BRAIN_OAUTH_ISSUER": "http://127.0.0.1:8011",
            "BRAIN_OAUTH_AUDIENCE": "http://127.0.0.1:8000/mcp",
            "BRAIN_OAUTH_JWKS_URL": "http://127.0.0.1:8011/jwks.json",
            "BRAIN_HOST": "127.0.0.1",
            "BRAIN_PORT": "8000",
            "BRAIN_SOURCE_TTL_SECONDS": "86400",
            "DEMO_READER_PASSWORD": secret["reader"],
            "DEMO_INGEST_PASSWORD": secret["ingest"],
        })
        print("Starting isolated demo database on 127.0.0.1:54329…", flush=True)
        try:
            run(compose("up", "-d", "--wait", "db"), env, log)
            for attempt in range(30):
                try:
                    with psycopg.connect(env["DATABASE_URL"], connect_timeout=2):
                        break
                except psycopg.OperationalError:
                    if attempt == 29:
                        raise RuntimeError("Demo database did not become ready") from None
                    time.sleep(1)
            run([sys.executable, "-m", "company_brain", "init-db"], env, log)
            run([sys.executable, "ops/database-roles.py", "--expected-database",
                 "company_brain_demo", "--admin-url-env", "DATABASE_URL",
                 "--reader-password-env", "DEMO_READER_PASSWORD",
                 "--ingest-password-env", "DEMO_INGEST_PASSWORD"], env, log)
            for arguments in (["collection-set", "pilot"],
                              ["user-set", "synthetic-pilot-user", "--collection", "pilot"]):
                run([sys.executable, "-m", "company_brain", *arguments], env, log)
            run([sys.executable, "scripts/create_smoke_identity.py", str(RUNTIME / "identity")],
                env, log)
            print("Indexing fictional fixtures with a local multilingual model. "
                  "The first run downloads the pinned model…", flush=True)
            ingest_env = env | {"DATABASE_URL": database_url("brain_ingest", secret["ingest"])}
            run([sys.executable, "-m", "company_brain", "ingest-snapshot", "fixtures/pilot.json"],
                ingest_env, log)
            server = ThreadingHTTPServer(("127.0.0.1", 8011), partial(
                PublicJwksHandler, public_file=RUNTIME / "identity" / "jwks.json"))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            reader_env = env | {"DATABASE_URL": database_url("brain_reader", secret["reader"])}
            reader_env.pop("DEMO_READER_PASSWORD")
            reader_env.pop("DEMO_INGEST_PASSWORD")
            child = subprocess.Popen([sys.executable, "-m", "company_brain", "serve"],
                                     cwd=ROOT, env=reader_env, stdout=log, stderr=log)
            for _ in range(300):
                if child.poll() is not None:
                    raise RuntimeError("Demo service exited; inspect runtime/demo/demo.log")
                try:
                    response = httpx.get("http://127.0.0.1:8000/.well-known/"
                                         "oauth-protected-resource/mcp", timeout=2, trust_env=False)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Demo service did not become ready")
            print("Ready: http://127.0.0.1:8000/mcp (authenticated, local only).\n"
                  "In another terminal: uv run python scripts/acceptance_client.py\n"
                  "Synthetic tokens expire in one hour; restart the demo to renew them.\n"
                  "Ctrl+C stops services and preserves the demo database.", flush=True)
            child.wait()
            if child.returncode:
                raise RuntimeError("Demo service exited; inspect runtime/demo/demo.log")
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if server is not None:
                server.shutdown()
                server.server_close()
            subprocess.run(compose("down"), cwd=ROOT, env=env, stdout=log, stderr=log)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["up", "stop"])
    args = parser.parse_args()
    os.chdir(ROOT)
    try:
        if args.command == "up":
            start()
        elif (RUNTIME / "compose.env").exists():
            subprocess.run(compose("down"), check=True)
    except KeyboardInterrupt:
        print("Demo stopped; local data preserved.")
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
