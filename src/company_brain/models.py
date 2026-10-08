from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["notion", "drive", "linear", "fixture"]
    external_id: str = Field(min_length=1, max_length=500)
    collection: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=2000)
    text: str = Field(max_length=5_000_000)
    url: str = Field(max_length=4000)
    modified_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("modified_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("modified_at requires timezone")
        return value

    @field_validator("url")
    @classmethod
    def safe_url(cls, value):
        p = urlsplit(value)
        if p.scheme != "https" or not p.hostname or p.username or p.password:
            raise ValueError("Source URLs must be HTTPS without credentials")
        return value


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["notion", "drive", "linear", "fixture"]
    collection: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    complete: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    documents: list[Document] = Field(default_factory=list, max_length=100000)
    errors: list[Any] = Field(default_factory=list)
    authorization_failed: bool = False
    quarantine_all: bool = False
    quarantined_external_ids: list[str] = Field(default_factory=list)
    deleted_external_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        ids = []
        for doc in self.documents:
            if (doc.source, doc.collection) != (self.source, self.collection):
                raise ValueError("Snapshot documents must belong to its source and collection")
            ids.append(doc.external_id)
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate source IDs in snapshot")
        if set(ids) & set(self.deleted_external_ids):
            raise ValueError("Snapshot contains an explicitly deleted document")
        return self
