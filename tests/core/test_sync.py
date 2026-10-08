import json
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from company_brain.sync import SourceConfig, SyncConfig, make_connector, read_token, run_sync
from .conftest import StubEmbedder


class FakeStore:
    def __init__(self):
        self.calls = []

    def quarantine_existing(self, source, collection, error):
        self.calls.append(("quarantine", source, collection, error))

    def ingest(self, snapshot, embedder):
        self.calls.append(("ingest", snapshot.complete))
        return {
            "state": "ready" if snapshot.complete else "quarantined",
            "indexed": len(snapshot.documents),
        }


class FakeConnector:
    def __init__(self, complete=True):
        self.complete = complete
        self.closed = False

    def snapshot(self):
        return SimpleNamespace(
            to_dict=lambda: {
                "source": "drive",
                "collection": "pilot",
                "complete": self.complete,
                "documents": [],
                "errors": [],
            }
        )

    def close(self):
        self.closed = True


def config(**kwargs):
    return SyncConfig(
        sources=[
            SourceConfig(
                source="drive",
                collection="pilot",
                enabled=True,
                root_folder_ids=["approved"],
                token_env="BRAIN_TEST_SOURCE_TOKEN",
                **kwargs,
            )
        ]
    )


def test_disabled_by_default_never_reads_token_or_calls_provider(settings, tmp_path):
    store = FakeStore()
    scopes = SyncConfig(sources=[SourceConfig(source="drive", collection="pilot")])

    def forbidden(*args):
        pytest.fail("Disabled config accessed connector")

    result = run_sync(
        settings,
        scopes,
        tmp_path / "private",
        store=store,
        embedder=StubEmbedder(),
        connector_factory=forbidden,
    )
    assert result["sources"][0]["state"] == "disabled"
    assert store.calls == [("quarantine", "drive", "pilot", "scope_disabled")]


@pytest.mark.parametrize("complete", [True, False])
def test_atomic_snapshot_ingested_even_when_incomplete(settings, tmp_path, monkeypatch, complete):
    monkeypatch.setenv("BRAIN_TEST_SOURCE_TOKEN", "synthetic-token")
    store = FakeStore()
    connector = FakeConnector(complete)
    state = tmp_path / "private"
    result = run_sync(
        settings,
        config(),
        state,
        store=store,
        embedder=StubEmbedder(),
        connector_factory=lambda *_: connector,
    )
    assert connector.closed
    assert store.calls[0][0] == "quarantine"
    assert store.calls[1] == ("ingest", complete)
    assert result["success"] == complete
    path = state / "drive-pilot.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["complete"] == complete
    assert "synthetic-token" not in path.read_text()


