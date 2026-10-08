from starlette.testclient import TestClient
from company_brain.auth import JWTVerifier
from company_brain.server import create_app
from .conftest import StubEmbedder


class MemoryStore:
    active = True

    def active_user(self, subject):
        return self.active and subject == "alice"

    def fetch(self, subject, identifier, cursor):
        return {"id": identifier, "text": "Synthetic source only", "next_cursor": None}

    def search(self, subject, query, embedding, limit, collection):
        return {
            "results": [
                {"id": "synthetic", "title": "Synthetic fixture", "url": "https://example.com"}
            ]
        }


def call(client, token, method, params=None, request_id=1):
    return client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
    )


def test_mcp_http_discovery_and_tools_and_revocation(settings, token_factory):
    store = MemoryStore()
    verifier = JWTVerifier(settings, store, token_factory.keys)
    app = create_app(settings, store=store, embedder=StubEmbedder(), verifier=verifier)
    with TestClient(app, base_url=settings.base_url) as client:
        response = client.post("/mcp")
        assert response.status_code == 401
        assert "resource_metadata" in response.headers["www-authenticate"]
        discovery = client.get("/.well-known/oauth-protected-resource/mcp")
        assert discovery.status_code == 200
        assert discovery.json()["resource"] == settings.audience
        token = token_factory()
        response = call(
            client,
            token,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["serverInfo"]["name"] == "Company Brain"
        response = call(client, token, "tools/list")
        assert {t["name"] for t in response.json()["result"]["tools"]} == {
            "search",
            "fetch",
            "propose_update",
        }
        response = call(
            client,
            token,
            "tools/call",
            {"name": "fetch", "arguments": {"document_id": "synthetic"}},
        )
        assert response.status_code == 200, response.text
        assert "Synthetic source" in response.text
        response = call(client, token_factory(aud="https://other.example"), "tools/list")
        assert response.status_code == 401
        store.active = False
        assert call(client, token, "tools/list").status_code == 401


def test_mcp_rejects_untrusted_host(settings, token_factory):
    store = MemoryStore()
    app = create_app(
        settings,
        store=store,
        embedder=StubEmbedder(),
        verifier=JWTVerifier(settings, store, token_factory.keys),
    )
    with TestClient(app, base_url="https://evil.example") as client:
        assert call(client, token_factory(), "tools/list").status_code == 421
