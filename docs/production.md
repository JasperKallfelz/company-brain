# Generic production setup

Use a dedicated Linux server or VM with Docker Compose v2, Python 3.12/3.13 and
uv. Reserve roughly 10 GB RAM for the bounded services plus sufficient disk for
images, model cache and indexed sources. No production credentials or company
accounts are supplied. Review and update pinned images before deployment.

This recipe provides explicit client credentials. URL-only dynamic client
registration is not enabled by the template. Verify what the actual client
supports and use its exact callback; do not add wildcard redirect URIs.

## 1. Generate private configuration

Select two HTTPS names you control, such as `brain.example.com` and
`auth.example.com`. Obtain the actual callback URLs from each client setup.
The following values illustrate the documented standard ChatGPT/Claude
callbacks; confirm them in your client before using them:

```sh
uv sync --locked
uv run python scripts/prepare_production.py \
  --brain-origin https://brain.example.com \
  --identity-origin https://auth.example.com \
  --chatgpt-callback https://chatgpt.com/connector_platform_oauth_redirect \
  --claude-callback https://claude.ai/api/mcp/auth_callback
```

Replace the example hostnames. Generated files go to `runtime/production/`,
mode 0700, with private secret files mode 0600. The non-secret Nginx configuration
is mode 0644 so its container can read it. The generator refuses to replace an
existing directory. Keep a protected backup of this configuration and never
commit or upload it to a public repository.

The realm has confidential clients `company-brain-chatgpt` and
`company-brain-claude`, authorization code + PKCE S256, five-minute access tokens,
refresh rotation and explicit `brain:read` / optional `brain:propose` scopes.
There is no public sign-up, password grant or service-account grant. User
credentials are separate from each application's client ID and secret.

## 2. Initialize and start the private stack

Run these commands from the repository root:

```sh
docker compose --env-file runtime/production/compose.env \
  -f deploy/compose.production.yaml build brain identity

docker compose --env-file runtime/production/compose.env \
  -f deploy/compose.production.yaml up -d --wait db identity-db

docker compose --env-file runtime/production/compose.env \
  -f deploy/compose.production.yaml --profile admin run --rm admin

docker compose --env-file runtime/production/compose.env \
  -f deploy/compose.production.yaml --profile app up -d brain identity gateway
```

The administrator job creates the schema, separate reader/ingest database roles
and a realm-import volume readable only by Keycloak's UID. The server has only
the reader credential; the optional sync worker has only the ingest credential.
Admin credentials belong to explicit operator jobs. The job does not grant
access to any user or collection. Importing a realm on startup does not migrate
an existing realm: apply later changes through Keycloak administration.

The only published production listener is `127.0.0.1:8010` on the Docker host.
It is a plain HTTP origin for a trusted HTTPS reverse proxy or outbound tunnel.
No database, Keycloak administration or host management port is published.

## 3. Add HTTPS routing

Configure your existing HTTPS reverse proxy or an outbound tunnel to send both
chosen hostnames to `http://127.0.0.1:8010`, preserving the original `Host`.
For a named Cloudflare tunnel, the relevant ingress configuration is:

```yaml
ingress:
  - hostname: brain.example.com
    service: http://127.0.0.1:8010
  - hostname: auth.example.com
    service: http://127.0.0.1:8010
  - service: http_status:404
```

This assumes the tunnel process runs on the Docker host. Supply its credential
through your own secret store; the repository neither creates nor bills a
tunnel. A reverse proxy on another host needs a deliberate private-network
design instead of exposing the origin publicly. Do not put an interactive
Cloudflare Access HTML challenge in front of `/mcp`; OAuth-capable clients need
the protocol's bearer challenge and discovery responses.

Nginx exposes only MCP, metadata, the selected realm's OAuth/login paths and
login assets. `/admin`, the master realm and management paths remain blocked.
Request logging is disabled at this gateway to avoid recording OAuth codes and
tokens. Apply the same policy at any outer proxy. Audit provider retention and
request limits for your deployment.

## 4. Create users and collection grants

Manage Keycloak privately using its in-container administration CLI. The command
below prompts for the generated bootstrap administrator password; obtain it
through your private configuration/secret manager:

```sh
docker compose --env-file runtime/production/compose.env \
  -f deploy/compose.production.yaml exec identity \
  /opt/keycloak/bin/kcadm.sh config credentials \
  --server http://127.0.0.1:8081 --realm master \
  --user brain-local-admin --config /tmp/operator.config
```

For example, replace the fictional username/email below and create a colleague:

