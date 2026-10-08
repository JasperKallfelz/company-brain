"""Bounded, explicit source sync. Run locally in the VM, never as a public tool."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .embeddings import LocalEmbedder
from .models import Snapshot
from .store import Store


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    source: Literal["notion", "drive", "linear"]
    collection: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    token_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,100}$")
    token_file: str | None = None
    root_page_ids: list[str] = Field(default_factory=list, max_length=100)
    root_database_ids: list[str] = Field(default_factory=list, max_length=100)
    root_folder_ids: list[str] = Field(default_factory=list, max_length=100)
    organization_id: str | None = None
    team_ids: list[str] = Field(default_factory=list, max_length=100)
    auth_type: Literal["oauth", "api_key"] = "oauth"
    max_runtime_seconds: int = Field(default=240, ge=30, le=900)
    max_requests: int = Field(default=3000, ge=1, le=10000)
    max_documents: int = Field(default=2000, ge=1, le=10000)

    @field_validator("organization_id", "team_ids")
    @classmethod
    def linear_ids_are_uuids(cls, value):
        if value is None:
            return value
        values = value if isinstance(value, list) else [value]
        normalized = []
        for item in values:
            parsed = str(UUID(item))
            if parsed != item.lower():
                raise ValueError("Linear organization and team IDs must be canonical UUIDs")
            normalized.append(parsed)
        if len(normalized) != len(set(normalized)):
            raise ValueError("Duplicate Linear team ID")
        return normalized if isinstance(value, list) else normalized[0]

    @model_validator(mode="after")
    def explicit_scope(self):
        if self.enabled:
            if bool(self.token_env) == bool(self.token_file):
                raise ValueError("Enabled scopes require exactly one explicit credential source")
            if self.source == "notion" and not (self.root_page_ids or self.root_database_ids):
                raise ValueError("Enabled Notion scopes require approved roots")
            if self.source == "drive" and not self.root_folder_ids:
                raise ValueError("Enabled Drive scopes require approved folders")
            if self.source == "linear" and not (self.organization_id and self.team_ids):
                raise ValueError("Enabled Linear scopes require an approved organization and teams")
        if self.source == "notion" and self.root_folder_ids:
            raise ValueError("Drive roots cannot be used in a Notion scope")
        if self.source == "drive" and (self.root_page_ids or self.root_database_ids):
            raise ValueError("Notion roots cannot be used in a Drive scope")
        if self.source == "linear" and (
            self.root_page_ids or self.root_database_ids or self.root_folder_ids
        ):
            raise ValueError("Notion and Drive roots cannot be used in a Linear scope")
        if self.source != "linear" and (
            self.organization_id or self.team_ids or self.auth_type != "oauth"
        ):
            raise ValueError("Linear settings cannot be used for another source")
        return self


class SyncConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[SourceConfig] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_scopes(self):
        pairs = [(s.source, s.collection) for s in self.sources]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Duplicate source/collection scope")
        return self


def read_token(config):
    if config.token_env:
        value = os.environ.get(config.token_env, "")
    elif config.token_file:
        path = Path(config.token_file)
        if not path.is_absolute():
            raise ValueError("Credential file must be absolute")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("Credential file requires owner-only access")
            if info.st_uid not in {0, os.geteuid()} or info.st_size > 8192:
                raise ValueError("Invalid credential file owner or size")
            value = handle.read(8193).strip()
    else:
        raise ValueError("Credential source missing")
    if not value or len(value) > 8192 or "\n" in value or "\r" in value:
        raise ValueError("Credential missing or malformed")
    return value


def make_connector(config, token):
    from .connectors import DriveConnector, LinearConnector, NotionConnector, Limits

    limits = Limits(
        max_requests=config.max_requests,
        max_documents=config.max_documents,
        timeout_seconds=10,
        attempts=2,
    )
    if config.source == "notion":
        return NotionConnector(
            token,
            config.root_page_ids,
            config.collection,
            root_database_ids=config.root_database_ids,
            limits=limits,
        )
    if config.source == "linear":
        return LinearConnector(
            token,
            config.team_ids,
            config.collection,
            organization_id=config.organization_id,
            auth_type=config.auth_type,
            limits=limits,
        )
    return DriveConnector(token, config.root_folder_ids, config.collection, limits=limits)


@contextmanager
def deadline(seconds):
    """CLI main-thread deadline; service manager adds a hard outer process timeout."""

    def expired(signum, frame):
        raise TimeoutError("Source sync deadline exceeded")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def atomic_snapshot(path, snapshot):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".snapshot-", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(snapshot.model_dump(mode="json"), handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def run_sync(
    settings,
    config: SyncConfig,
    state_dir: Path,
    *,
    store=None,
    embedder=None,
    connector_factory=make_connector,
):
    store = store or Store(settings)
    embedder = embedder or LocalEmbedder(settings.embedding_model, settings.embedding_revision)
    state_dir = Path(state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = state_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise ValueError("Sync state directory must be owned by service user with mode0700")
    fd = os.open(state_dir / "sync.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    results = []
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for scope in config.sources:
            connector = None
            result = {"source": scope.source, "collection": scope.collection}
            try:
                # Immediately invalidate cached source authorization, even if credentials fail.
                store.quarantine_existing(
                    scope.source,
                    scope.collection,
                    "sync_in_progress" if scope.enabled else "scope_disabled",
                )
                if not scope.enabled:
                    results.append({**result, "state": "disabled"})
                    continue
                with deadline(scope.max_runtime_seconds):
                    connector = connector_factory(scope, read_token(scope))
                    payload = connector.snapshot().to_dict()
                    snapshot = Snapshot.model_validate(payload)
                    if (snapshot.source, snapshot.collection) != (scope.source, scope.collection):
                        raise ValueError("Connector returned wrong scope")
                    atomic_snapshot(state_dir / f"{scope.source}-{scope.collection}.json", snapshot)
                    outcome = store.ingest(snapshot, embedder)
                    results.append({**result, **outcome})
            except Exception:
                # Fixed error code only: provider bodies, paths and credentials must not leak.
                failed = Snapshot(
                    source=scope.source,
                    collection=scope.collection,
                    complete=False,
                    quarantine_all=True,
                    errors=[{"code": "sync_failed"}],
                )
                try:
                    store.quarantine_existing(scope.source, scope.collection, "sync_failed")
                    atomic_snapshot(state_dir / f"{scope.source}-{scope.collection}.json", failed)
                except Exception:
                    pass  # Database access itself fails closed; old source TTL is the fallback.
                results.append({**result, "state": "failed", "error_code": "sync_failed"})
            finally:
                if connector:
                    try:
                        connector.close()
                    except Exception:
                        pass
    return {
        "sources": results,
        "success": all(r["state"] in {"ready", "disabled"} for r in results),
    }
