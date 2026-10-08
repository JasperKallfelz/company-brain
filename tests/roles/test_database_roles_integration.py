"""Opt-in role verification against a separately disposable PostgreSQL cluster.

Roles are cluster-global! Never point this at the runtime cluster, even if the
chosen database ends in _test. The explicit extra flag documents that boundary.
"""

import importlib.util
import os
from pathlib import Path
import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def role_database():
    url = os.environ.get("BRAIN_ROLES_TEST_DATABASE_URL")
    if not url or os.environ.get("BRAIN_ROLES_TEST_ALLOW_GLOBAL_ROLES") != "1":
        pytest.skip("Requires explicitly disposable cluster for global role tests")
    info = conninfo_to_dict(url)
    if not info.get("dbname", "").endswith("_test"):
        pytest.fail("Role acceptance database must end in _test")
    spec = importlib.util.spec_from_file_location(
        "roles_integration", Path(__file__).parents[2] / "ops" / "database-roles.py"
    )
    roles = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(roles)
    reader_password, ingest_password = (
        "Test-reader-" + uuid.uuid4().hex,
        "Test-ingest-" + uuid.uuid4().hex,
    )
    with psycopg.connect(url) as admin:
        admin.execute(
            (Path(__file__).parents[2] / "src/company_brain/schema.sql").read_text()
        )
        # Apply twice to verify the managed policy is repeatable without broadening.
        for _ in range(2):
            roles.apply_roles(
                admin,
                info["dbname"],
                {"brain_reader": reader_password, "brain_ingest": ingest_password},
            )
    yield {
        "admin": url,
        "reader": make_conninfo(url, user="brain_reader", password=reader_password),
        "ingest": make_conninfo(url, user="brain_ingest", password=ingest_password),
    }
    # The operator discards the whole cluster; avoid DROP OWNED on a mistaken DSN.


def assert_denied(url, statement):
    with psycopg.connect(url) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(statement)


def test_runtime_roles_cannot_escalate_or_change_schema(role_database):
    for name in ("reader", "ingest"):
        with psycopg.connect(role_database[name]) as connection:
            flags = connection.execute(
                "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolinherit "
                "FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            assert flags == (False,) * 6
            assert connection.execute(
                "SELECT has_database_privilege(current_database(),'CREATE'), "
                "has_database_privilege(current_database(),'TEMPORARY'), has_schema_privilege('public','CREATE')"
            ).fetchone() == (False, False, False)
        assert_denied(role_database[name], "CREATE TABLE public.forbidden(id int)")
        assert_denied(role_database[name], "CREATE TEMP TABLE forbidden(id int)")
        assert_denied(role_database[name], "CREATE ROLE forbidden_role")
        assert_denied(role_database[name], "SET ROLE pg_read_server_files")


def test_actual_reader_queries_and_ingest_dml_permissions(role_database):
    collection = "role-fixture-" + uuid.uuid4().hex
    subject = "role-subject-" + uuid.uuid4().hex
    identifier, proposal = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(role_database["admin"]) as admin:
        admin.execute("INSERT INTO collections(id) VALUES(%s)", (collection,))
        admin.execute("INSERT INTO brain_users(subject) VALUES(%s)", (subject,))
        admin.execute(
            "INSERT INTO grants(subject,collection) VALUES(%s,%s)",
            (subject, collection),
        )
    vector = "[" + ",".join(["1"] + ["0"] * 383) + "]"
    with psycopg.connect(role_database["ingest"]) as ingest:
        ingest.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (collection,))
        ingest.execute("SELECT id FROM collections WHERE id=%s", (collection,))
        ingest.execute(
            "INSERT INTO sources(source,collection) VALUES('fixture',%s)", (collection,)
        )
        ingest.execute(
            "UPDATE sources SET state='ready',last_success_at=now() WHERE collection=%s",
            (collection,),
        )
        ingest.execute(
            "INSERT INTO documents(id,source,external_id,collection,title,body,url,modified_at,content_hash,embedding_model) "
            "VALUES(%s,'fixture',%s,%s,'Synthetic','Text','https://example.test',now(),'hash','stub')",
            (identifier, str(identifier), collection),
        )
        ingest.execute(
            "INSERT INTO chunks(document_id,ordinal,body,embedding) VALUES(%s,0,'Text',%s::public.vector)",
            (identifier, vector),
        )
        ingest.execute("UPDATE documents SET body='Updated' WHERE id=%s", (identifier,))
    with psycopg.connect(role_database["reader"]) as reader:
        assert reader.execute(
            "SELECT 1 FROM brain_users WHERE subject=%s AND active", (subject,)
        ).fetchone()
        assert reader.execute(
            "SELECT d.* FROM documents d JOIN sources s ON s.collection=d.collection AND s.source=d.source WHERE d.id=%s",
            (identifier,),
        ).fetchone()
        assert reader.execute(
            "SELECT embedding <=> %s::public.vector, ts_rank_cd(fts,websearch_to_tsquery('simple','Text')) FROM chunks WHERE document_id=%s",
            (vector, identifier),
        ).fetchone()
        reader.execute(
            "SELECT id FROM proposals WHERE subject=%s AND state='pending'", (subject,)
        )
        reader.execute(
            "SELECT count(*) FROM proposals WHERE subject=%s AND state='pending'",
            (subject,),
        )
        reader.execute(
            "INSERT INTO proposals(id,subject,document_id,base_hash,proposed_text,proposal_hash,rationale) VALUES(%s,%s,%s,'hash','Suggestion','proposed-hash','Evidence')",
            (proposal, subject, identifier),
        )
    for statement in (
        "UPDATE brain_users SET active=false",
        "DELETE FROM documents",
        "UPDATE proposals SET state='approved'",
        "TRUNCATE documents",
        "SELECT proposed_text FROM proposals",
    ):
        assert_denied(role_database["reader"], statement)
    for statement in (
        "UPDATE collections SET active=false",
        "UPDATE grants SET subject='x'",
        "SELECT * FROM proposals",
        "UPDATE brain_users SET active=false",
    ):
        assert_denied(role_database["ingest"], statement)
    with psycopg.connect(role_database["ingest"]) as ingest:
        ingest.execute("DELETE FROM chunks WHERE document_id=%s", (identifier,))
        # FK cleanup cascades proposals without granting direct proposal DELETE.
        ingest.execute("DELETE FROM documents WHERE id=%s", (identifier,))
