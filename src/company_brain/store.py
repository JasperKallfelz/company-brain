from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
from importlib.resources import files
import json
import uuid
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import Snapshot


class AccessDenied(ValueError):
    pass


class NotFound(ValueError):
    pass


def doc_id(source, external_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"company-brain:{source}:{external_id}"))


def content_hash(document):
    payload = document.model_dump(mode="json")
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def vector_literal(value):
    if len(value) != 384:
        raise ValueError("Embedding must have 384 dimensions")
    import math

    if not all(math.isfinite(x) for x in value):
        raise ValueError("Embedding contains nonfinite values")
    return "[" + ",".join(str(float(x)) for x in value) + "]"


class Store:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def connection(self):
        with psycopg.connect(
            self.settings.database_url,
            row_factory=dict_row,
            connect_timeout=5,
            options="-c statement_timeout=15000",
        ) as conn:
            yield conn

    def init_db(self):
        with self.connection() as conn:
            conn.execute(files("company_brain").joinpath("schema.sql").read_text())

    def configure_collection(self, collection, active=True):
        with self.connection() as c:
            c.execute(
                "INSERT INTO collections(id,active) VALUES(%s,%s) ON CONFLICT(id) DO UPDATE SET active=excluded.active",
                (collection, active),
            )

    def configure_user(self, subject, collections, active=True):
        with self.connection() as c:
            c.execute(
                "INSERT INTO brain_users(subject,active) VALUES(%s,%s) ON CONFLICT(subject) DO UPDATE SET active=excluded.active",
                (subject, active),
            )
            c.execute("DELETE FROM grants WHERE subject=%s", (subject,))
            for collection in collections:
                c.execute(
                    "INSERT INTO grants(subject,collection) VALUES(%s,%s)", (subject, collection)
                )

    def revoke_user(self, subject):
        with self.connection() as c:
            c.execute("UPDATE brain_users SET active=false WHERE subject=%s", (subject,))

    def active_user(self, subject):
        with self.connection() as c:
            return bool(
                c.execute(
                    "SELECT 1 FROM brain_users WHERE subject=%s AND active", (subject,)
                ).fetchone()
            )

    def source_state(self, source, collection, state, error="operator_action"):
        if state not in {"quarantined", "revoked"}:
            raise ValueError("Source can only be quarantined or revoked explicitly")
        with self.connection() as c:
            c.execute(
                "INSERT INTO sources(source,collection,state,error_code) VALUES(%s,%s,%s,%s) ON CONFLICT(source,collection) DO UPDATE SET state=CASE WHEN sources.state='revoked' THEN 'revoked' ELSE excluded.state END,error_code=excluded.error_code",
                (source, collection, state, error),
            )

    def quarantine_existing(self, source, collection, error="sync_failed"):
        with self.connection() as c:
            c.execute(
                "UPDATE sources SET state=CASE WHEN state='revoked' THEN 'revoked' ELSE 'quarantined' END, error_code=%s WHERE source=%s AND collection=%s",
                (error, source, collection),
            )

    def ingest(self, snapshot: Snapshot, embedder):
        # One writer per scope, including model work. Other scopes continue independently.
        with self.connection() as lock_conn:
            lock_conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("ingest:" + snapshot.source + ":" + snapshot.collection,),
            )
            return self._ingest(snapshot, embedder)

    def _ingest(self, snapshot: Snapshot, embedder):
        # Failed/incomplete reads provide no evidence that cached ACLs remain valid.
        if (
            not snapshot.complete
            or snapshot.errors
            or snapshot.quarantine_all
            or snapshot.authorization_failed
        ):
            self.source_state(
                snapshot.source, snapshot.collection, "quarantined", "incomplete_sync"
            )
            return {"state": "quarantined", "indexed": 0, "deleted": 0}
        with self.connection() as c:
            allowed = c.execute(
                "SELECT 1 FROM collections WHERE id=%s AND active", (snapshot.collection,)
            ).fetchone()
            if not allowed:
                raise AccessDenied("Collection is not explicitly enabled")
            state = c.execute(
                "SELECT state FROM sources WHERE source=%s AND collection=%s",
                (snapshot.source, snapshot.collection),
            ).fetchone()
            if state and state["state"] == "revoked":
                raise AccessDenied("Source is revoked; create a new reviewed source scope")
            existing = {
                r["external_id"]: r
                for r in c.execute(
                    "SELECT external_id,content_hash,embedding_model,quarantined FROM documents WHERE source=%s AND collection=%s",
                    (snapshot.source, snapshot.collection),
                ).fetchall()
            }
        self.source_state(snapshot.source, snapshot.collection, "quarantined", "sync_in_progress")
        prepared = []
        quarantined = set(snapshot.quarantined_external_ids)
        for doc in snapshot.documents:
            digest = content_hash(doc)
            old = existing.get(doc.external_id)
            changed = (
                not old
                or old["quarantined"]
                or old["content_hash"] != digest
                or old["embedding_model"] != embedder.model_id
            )
            parts = (
                list(embedder.chunks(doc.text))
                if changed and doc.external_id not in quarantined
                else []
            )
            vectors = embedder.encode(parts) if parts else []
            if len(parts) != len(vectors):
                raise ValueError("Embedding count does not match chunk count")
            prepared.append((doc, digest, changed, parts, vectors))
        with self.connection() as c:
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (snapshot.source + ":" + snapshot.collection,),
            )
            c.execute(
                "INSERT INTO sources(source,collection) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (snapshot.source, snapshot.collection),
            )
            state = c.execute(
                "SELECT state FROM sources WHERE source=%s AND collection=%s FOR UPDATE",
                (snapshot.source, snapshot.collection),
            ).fetchone()
            if state["state"] == "revoked":
                raise AccessDenied("Source revoked during import")
            for doc, digest, changed, parts, vectors in prepared:
                identifier = doc_id(doc.source, doc.external_id)
                collision = c.execute(
                    "SELECT collection FROM documents WHERE id=%s", (identifier,)
                ).fetchone()
                if collision and collision["collection"] != doc.collection:
                    raise ValueError(
                        "A source ID belongs to another collection; revoke old scope before moving"
                    )
                c.execute(
                    """INSERT INTO documents(id,source,external_id,collection,title,body,url,modified_at,metadata,content_hash,embedding_model,quarantined)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,body=excluded.body,url=excluded.url,
                    modified_at=excluded.modified_at,metadata=excluded.metadata,content_hash=excluded.content_hash,
                    embedding_model=excluded.embedding_model,quarantined=excluded.quarantined""",
                    (
                        identifier,
                        doc.source,
                        doc.external_id,
                        doc.collection,
                        doc.title,
                        doc.text,
                        doc.url,
                        doc.modified_at,
                        Jsonb(doc.metadata),
                        digest,
                        embedder.model_id,
                        doc.external_id in quarantined,
                    ),
                )
                if changed:
                    c.execute("DELETE FROM chunks WHERE document_id=%s", (identifier,))
                    for n, (part, vec) in enumerate(zip(parts, vectors, strict=True)):
                        c.execute(
                            "INSERT INTO chunks(document_id,ordinal,body,embedding) VALUES(%s,%s,%s,%s::vector)",
                            (identifier, n, part, vector_literal(vec)),
                        )
            ids = [d.external_id for d in snapshot.documents]
            removed = c.execute(
                "DELETE FROM documents WHERE source=%s AND collection=%s AND NOT (external_id = ANY(%s::text[]))",
                (snapshot.source, snapshot.collection, ids),
            ).rowcount
            if quarantined:
                c.execute(
                    "UPDATE documents SET quarantined=true WHERE source=%s AND collection=%s AND external_id=ANY(%s::text[])",
                    (snapshot.source, snapshot.collection, list(quarantined)),
                )
            c.execute(
                "UPDATE sources SET state='ready', last_success_at=now(),error_code=NULL WHERE source=%s AND collection=%s",
                (snapshot.source, snapshot.collection),
            )
        return {"state": "ready", "indexed": len(prepared), "deleted": removed}

    def _authorized(self, c, subject):
        rows = c.execute(
            """SELECT g.collection FROM grants g JOIN brain_users u ON u.subject=g.subject
            JOIN collections col ON col.id=g.collection WHERE u.subject=%s AND u.active AND col.active""",
            (subject,),
        ).fetchall()
        if not rows:
            raise AccessDenied("No active collection grants")
        return [r["collection"] for r in rows]

    def _freshness(self):
        return timedelta(seconds=self.settings.source_ttl_seconds)

    def _document(self, c, subject, identifier):
        collections = self._authorized(c, subject)
        try:
            identifier = str(uuid.UUID(identifier))
        except ValueError:
            raise NotFound("Document not found or unavailable") from None
        row = c.execute(
            """SELECT d.* FROM documents d JOIN sources s ON s.source=d.source AND s.collection=d.collection
            WHERE d.id=%s AND d.collection=ANY(%s::text[]) AND NOT d.quarantined AND s.state='ready'
            AND s.last_success_at > now() - %s::interval""",
            (identifier, collections, self._freshness()),
        ).fetchone()
        if row is None:
            raise NotFound("Document not found or unavailable")
        return row

    def fetch(self, subject, identifier, cursor=None):
        with self.connection() as c:
            row = self._document(c, subject, identifier)
            offset = 0
            if cursor:
                import base64

                try:
                    obj = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                    if obj["id"] != str(row["id"]) or obj["hash"] != row["content_hash"]:
                        raise ValueError()
                    offset = obj["offset"]
                    if type(offset) is not int or not 0 <= offset < len(row["body"]):
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise ValueError(
                        "Invalid or outdated cursor; fetch from the first page"
                    ) from None
            end = min(offset + self.settings.fetch_page_chars, len(row["body"]))
            next_cursor = None
            if end < len(row["body"]):
                import base64

                next_cursor = (
                    base64.urlsafe_b64encode(
                        json.dumps(
                            {"id": str(row["id"]), "hash": row["content_hash"], "offset": end}
                        ).encode()
                    )
                    .decode()
                    .rstrip("=")
                )
            return {
                "id": str(row["id"]),
                "title": row["title"],
                "text": row["body"][offset:end],
                "url": row["url"],
                "source": row["source"],
                "collection": row["collection"],
                "modified_at": row["modified_at"].isoformat(),
                "content_hash": row["content_hash"],
                "offset": offset,
                "total_characters": len(row["body"]),
                "next_cursor": next_cursor,
            }

    def search(self, subject, query, embedding, limit=5, collection=None):
        if not query.strip() or len(query) > 2000 or not 1 <= limit <= 20:
            raise ValueError("Query must contain 1-2000 characters and limit must be 1-20")
        with self.connection() as c:
            collections = self._authorized(c, subject)
            if collection:
                if collection not in collections:
                    raise AccessDenied("Collection is unavailable")
                collections = [collection]
            # Exact cosine search is intentional at pilot scale: ACL filters run before ranking.
            # Reciprocal-rank fusion retains exact names and multilingual semantic matches.
            rows = c.execute(
                """WITH visible AS (
                SELECT ch.document_id,ch.ordinal,ch.body,ch.embedding,ch.fts,d.title,d.url,d.modified_at,d.source,d.collection
                FROM chunks ch JOIN documents d ON d.id=ch.document_id
                JOIN sources s ON s.source=d.source AND s.collection=d.collection
                WHERE d.collection=ANY(%s::text[]) AND NOT d.quarantined AND s.state='ready'
                AND s.last_success_at > now() - %s::interval
            ), semantic AS (
                SELECT document_id,ordinal,row_number() OVER (ORDER BY embedding <=> %s::vector) rank
                FROM visible ORDER BY embedding <=> %s::vector LIMIT 100
            ), lexical AS (
                SELECT document_id,ordinal,row_number() OVER (ORDER BY ts_rank_cd(fts,websearch_to_tsquery('simple',%s)) DESC) rank
                FROM visible WHERE fts @@ websearch_to_tsquery('simple',%s)
                ORDER BY ts_rank_cd(fts,websearch_to_tsquery('simple',%s)) DESC LIMIT 100
            ), scores AS (
                SELECT document_id,ordinal,sum(score) score FROM (
                    SELECT document_id,ordinal,1.0/(60+rank) score FROM semantic UNION ALL
                    SELECT document_id,ordinal,1.0/(60+rank) score FROM lexical
                ) ranked GROUP BY document_id,ordinal
            ), best AS (
                SELECT DISTINCT ON (v.document_id) v.document_id,v.title,v.url,v.modified_at,v.source,v.collection,v.body,s.score
                FROM scores s JOIN visible v USING(document_id,ordinal)
                ORDER BY v.document_id,s.score DESC
            ) SELECT * FROM best ORDER BY score DESC LIMIT %s""",
                (
                    collections,
                    self._freshness(),
                    vector_literal(embedding),
                    vector_literal(embedding),
                    query,
                    query,
                    query,
                    limit,
                ),
            ).fetchall()
            return {
                "results": [
                    {
                        "id": str(r["document_id"]),
                        "title": r["title"],
                        "url": r["url"],
                        "text": r["body"][:1600],
                        "modified_at": r["modified_at"].isoformat(),
                        "source": r["source"],
                        "collection": r["collection"],
                    }
                    for r in rows
                ]
            }

    def propose(self, subject, identifier, base_hash, text, rationale):
        if not 1 <= len(text) <= 20000 or not 1 <= len(rationale) <= 2000:
            raise ValueError("Proposal text must be 1-20000 and rationale 1-2000 characters")
        with self.connection() as c:
            row = self._document(c, subject, identifier)
            if row["content_hash"] != base_hash:
                raise ValueError("Document changed; fetch current content before proposing")
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("proposals:" + subject,))
            existing = c.execute(
                "SELECT id FROM proposals WHERE subject=%s AND document_id=%s AND base_hash=%s AND proposal_hash=%s",
                (subject, identifier, base_hash, sha256(text.encode()).hexdigest()),
            ).fetchone()
            if existing:
                return {"id": str(existing["id"]), "state": "pending", "published": False}
            count = c.execute(
                "SELECT count(*) n FROM proposals WHERE subject=%s AND state='pending'", (subject,)
            ).fetchone()["n"]
            if count >= 100:
                raise ValueError("Pending proposal limit reached; request review")
            new_id = str(uuid.uuid4())
            c.execute(
                "INSERT INTO proposals(id,subject,document_id,base_hash,proposed_text,proposal_hash,rationale) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    new_id,
                    subject,
                    identifier,
                    base_hash,
                    text,
                    sha256(text.encode()).hexdigest(),
                    rationale,
                ),
            )
            return {"id": new_id, "state": "pending", "published": False}

    def status(self):
        with self.connection() as c:
            sources = c.execute(
                "SELECT source,collection,state,last_success_at,error_code FROM sources ORDER BY source,collection"
            ).fetchall()
            count = c.execute("SELECT count(*) n FROM documents").fetchone()["n"]
            return {"documents": count, "sources": sources}
