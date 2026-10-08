"""MCP OAuth resource server: no identity-provider proxy or token forwarding."""

import asyncio
import jwt
from mcp.server.auth.provider import AccessToken


class JWTVerifier:
    def __init__(self, settings, store, key_client=None):
        settings.validate(serving=True)
        self.settings = settings
        self.store = store
        # URL is operator configuration, never taken from JWT jku/x5u or tool input.
        self.keys = key_client or jwt.PyJWKClient(
            settings.jwks_url, cache_keys=False, lifespan=60, timeout=5
        )

    async def verify_token(self, token):
        try:
            key = await asyncio.to_thread(self.keys.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
            subject = claims["sub"]
            if not isinstance(subject, str) or not subject or len(subject) > 500:
                return None
            scope = claims.get("scope", "")
            if not isinstance(scope, str) or "brain:read" not in scope.split():
                return None
            if not await asyncio.to_thread(self.store.active_user, subject):
                return None
            return AccessToken(
                token=token,
                client_id=claims.get("azp", claims.get("client_id", subject)),
                subject=subject,
                scopes=scope.split(),
                expires_at=claims["exp"],
                resource=self.settings.audience,
                claims={"iss": claims["iss"]},
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OSError):
            return None
        except Exception:
            # Database/IdP outage must not accidentally enable anonymous service.
            # Never log a JWT or document in an authentication error.
            return None
