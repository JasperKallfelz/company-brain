"""Policy/credential tests without any database mutation."""

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "ops" / "database-roles.py"
spec = importlib.util.spec_from_file_location("database_roles", SCRIPT)
roles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roles)


def policy():
    columns = {
        "proposals": ["id", "subject", "state", "created_at", "proposed_text"],
        "documents": ["id", "body"],
    }
    return [
        statement.as_string()
        for statement in roles.policy_statements("brain_test", columns)
    ]


def grants_for(role):
    return [
        statement
        for statement in policy()
        if statement.startswith("GRANT")
        and (
            f'TO "{role}"' in statement
            or f', "{role}"' in statement
            or f"TO {role}" in statement
        )
    ]


def test_reader_can_only_read_company_data_and_insert_review_proposals():
    statements = grants_for("brain_reader")
    writes = [
        statement
        for statement in statements
        if any(word in statement for word in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"))
    ]
    assert len(writes) == 1
    assert 'ON TABLE "public"."proposals"' in writes[0]
    assert '"proposed_text"' in writes[0]
    assert '"state"' not in writes[0] and '"created_at"' not in writes[0]
    # No collection/user grant, schema creation, sequences, or approval rights.
    assert not any(
        "WITH GRANT OPTION" in statement
        or "CREATE " in statement
        or "SEQUENCE" in statement
        for statement in statements
    )
    assert set(roles.TABLE_GRANTS["brain_reader"]) == set(roles.TABLES) - {"proposals"}
    assert (
        "proposed_text"
        not in roles.COLUMN_GRANTS["brain_reader"]["proposals"]["SELECT"]
    )


def test_ingest_has_no_user_grant_or_proposal_authority():
    statements = grants_for("brain_ingest")
    assert not any(
        f'"{table}"' in statement
        for statement in statements
        for table in ("brain_users", "grants", "proposals")
    )
    assert roles.TABLE_GRANTS["brain_ingest"]["collections"] == ("SELECT",)
    assert roles.TABLE_GRANTS["brain_ingest"]["sources"] == (
        "SELECT",
        "INSERT",
        "UPDATE",
    )
    assert "DELETE" in roles.TABLE_GRANTS["brain_ingest"]["documents"]
    assert roles.COLUMN_GRANTS["brain_ingest"]["chunks"]["SELECT"] == ("document_id",)
    assert roles.COLUMN_GRANTS["brain_ingest"]["chunks"]["INSERT"] == (
        "document_id",
        "ordinal",
        "body",
        "embedding",
    )


def test_removes_public_and_previous_column_grants_and_future_defaults():
    statements = policy()
    assert any(
        'REVOKE ALL PRIVILEGES ON DATABASE "brain_test" FROM PUBLIC' in statement
        for statement in statements
    )
    assert any(
        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES" in statement
        for statement in statements
    )
    assert any(
        "REVOKE INSERT (" in statement and '"state"' in statement
        for statement in statements
    )
    assert any(
        "ALTER DEFAULT PRIVILEGES" in statement and "FUNCTIONS" in statement
        for statement in statements
    )
    assert any(
        statement.startswith("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS")
        for statement in statements
    )
    assert any(
        "REVOKE ALL PRIVILEGES ON ALL ROUTINES" in statement for statement in statements
    )


def test_only_static_vector_functions_are_granted():
    functions = [
        statement for statement in policy() if statement.startswith("GRANT EXECUTE")
    ]
    assert len(functions) == len(roles.VECTOR_FUNCTIONS)
    assert any(
        "cosine_distance(public.vector,public.vector)" in statement
        for statement in functions
    )
    assert not any(
        "ALL FUNCTIONS" in statement or "SECURITY DEFINER" in statement
        for statement in functions
    )


def test_database_identifier_is_quoted_not_interpolated():
    statements = roles.policy_statements('brain"; DROP DATABASE production; --', {})
    assert '"brain""; DROP DATABASE production; --"' in statements[0].as_string()


def test_secrets_only_from_explicit_sources_and_protected_regular_files(
    tmp_path, monkeypatch
):
    source = tmp_path / "credential"
    source.write_text("synthetic\n")
    source.chmod(0o600)
    assert roles.read_secret(filename=str(source)) == "synthetic"
    source.chmod(0o644)
    with pytest.raises(roles.RoleSetupError, match="secret_file_permissions"):
        roles.read_secret(filename=str(source))
    source.chmod(0o600)
    link = tmp_path / "symlink"
    link.symlink_to(source)
    with pytest.raises(OSError):
        roles.read_secret(filename=str(link))
    monkeypatch.setenv("SYNTHETIC_ROLE_SECRET", "from-environment")
    assert roles.read_secret(environment="SYNTHETIC_ROLE_SECRET") == "from-environment"
    with pytest.raises(roles.RoleSetupError):
        roles.read_secret(environment="SYNTHETIC_ROLE_SECRET", filename=str(source))


@pytest.mark.parametrize(
    "password", ["short", "a" * 31, "a" * 32 + "\n", "ü" * 40, "a" * 32 + " "]
)
def test_reject_weak_or_ambiguous_generated_passwords(password):
    with pytest.raises(roles.RoleSetupError):
        roles.validate_password(password)


def test_redacted_cli_failure_does_not_expose_admin_url_or_password(
    monkeypatch, capsys
):
    monkeypatch.setenv(
        "TEST_ROLE_ADMIN_URL", "postgresql://admin:secret-at-host@example.test/database"
    )
    monkeypatch.setenv("TEST_ROLE_READER_PASSWORD", "R" * 40)
    monkeypatch.setenv("TEST_ROLE_INGEST_PASSWORD", "I" * 40)

    def fail(*args, **kwargs):
        raise roles.psycopg.OperationalError(
            "postgresql://admin:secret-at-host@example.test/database"
        )

    monkeypatch.setattr(roles.psycopg, "connect", fail)
    code = roles.main(
        [
            "--expected-database",
            "brain_test",
            "--admin-url-env",
            "TEST_ROLE_ADMIN_URL",
            "--reader-password-env",
            "TEST_ROLE_READER_PASSWORD",
            "--ingest-password-env",
            "TEST_ROLE_INGEST_PASSWORD",
        ]
    )
    assert code == 1
    output = capsys.readouterr()
    assert "secret-at-host" not in output.out + output.err
    assert "R" * 40 not in output.out + output.err
    assert "database_role_setup_failed" in output.out
