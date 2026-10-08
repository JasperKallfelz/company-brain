"""Security boundaries introduced by the portable demo and config generator."""

from functools import partial
from http.server import ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import threading

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = load("demo")
production = load("prepare_production")


def test_public_jwks_never_serves_private_files_or_directory(tmp_path):
    (tmp_path / "jwks.json").write_text('{"keys": []}')
    (tmp_path / "private.pem").write_text("synthetic-secret")
    (tmp_path / "valid.token").write_text("synthetic-secret")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(
        demo.PublicJwksHandler, public_file=tmp_path / "jwks.json"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
            assert client.get("/jwks.json").json() == {"keys": []}
            for path in ("/", "/private.pem", "/valid.token", "/%2e%2e/private.pem"):
                response = client.get(path)
                assert response.status_code == 404
                assert "synthetic-secret" not in response.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_demo_credentials_are_private_stable_and_not_shared(tmp_path):
    path = tmp_path / "demo"
    first = demo.credentials(path)
    assert demo.credentials(path) == first
    assert len(set(first.values())) == 3
    assert all(len(value) >= 32 for value in first.values())
    assert path.stat().st_mode & 0o777 == 0o700
    for name in ("credentials.json", "compose.env"):
        assert (path / name).stat().st_mode & 0o777 == 0o600


def test_demo_refuses_symlink_secret_target(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    target = tmp_path / "secret"
    target.symlink_to(outside)
    with pytest.raises(OSError):
        demo.private_write(target, "replacement")
    assert outside.read_text() == "untouched"


def test_production_config_has_scoped_clients_and_separate_credentials(tmp_path):
    output = tmp_path / "prod"
    production.prepare(output, "https://brain.example.com", "https://auth.example.com",
                       {"claude": "https://claude.ai/api/mcp/auth_callback"})
    realm = json.loads((output / "company-brain-realm.json").read_text())
    assert realm["registrationAllowed"] is False
    assert not realm.get("users")
    client = realm["clients"][0]
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["directAccessGrantsEnabled"] is False
    assert client["redirectUris"] == ["https://claude.ai/api/mcp/auth_callback"]
    for path in output.iterdir():
        if path.name != "nginx.conf":
            assert path.stat().st_mode & 0o777 == 0o600
    admin = (output / "admin.env").read_text()
    reader = (output / "brain.env").read_text()
    ingest = (output / "sync.env").read_text()
    assert "brain_reader:" in reader and "brain_ingest:" in ingest
    assert "READER_PASSWORD=" not in reader and "INGEST_PASSWORD=" not in reader
    assert admin.splitlines()[0] not in reader
    gateway = (output / "nginx.conf").read_text()
    assert "__BRAIN_HOST__" not in gateway and "__IDENTITY_HOST__" not in gateway
    assert "server_name brain.example.com;" in gateway
    assert "server_name auth.example.com;" in gateway
    with pytest.raises(ValueError, match="already exists"):
        production.prepare(output, "https://brain.example.com", "https://auth.example.com",
                           {"claude": "https://claude.ai/api/mcp/auth_callback"})


@pytest.mark.parametrize("origin", ["http://brain.example.com", "https://127.0.0.1",
                                     "https://brain.example.com:8443",
                                     "https://brain.example.com;bad",
                                     "https://bad..example.com"])
def test_production_rejects_unsafe_gateway_origins(tmp_path, origin):
    with pytest.raises(ValueError):
        production.prepare(tmp_path / "prod", origin, "https://auth.example.com",
                           {"claude": "https://claude.ai/api/mcp/auth_callback"})


def test_production_requires_exact_client_callback(tmp_path):
    with pytest.raises(ValueError):
        production.prepare(tmp_path / "prod", "https://brain.example.com",
                           "https://auth.example.com", {})
    with pytest.raises(ValueError):
        production.prepare(tmp_path / "prod", "https://brain.example.com",
                           "https://auth.example.com", {"claude": "https://claude.ai/*"})
