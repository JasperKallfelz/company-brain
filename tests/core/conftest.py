import os
import time
from types import SimpleNamespace
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from company_brain.config import Settings


@pytest.fixture
def settings():
    return Settings(
        database_url="postgresql://unused",
        issuer="https://identity.example/realm",
        audience="https://brain.example/mcp",
        jwks_url="https://identity.example/jwks",
        base_url="https://brain.example",
        fetch_page_chars=256,
    )


@pytest.fixture
def token_factory(settings):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def issue(**overrides):
        claims = {
            "iss": settings.issuer,
            "aud": settings.audience,
            "sub": "alice",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "scope": "brain:read brain:propose",
        }
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-only"})

    issue.keys = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key())
    )
    return issue


class StubEmbedder:
    """Unit/integration ranking fixture; never a semantic acceptance substitute."""

    model_id = "test-double-only"

    def chunks(self, text):
        for start in range(0, len(text), 128):
            yield text[start : start + 128]

    def encode(self, texts):
        return [[1.0] + [0.0] * 383 for _ in texts]


@pytest.fixture
def database(settings):
    url = os.environ.get("BRAIN_TEST_DATABASE_URL")
    if not url:
        pytest.skip("BRAIN_TEST_DATABASE_URL not set; real PostgreSQL check required separately")
    from dataclasses import replace
    from psycopg.conninfo import conninfo_to_dict
    from company_brain.store import Store

    if "test" not in conninfo_to_dict(url).get("dbname", ""):
        pytest.fail("Use a disposable database whose name contains 'test'")
    store = Store(replace(settings, database_url=url))
    store.init_db()
    with store.connection() as c:
        c.execute("TRUNCATE proposals,chunks,documents,sources,grants,collections,brain_users")
    store.configure_collection("team")
    store.configure_collection("private")
    store.configure_user("alice", ["team"])
    store.configure_user("bob", ["private"])
    return store
