from dataclasses import dataclass
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    database_url: str
    issuer: str = ""
    audience: str = ""
    jwks_url: str = ""
    base_url: str = ""
    profile: str = "production"
    source_ttl_seconds: int = 1800
    fetch_page_chars: int = 12000
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_revision: str = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.environ.get("DATABASE_URL", ""),
            issuer=os.environ.get("BRAIN_OAUTH_ISSUER", ""),
            audience=os.environ.get("BRAIN_OAUTH_AUDIENCE", ""),
            jwks_url=os.environ.get("BRAIN_OAUTH_JWKS_URL", ""),
            base_url=os.environ.get("BRAIN_BASE_URL", "").rstrip("/"),
            profile=os.environ.get("BRAIN_PROFILE", "production"),
            source_ttl_seconds=int(os.environ.get("BRAIN_SOURCE_TTL_SECONDS", "1800")),
            fetch_page_chars=int(os.environ.get("BRAIN_FETCH_PAGE_CHARS", "12000")),
        )

    def validate(self, serving=False):
        if not self.database_url:
            raise ValueError("DATABASE_URL must be configured")
        if self.profile not in {"production", "local-smoke"}:
            raise ValueError("Unknown BRAIN_PROFILE")
        if not 60 <= self.source_ttl_seconds <= 86400:
            raise ValueError("Source TTL must be between 60 and 86400 seconds")
        if not 256 <= self.fetch_page_chars <= 32000:
            raise ValueError("Fetch page size must be between 256 and 32000 characters")
        if serving:
            for name in ("issuer", "audience", "jwks_url", "base_url"):
                value = getattr(self, name)
                parsed = urlsplit(value)
                local = (
                    self.profile == "local-smoke"
                    and parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                )
                if (
                    not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.fragment
                    or parsed.query
                ):
                    raise ValueError(f"Configure valid {name}; authentication is mandatory")
                if parsed.scheme != "https" and not local:
                    raise ValueError(f"{name} requires HTTPS (loopback HTTP only in local-smoke)")
            if self.audience != self.base_url + "/mcp":
                raise ValueError("Audience must equal BRAIN_BASE_URL + /mcp")
