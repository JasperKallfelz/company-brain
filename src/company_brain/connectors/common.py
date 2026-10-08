"""Read-only provider transport and fail-closed snapshot contract.

Provider payloads and credentials never become exception messages. Only fixed
origins/routes are used; source links and redirects are never fetch targets.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx


class ConnectorError(Exception):
    def __init__(self, code: str, *, status: int | None = None):
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass
class Snapshot:
    source: str
    collection: str
    documents: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True
    errors: list[dict[str, Any]] = field(default_factory=list)
    authorization_failed: bool = False
    quarantine_all: bool = False
    quarantined_external_ids: list[str] = field(default_factory=list)
    deleted_external_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def fail(self, error: ConnectorError, external_id: str | None = None) -> None:
        self.complete = False
        # An interrupted enumeration may hide a permission change. Retain the
        # previous index for recovery, but do not serve this scope until repaired.
        self.quarantine_all = True
        item: dict[str, Any] = {"code": error.code}
        if error.status is not None:
            item["status"] = error.status
        if external_id:
            item["external_id"] = external_id
            if external_id not in self.quarantined_external_ids:
                self.quarantined_external_ids.append(external_id)
        self.errors.append(item)
        if error.status in (401, 403, 404):
            self.authorization_failed = True

    def count(self, key: str, amount: int = 1) -> None:
        counts = self.metadata.setdefault("counts", {})
        counts[key] = counts.get(key, 0) + amount

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Limits:
    max_requests: int = 10_000
    max_documents: int = 2_000
    max_depth: int = 64
    max_response_bytes: int = 16 * 1024 * 1024
    max_total_response_bytes: int = 128 * 1024 * 1024
    max_snapshot_text_bytes: int = 64 * 1024 * 1024
    attempts: int = 3
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if any(value <= 0 for value in asdict(self).values()):
            raise ValueError("connector limits must be positive")
        if self.attempts > 5:
            raise ValueError("at most five attempts are permitted")


def resource_id(value: str) -> str:
    """Provider ID, not a URL/path/query; also prevents Drive query injection."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise ConnectorError("invalid_resource_id")
    return value


def notion_id(value: str) -> str:
    import uuid

    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ConnectorError("invalid_notion_id") from None


def source_url(value: Any, allowed_hosts: set[str], fallback: str) -> str:
    """Links are only output, nevertheless never emit script/data/credential URLs."""
    if isinstance(value, str):
        try:
            parsed = urlsplit(value)
            if (parsed.scheme == "https" and parsed.hostname in allowed_hosts
                    and not parsed.username and not parsed.password and parsed.port in (None, 443)):
                return value
        except ValueError:
            pass
    return fallback


class ProviderHTTP:
    ORIGINS = {"https://api.notion.com", "https://www.googleapis.com",
               "https://docs.googleapis.com", "https://api.linear.app"}

    def __init__(self, token: str, *, client: httpx.Client | None = None,
                 limits: Limits | None = None, headers: dict[str, str] | None = None,
                 sleep: Callable[[float], None] = time.sleep, interval: float = 0.0):
        if not token or "\n" in token or "\r" in token:
            raise ValueError("a nonempty provider token is required")
        self.limits = limits or Limits()
        self.headers = {"Authorization": f"Bearer {token}", **(headers or {})}
        self._own_client = client is None
        self.client = client or httpx.Client(follow_redirects=False, trust_env=False)
        self.sleep = sleep
        self.interval = interval
        self.requests = 0
        self.response_bytes = 0

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    def reset(self) -> None:
        self.requests = 0
        self.response_bytes = 0

    def request(self, method: str, origin: str, path: str, *,
                params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None) -> bytes:
        if origin not in self.ORIGINS or not path.startswith("/") or path.startswith("//"):
            raise ConnectorError("disallowed_destination")
        if method not in {"GET", "POST"}:
            raise ConnectorError("disallowed_method")
        # Linear admits only the fixed read queries owned by its connector;
        # caller-supplied GraphQL (including mutations) never reaches transport.
        linear_query = False
        if origin == "https://api.linear.app":
            from .linear import READ_QUERIES

            linear_query = (method == "POST" and path == "/graphql"
                            and isinstance(body, dict)
                            and isinstance(body.get("query"), str)
                            and body.get("query") in READ_QUERIES)
            if not linear_query:
                raise ConnectorError("disallowed_method")
        if method == "POST" and not (linear_query or (origin == "https://api.notion.com" and
                re.fullmatch(r"/v1/data_sources/[0-9a-f-]+/query", path))):
            raise ConnectorError("disallowed_method")
        for attempt in range(self.limits.attempts):
            if self.requests >= self.limits.max_requests:
                raise ConnectorError("request_limit")
            if self.requests and self.interval:
                self.sleep(self.interval)
            self.requests += 1
            try:
                with self.client.stream(method, origin + path, params=params, json=body,
                                        headers=self.headers, follow_redirects=False,
                                        timeout=self.limits.timeout_seconds) as response:
                    status = response.status_code
                    if status == 429 or 500 <= status <= 599:
                        if attempt + 1 == self.limits.attempts:
                            raise ConnectorError("provider_unavailable", status=status)
                        try:
                            delay = float(response.headers.get("Retry-After", 2**attempt))
                        except ValueError:
                            delay = float(2**attempt)
                        self.sleep(min(10.0, max(0.0, delay)))
                        continue
                    if not 200 <= status <= 299:
                        raise ConnectorError("provider_http_error", status=status)
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        self.response_bytes += len(chunk)
                        if size > self.limits.max_response_bytes:
                            raise ConnectorError("response_size_limit")
                        # Bound aggregate memory before a huge property/page is
                        # fully assembled, not only when adding the final doc.
                        if self.response_bytes > self.limits.max_total_response_bytes:
                            raise ConnectorError("total_response_size_limit")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.HTTPError:
                if attempt + 1 == self.limits.attempts:
                    raise ConnectorError("provider_transport_error") from None
                self.sleep(float(2**attempt))
        raise ConnectorError("provider_unavailable")

    def json(self, method: str, origin: str, path: str, **kwargs: Any) -> dict[str, Any]:
        raw = self.request(method, origin, path, **kwargs)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise ConnectorError("invalid_provider_response") from None


def add_document(snapshot: Snapshot, document: dict[str, Any], limits: Limits) -> None:
    if len(snapshot.documents) >= limits.max_documents:
        raise ConnectorError("document_limit")
    size = len(document["text"].encode("utf-8"))
    total = snapshot.metadata.get("text_bytes", 0) + size
    if total > limits.max_snapshot_text_bytes:
        raise ConnectorError("snapshot_size_limit")
    snapshot.metadata["text_bytes"] = total
    snapshot.documents.append(document)
