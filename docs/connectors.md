# Source connectors

Implemented read-only adapters: `NotionConnector`, `DriveConnector`, and
`LinearConnector`. They export normalized snapshots for the core ingestion
command. Connector tests use synthetic provider responses; live provider
acceptance must be recorded separately. Notion/Drive documentation checked on
2026-10-06 and Linear documentation/schema on 2026-10-07.

## Scope before activation

Use one approved collection per source configuration. Everyone granted that
collection must be allowed to read everything in its approved roots. This pilot
does **not** reproduce individual Notion or Google ACLs. The operator supplies
root IDs and a dedicated integration identity with access only to those areas.
The connector never searches the workspace or lists a user's entire Drive.

Notion requires a read-content integration shared with the approved page/database
roots. Drive needs a dedicated read identity with the selected folders explicitly
shared; enable Drive API and Docs API. `drive.readonly` works with both read APIs.
Do not grant domain-wide delegation for this pilot. Access token refresh is the
deployment credential provider's responsibility; these adapters consume an
explicit token and never discover credentials on the host.

The Notion config is:

```json
{
  "source": "notion",
  "collection": "pilot",
  "root_page_ids": ["00000000-0000-4000-8000-000000000001"],
  "root_database_ids": []
}
```

The Drive config is:

```json
{
  "source": "drive",
  "collection": "pilot",
  "root_folder_ids": ["REPLACE_WITH_APPROVED_FOLDER_ID"]
}
```

The Linear config requires the exact approved workspace UUID and team UUIDs:

```json
{
  "source": "linear",
  "collection": "pilot",
  "organization_id": "00000000-0000-4000-8000-000000000009",
  "team_ids": ["00000000-0000-4000-8000-000000000001"],
  "auth_type": "oauth"
}
```

Use a dedicated read-only Linear OAuth identity authorized for those common team
areas. `auth_type: "oauth"` sends a Bearer token; `"api_key"` explicitly selects
Linear's raw API-key header. Token formats are never guessed. The connector first
checks `viewer.organization.id`, then queries each configured team by UUID and
checks its workspace and every issue's team. It never enumerates all teams,
issues, users, or projects in the workspace and never expands to subteams.
Only explicitly allowlisted read queries can reach the Linear endpoint; this
importer cannot create tasks, change priorities, or post comments. Interactive
writes remain a separate authorized Linear MCP action.

Store real configurations and snapshot output in a VM directory readable only by
the sync account, outside the repository. Provide `COMPANY_BRAIN_NOTION_TOKEN` or
`COMPANY_BRAIN_DRIVE_TOKEN` or `COMPANY_BRAIN_LINEAR_TOKEN` through the deployment's
secret mechanism. Never put
tokens in command arguments, committed config, or shell history.

For normal operation prefer the core sync runner. Its configuration wraps up to
eight unique source/collection configurations in `sources`, adds `enabled: false`
by default, and requires exactly one of `token_env` or `token_file` for each enabled
source. Example, deliberately disabled:

```json
{
  "sources": [{
    "enabled": false,
    "source": "notion",
    "collection": "pilot",
    "root_page_ids": [],
    "root_database_ids": [],
    "token_file": "/run/secrets/notion_token",
    "max_runtime_seconds": 240,
    "max_requests": 3000,
    "max_documents": 2000
  }]
}
```

```sh
python -m company_brain sync-once --config /run/company-brain/sources.json --state-dir /var/lib/company-brain/sync
```

The core runner handles export and ingestion together, including failed snapshots.
Protected token files must be owned by the current user or root, mode `0600`, and
not symlinks. The standalone export commands below are for controlled diagnostics.

```sh
python -m company_brain.connectors --config /run/company-brain/source.json --output /var/lib/company-brain/snapshots/source.json
python -m company_brain ingest-snapshot /var/lib/company-brain/snapshots/source.json
```

Export writes a new snapshot atomically with file mode `0600`. Its stdout contains
counts only. Exit 0 means complete, exit 2 means a written incomplete/quarantine
snapshot, exit 1 means config/output failed and no new snapshot is available.
**The scheduler must ingest the newly written snapshot on exit 2 as well.** Do
not join those commands with `&&`, and do not ingest a previous output file after
exit 1. An export/ingest job failure must quarantine the scope or leave it subject
to the core's fail-closed freshness expiry. Schedule only after credentials and
source approval exist; no live schedule is activated by this module.

## Coverage and boundaries

| Source | Imported | Deliberately excluded |
| --- | --- | --- |
| Notion | Page titles, paginated text properties, simple properties, nested text blocks, table rows, descendant pages, database data-source rows | Comments, media bytes/OCR, linked-page targets, synced blocks originating elsewhere, formula/rollup values that may depend on unavailable related databases |
| Drive blobs | UTF-8 plain text, Markdown, CSV, TSV, JSON | PDF, images, audio/video, Office binary formats, shortcuts |
| Google Docs | All tabs and nested tabs; body/table/header/footer/footnote text; accepted content without suggested edits | Comments, unaccepted suggestions, image contents and equation objects |
| Other Google Workspace files | Counted as unsupported | Sheets, Slides, Forms, Vids |
| Linear | Issue identifier/title/full Markdown description, source URL, status, priority, assignee, project, update timestamps; all paginated root comments and recursively paginated replies with author and source links; archived issues/comments and all workflow states | Trashed issues, attachment/media bytes, external link targets, issue history, project documents |

Unsupported media and references are counted in `metadata.counts`; Drive also
reports MIME counts. Unknown Notion blocks and unknown Google Docs text elements
fail the snapshot instead of silently claiming text completeness. The configured
limits fail closed when exceeded; they never truncate text and report success.
PDF ingestion can be added with a separate bounded extractor and coverage tests.
Until then, store authoritative transcripts as text or Google Docs.

