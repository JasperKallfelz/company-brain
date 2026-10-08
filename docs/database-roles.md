# Runtime database roles

`ops/database-roles.py` prepares the dedicated Company Brain PostgreSQL database
after `init-db` or migrations have run with the separate administrator identity.
It does not run implicitly during MCP startup. Real database-policy acceptance
uses the opt-in disposable-cluster tests.

## Privileges derived from Store SQL

| Identity | Granted access | Explicitly absent |
| --- | --- | --- |
| `brain_reader` — MCP service | SELECT on users, collections, grants, sources, documents and chunks; limited proposal lookup columns; INSERT only proposal input columns | User/grant/source/document changes; proposal approval or modification; schema creation; sequences; ownership; administrative role flags |
| `brain_ingest` — sync worker | SELECT collections; SELECT/INSERT/UPDATE sources; SELECT/INSERT/UPDATE/DELETE documents; limited chunk SELECT/INSERT plus chunk DELETE | Users/grants/proposals direct access; collection changes; schema creation; sequences; ownership; administrative role flags |
| Separate administrator | Schema initialization/migrations, configure collections/users, review or approve proposals, role setup | Credentials must never be mounted into the normal MCP or sync containers |

The current schema has UUID/composite primary keys and no serial/identity
sequences. The roles receive no sequence privileges. Cascading deletion from a
document removes its chunks and proposals through the schema's foreign keys;
this does not grant the worker direct proposal-editing rights. Proposal creation
cannot set its approval state or creation timestamp: these columns use defaults.

The policy revokes pre-existing table **and column** privileges for both app roles
and PUBLIC, including privileges on other objects in the dedicated `public`
schema. It then grants the exact policy. PostgreSQL column grants survive a
table-level revoke, so both layers are reset. PUBLIC loses database CONNECT/TEMP
and public-schema creation. Known vector IO/cast functions and cosine distance
are allowed; blanket execution of public routines is removed.
[PostgreSQL privilege rules](https://www.postgresql.org/docs/current/sql-grant.html),
[pgvector functions](https://github.com/pgvector/pgvector/blob/v0.8.0/sql/vector.sql).

Both app roles are LOGIN but NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION,
NOBYPASSRLS and NOINHERIT. Memberships involving managed app roles are removed so
SET ROLE cannot inherit an administrative identity. A connection limit and query
timeout are set. Existing unmanaged roles with the same names or app-owned
objects cause a redacted failure; the script never takes ownership or drops
objects. PostgreSQL roles are cluster-wide, so use a dedicated cluster.
[PostgreSQL role attributes](https://www.postgresql.org/docs/current/sql-createrole.html).

This separates runtime capabilities; it is not row-level security. The MCP service
still enforces end-user collection authorization. A compromised reader credential
could read its permitted tables, and an importer credential can alter indexed
content. Neither role should be exposed to users or external networks.

## Application

Use the same dedicated administrator/migration identity for `init-db` and role
setup. Prepare three secrets outside the repository: the admin connection string,
an independent generated reader password, and an independent generated ingest
password. Passwords must be printable non-space ASCII with at least 32 characters;
`secrets.token_urlsafe(36)` is suitable. File secrets must be regular files owned
by the executing account or root, with mode `0400` or `0600`; symlinks are rejected.

```sh
python ops/database-roles.py \
  --expected-database company_brain \
  --admin-url-file /run/secrets/admin_database_url \
  --reader-password-file /run/secrets/reader_password \
  --ingest-password-file /run/secrets/ingest_password
```

Named environment variables are supported with the equivalent `--admin-url-env`,
`--reader-password-env` and `--ingest-password-env` arguments. These arguments take
variable names, never secret values. Exactly one file or environment source is
required for each secret. No host credential discovery occurs. The database name
must exactly match `--expected-database` before any DDL is applied.

The script runs transactionally, resets managed privileges and can be rerun. It
uses libpq to generate SCRAM verifiers; plaintext runtime passwords never appear
in SQL. Normal statement/duration/error-parameter logging is suppressed for the
rotation transaction. Deployment operators must also review any external audit
extension; an audit plugin can independently log utility statements. Script
stdout contains only fixed success/error codes and the two role names.

After successful setup, supply `brain_reader` credentials to the MCP service and
`brain_ingest` credentials to the sync worker. Keep the administrator connection
for explicit operator jobs. Restrict database networking to these services.
Password rotation does not terminate already-authenticated sessions: restart the
affected service at a safe boundary, then verify its actual database identity.

Migrations use the administrator identity and are followed by this script plus
affected permission tests. New tables/sequences/functions are not automatically
granted to runtime roles. Extend the explicit policy only when application SQL
requires access.

## Verification

Unit tests check the privilege boundaries, old column/default privilege removal,
identifier quoting, protected secret inputs and error redaction:

```sh
python -m pytest tests/roles/test_database_roles.py -q
```

Actual SQL acceptance requires a separately disposable PostgreSQL cluster with
pgvector and a database whose name ends in `_test`. **A test database in the same
cluster as the runtime is insufficient:** test setup creates/rotates the global
roles `brain_reader` and `brain_ingest`. Set `BRAIN_ROLES_TEST_DATABASE_URL` through
the test secret mechanism and `BRAIN_ROLES_TEST_ALLOW_GLOBAL_ROLES=1` only after
verifying that cluster boundary, then run:

```sh
python -m pytest tests/roles/test_database_roles_integration.py -q
```

These tests apply the policy twice, connect as each real role, exercise vector
queries and ingestion/proposal SQL, and assert forbidden writes, schema changes,
role changes and user administration fail. The cluster is discarded by its owner
after acceptance; tests intentionally do not issue broad cleanup against a DSN.
