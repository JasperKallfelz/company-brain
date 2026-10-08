import pytest
from company_brain.store import AccessDenied, NotFound, doc_id
from .conftest import StubEmbedder
from .test_snapshot import document, snapshot

pytestmark = pytest.mark.integration


def test_complete_update_delete_and_idempotence(database):
    embedder = StubEmbedder()
    assert database.ingest(snapshot([document(), document("two")]), embedder)["indexed"] == 2
    database.ingest(snapshot([document(), document("two")]), embedder)
    with database.connection() as c:
        assert c.execute("SELECT count(*) n FROM chunks").fetchone()["n"] == 2
    database.ingest(snapshot([document(text="A new decision")]), embedder)
    assert database.fetch("alice", doc_id("fixture", "one"))["text"] == "A new decision"
    with pytest.raises(NotFound):
        database.fetch("alice", doc_id("fixture", "two"))


def test_search_and_direct_fetch_obey_current_grants(database):
    database.ingest(snapshot(), StubEmbedder())
    result = database.search("alice", "Friday", StubEmbedder().encode(["Friday"])[0])
    assert result["results"][0]["id"] == doc_id("fixture", "one")
    assert database.search("bob", "Friday", StubEmbedder().encode(["Friday"])[0])["results"] == []
    with pytest.raises(NotFound):
        database.fetch("bob", doc_id("fixture", "one"))
    with pytest.raises(NotFound):
        database.fetch("alice", "https://private.example/")
    with pytest.raises(AccessDenied):
        database.search(
            "alice", "Friday", StubEmbedder().encode(["Friday"])[0], collection="private"
        )
    database.revoke_user("alice")
    with pytest.raises(AccessDenied):
        database.fetch("alice", doc_id("fixture", "one"))


def test_incomplete_snapshot_retains_but_quarantines_then_recovers(database):
    database.ingest(snapshot(), StubEmbedder())
    result = database.ingest(
        snapshot([], complete=False, errors=["source failure"]), StubEmbedder()
    )
    assert result["state"] == "quarantined"
    assert database.status()["documents"] == 1
    with pytest.raises(NotFound):
        database.fetch("alice", doc_id("fixture", "one"))
    database.ingest(snapshot(), StubEmbedder())
    assert database.fetch("alice", doc_id("fixture", "one"))["text"]
    database.source_state("fixture", "team", "revoked")
    with pytest.raises(AccessDenied):
        database.ingest(snapshot(), StubEmbedder())


def test_source_freshness_expires_fail_closed(database):
    database.ingest(snapshot(), StubEmbedder())
    with database.connection() as c:
        c.execute("UPDATE sources SET last_success_at=now()-interval '2 days'")
    with pytest.raises(NotFound):
        database.fetch("alice", doc_id("fixture", "one"))
    assert database.search("alice", "Friday", StubEmbedder().encode(["Friday"])[0])["results"] == []


def test_full_pagination_and_version_change(database):
    original = "Long transcript äö🚀 " * 1000
    database.ingest(snapshot([document(text=original)]), StubEmbedder())
    cursor = None
    text = ""
    while True:
        page = database.fetch("alice", doc_id("fixture", "one"), cursor)
        text += page["text"]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert text == original
    cursor = database.fetch("alice", doc_id("fixture", "one"))["next_cursor"]
    database.ingest(snapshot([document(text=original + "changed")]), StubEmbedder())
    with pytest.raises(ValueError, match="outdated"):
        database.fetch("alice", doc_id("fixture", "one"), cursor)


def test_quarantined_document_recovers_embeddings(database):
    database.ingest(snapshot(quarantined_external_ids=["one"]), StubEmbedder())
    with pytest.raises(NotFound):
        database.fetch("alice", doc_id("fixture", "one"))
    database.ingest(snapshot(), StubEmbedder())
    assert database.search("alice", "Friday", StubEmbedder().encode(["Friday"])[0])["results"]


def test_proposal_idempotency_scope_and_concurrent_change(database):
    database.ingest(snapshot(), StubEmbedder())
    row = database.fetch("alice", doc_id("fixture", "one"))
    # A large proposal must not hit PostgreSQL B-tree entry-size limits.
    proposal = database.propose("alice", row["id"], row["content_hash"], "x" * 20000, "Evidence")
    assert proposal["published"] is False
    assert (
        database.propose("alice", row["id"], row["content_hash"], "x" * 20000, "Evidence")
        == proposal
    )
    with pytest.raises(NotFound):
        database.propose("bob", row["id"], row["content_hash"], "change", "reason")
    with pytest.raises(ValueError, match="changed"):
        database.propose("alice", row["id"], "stale hash", "change", "reason")


def test_incomplete_retry_cannot_unrevoke_source(database):
    database.ingest(snapshot(), StubEmbedder())
    database.source_state("fixture", "team", "revoked")
    database.ingest(snapshot([], complete=False, authorization_failed=True), StubEmbedder())
    assert database.status()["sources"][0]["state"] == "revoked"
    with pytest.raises(AccessDenied):
        database.ingest(snapshot(), StubEmbedder())


def test_embedding_failure_quarantines_cached_source(database):
    database.ingest(snapshot(), StubEmbedder())

    class BrokenEmbedder(StubEmbedder):
        def encode(self, texts):
            raise RuntimeError("Synthetic embedding failure")

    with pytest.raises(RuntimeError):
        database.ingest(snapshot([document(text="Changed source")]), BrokenEmbedder())
    with pytest.raises(NotFound):
        database.fetch("alice", doc_id("fixture", "one"))