def test_missing_credential_quarantines_and_redacts(settings, tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_TEST_SOURCE_TOKEN", raising=False)
    store = FakeStore()
    result = run_sync(
        settings, config(), tmp_path / "private", store=store, embedder=StubEmbedder()
    )
    assert result["sources"][0]["state"] == "failed"
    assert store.calls[0][0] == "quarantine"
    assert "ingest" not in [c[0] for c in store.calls]


def test_provider_exception_does_not_leak_secret(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_TEST_SOURCE_TOKEN", "private-token")
    store = FakeStore()

    def failing(*args):
        raise RuntimeError("provider body includes private-token")

    result = run_sync(
        settings,
        config(),
        tmp_path / "private",
        store=store,
        embedder=StubEmbedder(),
        connector_factory=failing,
    )
    assert "private-token" not in json.dumps(result)
    assert "private-token" not in (tmp_path / "private" / "drive-pilot.json").read_text()
    assert result["success"] is False


def test_secure_file_credentials(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("synthetic-token\n")
    secret.chmod(0o600)
    scope = SourceConfig(
        source="drive",
        collection="pilot",
        enabled=True,
        root_folder_ids=["approved"],
        token_file=str(secret),
    )
    assert read_token(scope) == "synthetic-token"
    secret.chmod(0o644)
    with pytest.raises(ValueError):
        read_token(scope)
    secret.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises(OSError):
        read_token(scope.model_copy(update={"token_file": str(link)}))


def test_config_requires_explicit_roots_and_no_duplicate_scope():
    with pytest.raises(ValidationError):
        SourceConfig(source="drive", collection="pilot", enabled=True, token_env="TOKEN")
    with pytest.raises(ValidationError):
        SyncConfig(sources=[SourceConfig(source="drive", collection="pilot")] * 2)


LINEAR_ORG = "00000000-0000-4000-8000-000000000001"
LINEAR_TEAM = "00000000-0000-4000-8000-000000000002"


def linear_config(**overrides):
    return SourceConfig.model_validate(
        {
            "source": "linear",
            "collection": "team",
            "enabled": True,
            "organization_id": LINEAR_ORG,
            "team_ids": [LINEAR_TEAM],
            "token_env": "BRAIN_TEST_SOURCE_TOKEN",
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"organization_id": None},
        {"team_ids": []},
        {"organization_id": "example-team"},
        {"team_ids": ["ATH"]},
        {"team_ids": [LINEAR_TEAM, LINEAR_TEAM]},
        {"team_ids": [LINEAR_TEAM.replace("-", "")]},
        {"root_folder_ids": ["folder"]},
        {"root_page_ids": ["page"]},
        {"root_database_ids": ["database"]},
        {"token_file": "/config/duplicate.token"},
        {"auth_type": "automatic"},
    ],
)
def test_linear_requires_exact_scope_and_unambiguous_credentials(overrides):
    with pytest.raises(ValidationError):
        linear_config(**overrides)


@pytest.mark.parametrize("source", ["notion", "drive"])
@pytest.mark.parametrize(
    "extra",
    [{"organization_id": LINEAR_ORG}, {"team_ids": [LINEAR_TEAM]}, {"auth_type": "api_key"}],
)
def test_linear_scope_cannot_be_misapplied_to_another_provider(source, extra):
    with pytest.raises(ValidationError):
        SourceConfig(source=source, collection="team", **extra)


def test_disabled_linear_without_scope_never_opens_provider(settings, tmp_path):
    def forbidden(*args):
        pytest.fail("Disabled Linear config accessed provider")

    store = FakeStore()
    result = run_sync(
        settings,
        SyncConfig(sources=[SourceConfig(source="linear", collection="team")]),
        tmp_path / "private",
        store=store,
        embedder=StubEmbedder(),
        connector_factory=forbidden,
    )
    assert result["success"]
    assert store.calls == [("quarantine", "linear", "team", "scope_disabled")]


@pytest.mark.parametrize("auth_type", ["oauth", "api_key"])
def test_linear_factory_preserves_scope_and_explicit_auth(monkeypatch, auth_type):
    import company_brain.connectors as connectors

    observed = {}

    def capture(token, team_ids, collection, **options):
        observed.update(token=token, team_ids=team_ids, collection=collection, **options)
        return "connector"

    monkeypatch.setattr(connectors, "LinearConnector", capture)
    scope = linear_config(auth_type=auth_type, max_requests=17, max_documents=9)
    assert make_connector(scope, "synthetic-token") == "connector"
    assert observed["organization_id"] == LINEAR_ORG
    assert observed["team_ids"] == [LINEAR_TEAM]
    assert observed["collection"] == "team"
    assert observed["auth_type"] == auth_type
    assert observed["limits"].max_requests == 17
    assert observed["limits"].max_documents == 9


@pytest.mark.parametrize("complete", [True, False])
def test_linear_snapshot_reaches_ingest_without_losing_task_metadata(
    settings, tmp_path, monkeypatch, complete
):
    monkeypatch.setenv("BRAIN_TEST_SOURCE_TOKEN", "synthetic-token")
    from company_brain.connectors.common import Snapshot as ConnectorSnapshot

    payload = {
        "source": "linear",
        "collection": "team",
        "complete": complete,
        "quarantine_all": not complete,
        "documents": [
            {
                "source": "linear",
                "external_id": "00000000-0000-4000-8000-000000000003",
                "collection": "team",
                "title": "ATH-1 Synthetic task",
                "text": "Description\nFull comment tail",
                "url": "https://linear.app/example/issue/ATH-1/synthetic",
                "modified_at": "2026-10-07T10:00:00Z",
                "metadata": {"priority": 2, "team_id": LINEAR_TEAM, "status": "Started"},
            }
        ],
    }

    class LinearStore(FakeStore):
        def ingest(self, snapshot, embedder):
            assert snapshot.documents[0].metadata == payload["documents"][0]["metadata"]
            assert snapshot.documents[0].text.endswith("Full comment tail")
            assert snapshot.quarantine_all is not complete
            return super().ingest(snapshot, embedder)

    connector = FakeConnector()
    connector.snapshot = lambda: ConnectorSnapshot(**payload)
    result = run_sync(
        settings,
        SyncConfig(sources=[linear_config()]),
        tmp_path / "private",
        store=LinearStore(),
        embedder=StubEmbedder(),
        connector_factory=lambda *_: connector,
    )
    assert result["success"] is complete
    assert result["sources"][0]["indexed"] == 1
    assert connector.closed
    snapshot = tmp_path / "private" / "linear-team.json"
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert json.loads(snapshot.read_text())["documents"][0]["source"] == "linear"
