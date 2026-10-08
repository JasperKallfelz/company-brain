from dataclasses import replace
from types import SimpleNamespace
import time
import pytest
from company_brain.auth import JWTVerifier
from company_brain.config import Settings


def test_missing_auth_fails_closed():
    with pytest.raises(ValueError):
        Settings(database_url="postgresql://unused").validate(serving=True)


def test_local_http_requires_explicit_profile(settings):
    local = replace(
        settings,
        issuer="http://127.0.0.1:8765",
        jwks_url="http://127.0.0.1:8765/jwks.json",
        base_url="http://127.0.0.1:8000",
        audience="http://127.0.0.1:8000/mcp",
    )
    with pytest.raises(ValueError):
        local.validate(serving=True)
    replace(local, profile="local-smoke").validate(serving=True)
    with pytest.raises(ValueError):
        replace(local, profile="local-smoke", jwks_url="http://192.168.1.1/jwks").validate(
            serving=True
        )


@pytest.mark.asyncio
async def test_signed_jwt_and_immediate_user_revocation(settings, token_factory):
    active = {"alice"}
    verifier = JWTVerifier(
        settings, SimpleNamespace(active_user=lambda s: s in active), token_factory.keys
    )
    token = token_factory()
    assert (await verifier.verify_token(token)).subject == "alice"
    active.clear()
    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "https://another-resource.example/mcp"},
        {"iss": "https://other-issuer.example"},
        {"exp": int(time.time()) - 10},
        {"scope": "brain:propose"},
        {"sub": "unknown"},
        {"iat": int(time.time()) + 3600},
    ],
)
async def test_invalid_tokens_rejected(settings, token_factory, claims):
    verifier = JWTVerifier(
        settings, SimpleNamespace(active_user=lambda s: s == "alice"), token_factory.keys
    )
    assert await verifier.verify_token(token_factory(**claims)) is None


@pytest.mark.asyncio
async def test_unsigned_and_db_failure_rejected(settings, token_factory):
    def fail(_):
        raise RuntimeError("offline")

    verifier = JWTVerifier(settings, SimpleNamespace(active_user=fail), token_factory.keys)
    assert await verifier.verify_token("not-a-jwt") is None
    assert await verifier.verify_token(token_factory()) is None