```sh
docker compose --env-file runtime/production/compose.env \
  -f deploy/compose.production.yaml exec identity \
  /opt/keycloak/bin/kcadm.sh create users -r company-brain \
  --config /tmp/operator.config -s username=colleague \
  -s email=colleague@example.com -s enabled=true \
  -s 'requiredActions=["UPDATE_PASSWORD"]'

docker compose --env-file runtime/production/compose.env \
  -f deploy/compose.production.yaml exec identity bash -c \
  'read -rsp "Temporary user password: " INITIAL_PASSWORD; printf "\n";
   /opt/keycloak/bin/kcadm.sh set-password -r company-brain \
     --config /tmp/operator.config --username colleague \
     --new-password "$INITIAL_PASSWORD" --temporary;
   unset INITIAL_PASSWORD'

docker compose --env-file runtime/production/compose.env \
  -f deploy/compose.production.yaml exec identity \
  /opt/keycloak/bin/kcadm.sh get users -r company-brain \
  --config /tmp/operator.config -q username=colleague -q exact=true --fields id,username
```

The password prompt prevents a literal password entering shell history. The
administration CLI temporarily receives it as a process argument inside the
private container; use your established secrets/provisioning system if that
does not fit your operating policy. Deliver user credentials via your approved
private channel. Do not put real passwords in shared logs. Remove
`/tmp/operator.config` when the admin session ends. Provision a permanent
operator identity and retire bootstrap administration as part of your own
Keycloak operations procedure.

Retrieve the new user's exact Keycloak `id` (the OAuth token `sub`). Grant it an
explicit collection using the administrator job environment:

```sh
docker compose --env-file runtime/production/compose.env \
  -f deploy/compose.production.yaml --profile admin run --rm admin \
  python -m company_brain collection-set team

docker compose --env-file runtime/production/compose.env \
  -f deploy/compose.production.yaml --profile admin run --rm admin \
  python -m company_brain user-set REPLACE_WITH_KEYCLOAK_SUBJECT --collection team
```

To revoke Brain access, use `user-revoke REPLACE_WITH_KEYCLOAK_SUBJECT` through
the same administrator job. Deactivating a user only in Keycloak cannot recall
already-issued tokens instantly; Brain's active-user check is the immediate
resource-side control. No matching email suffix grants data access.

## 5. Configure sources and synchronization

Use [sources.example.json](../deploy/sources.example.json) as a disabled starting
point. Enable only approved roots/teams and one agreed collection per source.
Set the collection to `team` if you used the commands above. For Linear supply
the exact organization and team UUIDs and explicitly select `api_key` or `oauth`.
For Notion share only the approved roots with its integration. Google token
refresh remains your credential provider's responsibility.

On the Linux Docker host, place `sources.json` and specific provider token files
under `/etc/company-brain/sources/`. The sync process runs as UID/GID `10001`:
directory mode 0750, files mode 0600, owned by that UID (or read securely through
an equivalent deployment secret mount). Do not store master vault credentials.
Ensure every path is a regular file rather than a symlink.

Run the first sync manually:

```sh
docker compose --env-file runtime/production/compose.env \
  -f deploy/compose.production.yaml --profile sync run --rm sync
```

Verify imported counts, complete text, source links and denial of private sibling
sources. Then schedule that same one-shot command, for example every 15 minutes
with a systemd timer and an overlap lock. Source freshness expires after 30
minutes by default; missed/failed imports therefore stop serving stale data.
No background sync schedule is installed by this template. Bound provider
requests and monitor quotas; no Composio or paid embedding API is required.

## 6. Connect and accept actual clients

Enter `https://brain.example.com/mcp` in the client's custom MCP connector setup.
Where supported, select its own/custom OAuth client option and enter that
application's generated client ID and secret from the private realm configuration.
Each person then signs in with their own user account and changes their temporary
password. A client secret is not the user's password. Do not send one person's
bearer token to another person or copy source-provider tokens into this field.

Test with a synthetic shared collection first:

- Authentication, PKCE code exchange and token refresh in each actual app.
- `search`, complete paginated `fetch`, source citations and a denied private
  collection/document, including direct previously known document IDs.
- Brain-user revocation, expired/wrong-audience token denial and source
  authorization failure/recovery.
- If proposals are enabled, confirm they remain pending review and do not write
  directly to Notion or Linear.

Do not infer compatibility with every account from HTTP probes alone. Features
and client setup can change; consult the official client instructions.

## Operations before real company data

Back up both PostgreSQL databases, the generated configuration/realm secrets and
your source scope configuration to encrypted storage outside this machine.
Test restoration into an isolated stack and then its authentication/retrieval.
Named Docker volumes are persistent storage, not backups. Keep host SSH/admin
private, test LAN/host reachability from the VM, monitor sync freshness and disk
usage, and update dependencies/images with the tests before promotion.

Relevant primary documentation:
[Keycloak containers](https://www.keycloak.org/server/containers),
[Keycloak administration CLI](https://www.keycloak.org/docs/latest/server_admin/index.html#admin-cli),
[MCP authorization](https://modelcontextprotocol.io/specification/latest/basic/authorization),
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/).