The Notion connector pins `Notion-Version: 2026-03-11`, recursively paginates block
children and text/reference properties, and enumerates each database's data
sources using the current query endpoint. It detects the query's incomplete
status even when pagination appears finished. References are never followed
outside the selected tree. See [block pagination](https://developers.notion.com/reference/get-block-children),
[property pagination](https://developers.notion.com/reference/retrieve-a-page-property),
[database retrieval](https://developers.notion.com/reference/retrieve-database),
and [data-source querying](https://developers.notion.com/reference/query-a-data-source).

Drive enumerates each selected folder with parent-scoped queries and shared-drive
parameters, follows every page token, checks `incompleteSearch`, and re-reads
metadata before and after content retrieval. Shortcuts are not followed. Google
Docs uses `includeTabsContent=true` and traverses child tabs; the legacy first-tab
body is rejected. See [Drive listing](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list),
[download authorization](https://developers.google.com/workspace/drive/api/guides/manage-downloads),
[Docs retrieval](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/get),
and [tab structure](https://developers.google.com/workspace/docs/api/how-tos/tabs).

Linear reads `team.issues(includeArchived: true, includeSubTeams: false)` without
a workflow-state filter, so completed and canceled work is included. Each issue
is one document; full Markdown descriptions and comment/reply bodies are kept
without truncation. Root comments use `parent: { null: true }`; replies follow
the explicitly returned comment IDs through `children`, validating parent and
issue identities. Both connections include archived comments and follow every
cursor. Per-comment `updatedAt` values contribute to document `modified_at`.
Issue details are rechecked after reading comments, and parent comments are
checked on each replies page. This detects observed mid-read changes; the API
does not provide an atomic snapshot of all issues and comments. Changes after a
resource was read are reconciled on the next complete scan.

GraphQL errors, including HTTP 200 responses with partial data, fail closed.
Provider error messages never enter logs or snapshots. Missing/inaccessible
teams, workspace/team mismatches, malformed records, duplicate resources,
pagination loops and configured limits also quarantine the entire scope. The
connector never asserts deletions from failed/partial enumeration.
See Linear's [GraphQL and authentication documentation](https://linear.app/developers/graphql),
[pagination documentation](https://linear.app/developers/pagination), and
[official SDK schema](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql).

## Snapshot semantics

`source + external_id` is stable. Every document also contains `collection`,
`title`, full supported `text`, a provider `url`, `modified_at`, and `metadata`.

The envelope contains `complete`, `errors`, `authorization_failed`,
`quarantine_all`, `quarantined_external_ids`, `deleted_external_ids`, and documents.
`complete` means enumeration and supported text retrieval succeeded within the
declared coverage, not that excluded media were understood. Source HTTP errors
are recorded as fixed codes/statuses, never response bodies or token-bearing URLs.

Any failed request, incomplete listing, pagination loop, limit, or observed
concurrent edit makes the snapshot incomplete and requests quarantine of the
whole source/collection. Preserve prior stored data for recovery but deny search
and fetch while quarantined. Never delete missing documents from a partial list.
Only a successful full snapshot may reconcile missing IDs and release quarantine.
Notion 404 is ambiguous between absence and lack of access and therefore triggers
quarantine, not an asserted deletion.

Provider APIs do not offer a transaction over an entire hierarchy. Metadata
rechecks catch observed moves/edits, while a scheduled full rescan reconciles later
changes. A permission change cannot be known instantaneously by an offline index:
the maximum allowed source freshness and sync failure handling must be enforced
by the serving layer. Revocation of a Company Brain user is a separate immediate
server authorization check. Removing access cannot erase text already returned
to a user's existing chat.

## Resource and network limits

Defaults are 10,000 HTTP requests, 2,000 documents, depth 64, 16 MiB per response,
128 MiB cumulative response data, 64 MiB total indexed snapshot text, and three
attempts per request. Requests time
out after 30 seconds. Rate-limit/server-error retries are bounded; Notion requests
are paced. Configure lower limits for a small pilot if appropriate.

Only HTTPS requests to `api.notion.com`, `www.googleapis.com`,
`docs.googleapis.com`, and `api.linear.app` are issued. Redirects, arbitrary source URLs, attachment
URLs, and Drive download links are never followed. API IDs are validated before
building paths or Drive queries. POST is restricted to Notion's read-only query
and fixed allowlisted Linear GraphQL read queries at `/graphql`. Linear scope
accepts at most 100 distinct canonical-format UUIDs, with 50 records per page;
request, response, snapshot and recursion bounds also apply to comments/replies.
VM firewall policy should independently enforce approved egress.

## Live acceptance still required

- Share only synthetic pilot roots first; verify a sibling/private page is absent.
- Use a long Notion page with nested blocks, >100 children, long properties, and a
  database. Check the last known marker is present, not only the first section.
- Use Drive nested folders and a multi-tab Doc with tables and footnotes; verify
  all known markers. Compare unsupported counts against source inventory.
- Use an explicitly approved Linear team with active/completed/canceled and
  archived issues, long descriptions, >50 comments and >50 replies. Check every
  last marker, workspace/team boundaries, moved issues and access revocation.
- Change, move, archive/trash, and revoke access to synthetic sources. Verify new
  snapshots and core search/fetch behavior, including direct old document IDs.
- Interrupt a page of enumeration and expire a token. Verify retained storage is
  quarantined and a subsequent complete run recovers without duplicates.
- Confirm token refresh and a full day's sync cadence before company data import.
