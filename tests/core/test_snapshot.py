import pytest
from pydantic import ValidationError
from company_brain.models import Snapshot


def document(external_id="one", text="Team decided release on Friday.", collection="team"):
    return {
        "source": "fixture",
        "external_id": external_id,
        "collection": collection,
        "title": "Decision",
        "text": text,
        "url": "https://example.com/decision",
        "modified_at": "2026-10-06T10:00:00Z",
    }


def snapshot(documents=None, **kwargs):
    return Snapshot.model_validate(
        {
            "source": "fixture",
            "collection": "team",
            "complete": True,
            "documents": documents if documents is not None else [document()],
            **kwargs,
        }
    )


def test_snapshot_rejects_cross_scope_and_duplicate_ids():
    with pytest.raises(ValidationError):
        snapshot([document(collection="private")])
    with pytest.raises(ValidationError):
        snapshot([document(), document()])


def test_snapshot_rejects_active_document_in_deletions():
    with pytest.raises(ValidationError):
        snapshot(deleted_external_ids=["one"])


def test_snapshot_contract_supports_connector_quarantine():
    result = snapshot(
        authorization_failed=True,
        quarantine_all=True,
        quarantined_external_ids=["one"],
        errors=[{"code": "authorization_failed"}],
    )
    assert result.authorization_failed
