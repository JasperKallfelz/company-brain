#!/usr/bin/env python3
"""Apply the explicit runtime DB policy after admin-only init-db/migrations.

This is for the dedicated Company Brain database/cluster, not a shared database.
Connection details/passwords are accepted only from named environment variables
or protected files. Errors never include SQL, connection strings or credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import stat

import psycopg
from psycopg import sql

ROLES = ("brain_reader", "brain_ingest")
MARKER = "company-brain managed runtime role v1"
TABLES = (
    "brain_users",
    "collections",
    "grants",
    "sources",
    "documents",
    "chunks",
    "proposals",
)
TABLE_GRANTS = {
    "brain_reader": {name: ("SELECT",) for name in TABLES if name != "proposals"},
    "brain_ingest": {
        "collections": ("SELECT",),
        "sources": ("SELECT", "INSERT", "UPDATE"),
        "documents": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    },
}
COLUMN_GRANTS = {
    "brain_reader": {
        "proposals": {
            "SELECT": (
                "id",
                "subject",
                "document_id",
                "base_hash",
                "proposal_hash",
                "state",
            ),
            "INSERT": (
                "id",
                "subject",
                "document_id",
                "base_hash",
                "proposed_text",
                "proposal_hash",
                "rationale",
            ),
        },
    },
    "brain_ingest": {
        "chunks": {
            "SELECT": ("document_id",),
            "INSERT": ("document_id", "ordinal", "body", "embedding"),
        },
    },
}
# Only text/binary IO, vector typmod coercion, and the cosine operator required
# by Store.search are executable from the application's extension schema.
VECTOR_FUNCTIONS = (
    "public.vector_in(cstring,oid,integer)",
    "public.vector_out(public.vector)",
    "public.vector_recv(internal,oid,integer)",
    "public.vector_send(public.vector)",
    "public.vector_typmod_in(cstring[])",
    "public.vector(public.vector,integer,boolean)",
    "public.cosine_distance(public.vector,public.vector)",
)


class RoleSetupError(Exception):
    """Only fixed, redacted error codes may be attached."""


def read_secret(*, environment: str | None = None, filename: str | None = None) -> str:
    if bool(environment) == bool(filename):
        raise RoleSetupError("exactly_one_secret_source_required")
    if environment:
        value = os.environ.get(environment, "")
    else:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(filename, flags)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                or info.st_uid not in (0, os.geteuid())
            ):
                raise RoleSetupError("secret_file_permissions")
            raw = handle.read(16_385)
            if len(raw) > 16_384:
                raise RoleSetupError("secret_too_large")
            value = raw.decode("utf-8").rstrip("\r\n")
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise RoleSetupError("invalid_secret")
    return value


def validate_password(password: str) -> None:
    # Generated printable ASCII avoids implicit SASLprep changes and is suitable
    # for secret-file tooling. Example generator: secrets.token_urlsafe(36).
    if not 32 <= len(password) <= 1024 or any(
        not 33 <= ord(char) <= 126 for char in password
    ):
        raise RoleSetupError("password_must_be_generated_ascii_at_least_32_characters")


def recipients(*names: str) -> sql.Composed:
    return sql.SQL(", ").join(
        sql.SQL("PUBLIC") if name == "PUBLIC" else sql.Identifier(name)
        for name in names
    )


def policy_statements(
    database: str, columns: dict[str, list[str]]
) -> list[sql.Composable]:
    """Return non-secret DDL with safely quoted identifiers, never arbitrary SQL."""
    everyone = recipients("PUBLIC", *ROLES)
    statements = [
        sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
            sql.Identifier(database), everyone
        ),
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(database), recipients(*ROLES)
        ),
        sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(everyone),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(recipients(*ROLES)),
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(
            everyone
        ),
        sql.SQL(
            "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}"
        ).format(everyone),
        sql.SQL(
            "REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA public FROM {}"
        ).format(everyone),
        # Per-schema defaults cannot subtract global grants (notably PostgreSQL's
        # default PUBLIC EXECUTE on newly created functions). Reset both levels.
        sql.SQL("ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM {}").format(
            everyone
        ),
        sql.SQL("ALTER DEFAULT PRIVILEGES REVOKE ALL ON SEQUENCES FROM {}").format(
            everyone
        ),
        sql.SQL("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM {}").format(
            everyone
        ),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {}"
        ).format(everyone),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {}"
        ).format(everyone),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM {}"
        ).format(everyone),
    ]
    # Table-level REVOKE does not erase earlier column grants. Remove both.
    for table, names in columns.items():
        if not names:
            continue
        joined = sql.SQL(", ").join(map(sql.Identifier, names))
        for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
            statements.append(
                sql.SQL("REVOKE {} ({}) ON TABLE {} FROM {}").format(
                    sql.SQL(privilege),
                    joined,
                    sql.Identifier("public", table),
                    everyone,
                )
            )
    for role in ROLES:
        statements.append(
            sql.SQL("ALTER ROLE {} SET search_path = pg_catalog, public").format(
                sql.Identifier(role)
            )
        )
        statements.append(
            sql.SQL("ALTER ROLE {} SET statement_timeout = '15s'").format(
                sql.Identifier(role)
            )
        )
        for table, privileges in TABLE_GRANTS[role].items():
            statements.append(
                sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                    sql.SQL(", ").join(map(sql.SQL, privileges)),
                    sql.Identifier("public", table),
                    sql.Identifier(role),
                )
            )
        for table, privileges in COLUMN_GRANTS[role].items():
            for privilege, names in privileges.items():
                statements.append(
                    sql.SQL("GRANT {} ({}) ON TABLE {} TO {}").format(
                        sql.SQL(privilege),
                        sql.SQL(", ").join(map(sql.Identifier, names)),
                        sql.Identifier("public", table),
                        sql.Identifier(role),
                    )
                )
    statements.append(sql.SQL("GRANT DELETE ON TABLE public.chunks TO brain_ingest"))
    statements.append(
        sql.SQL("GRANT USAGE ON TYPE public.vector TO {}").format(recipients(*ROLES))
    )
    for function in VECTOR_FUNCTIONS:
        # Function signatures are compile-time constants, never external input.
        statements.append(
            sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(
                sql.SQL(function), recipients(*ROLES)
            )
        )
    return statements


def apply_roles(
    connection: psycopg.Connection, expected_database: str, passwords: dict[str, str]
) -> None:
    if set(passwords) != set(ROLES):
        raise RoleSetupError("both_runtime_passwords_required")
    for value in passwords.values():
        validate_password(value)
    if passwords[ROLES[0]] == passwords[ROLES[1]]:
        raise RoleSetupError("runtime_passwords_must_differ")
    with connection.transaction():
        state = connection.execute(
            "SELECT current_database(), current_user, rolsuper FROM pg_catalog.pg_roles WHERE rolname=current_user"
        ).fetchone()
        if not state or state[0] != expected_database:
            raise RoleSetupError("database_identity_mismatch")
        if state[1] in ROLES or not state[2]:
            raise RoleSetupError("separate_admin_connection_required")
        connection.execute("SET LOCAL search_path = pg_catalog")
        connection.execute("SET LOCAL lock_timeout = '10s'")
        connection.execute("SET LOCAL statement_timeout = '30s'")
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('company-brain:runtime-role-policy'))"
        )
        # Keep rotation statements/parameters out of normal server SQL logs.
        connection.execute("SET LOCAL log_statement = 'none'")
        connection.execute("SET LOCAL log_min_duration_statement = -1")
        connection.execute("SET LOCAL log_min_duration_sample = -1")
        connection.execute("SET LOCAL log_min_error_statement = 'panic'")
        connection.execute("SET LOCAL log_parameter_max_length = 0")
        connection.execute("SET LOCAL log_parameter_max_length_on_error = 0")
        rows = connection.execute(
            "SELECT c.relname,a.attname FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid "
            "WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f') "
            "AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum"
        ).fetchall()
        columns: dict[str, list[str]] = {}
        for table, column in rows:
            columns.setdefault(table, []).append(column)
        if not set(TABLES) <= columns.keys():
            raise RoleSetupError("run_admin_init_db_first")
        vector = connection.execute(
            "SELECT 1 FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid=e.extnamespace "
            "WHERE e.extname='vector' AND n.nspname='public'"
        ).fetchone()
        if not vector:
            raise RoleSetupError("vector_extension_must_exist_in_public")
        for role in ROLES:
            existing = connection.execute(
                "SELECT oid,pg_catalog.shobj_description(oid,'pg_authid') FROM pg_catalog.pg_roles WHERE rolname=%s",
                (role,),
            ).fetchone()
            if existing and existing[1] != MARKER:
                raise RoleSetupError("unmanaged_role_name_collision")
            if existing:
                ownership = connection.execute(
                    "SELECT 1 FROM pg_catalog.pg_shdepend WHERE refclassid='pg_authid'::regclass "
                    "AND refobjid=%s AND deptype='o' LIMIT 1",
                    (existing[0],),
                ).fetchone()
                if ownership:
                    raise RoleSetupError("runtime_role_owns_objects")
            else:
                connection.execute(
                    sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role))
                )
                connection.execute(
                    sql.SQL("COMMENT ON ROLE {} IS {}").format(
                        sql.Identifier(role), sql.Literal(MARKER)
                    )
                )
            # libpq constructs a SCRAM verifier locally; plaintext never enters
            # the ALTER ROLE statement or its bound query parameters.
            verifier = connection.pgconn.encrypt_password(
                passwords[role].encode(), role.encode(), b"scram-sha-256"
            ).decode()
            connection.execute(
                sql.SQL(
                    "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                    "NOBYPASSRLS NOINHERIT CONNECTION LIMIT 32 PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(verifier))
            )
            connection.execute(
                sql.SQL("ALTER ROLE {} RESET ALL").format(sql.Identifier(role))
            )
            connection.execute(
                sql.SQL("ALTER ROLE {} IN DATABASE {} RESET ALL").format(
                    sql.Identifier(role), sql.Identifier(expected_database)
                )
            )
        memberships = connection.execute(
            "SELECT parent.rolname,member.rolname FROM pg_catalog.pg_auth_members m "
            "JOIN pg_catalog.pg_roles parent ON parent.oid=m.roleid "
            "JOIN pg_catalog.pg_roles member ON member.oid=m.member "
            "WHERE parent.rolname=ANY(%s::text[]) OR member.rolname=ANY(%s::text[])",
            (list(ROLES), list(ROLES)),
        ).fetchall()
        for parent, member in memberships:
            connection.execute(
                sql.SQL("REVOKE {} FROM {} CASCADE").format(
                    sql.Identifier(parent), sql.Identifier(member)
                )
            )
        for statement in policy_statements(expected_database, columns):
            connection.execute(statement)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-database", required=True)
    for prefix in ("admin-url", "reader-password", "ingest-password"):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(f"--{prefix}-env")
        group.add_argument(f"--{prefix}-file")
    args = parser.parse_args(argv)
    try:
        secrets = {
            name: read_secret(
                environment=getattr(args, name + "_env"),
                filename=getattr(args, name + "_file"),
            )
            for name in ("admin_url", "reader_password", "ingest_password")
        }
        with psycopg.connect(
            secrets["admin_url"],
            connect_timeout=10,
            options="-c statement_timeout=30000",
        ) as connection:
            apply_roles(
                connection,
                args.expected_database,
                {
                    "brain_reader": secrets["reader_password"],
                    "brain_ingest": secrets["ingest_password"],
                },
            )
        print(json.dumps({"runtime_roles_configured": list(ROLES)}))
        return 0
    except RoleSetupError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    except (psycopg.Error, OSError, ValueError, UnicodeError):
        print(json.dumps({"error": "database_role_setup_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
