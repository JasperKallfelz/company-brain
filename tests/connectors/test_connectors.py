from __future__ import annotations

import json

import httpx
import pytest

from company_brain.connectors import DriveConnector, Limits, NotionConnector
from company_brain.connectors.common import ConnectorError, ProviderHTTP
from company_brain.connectors.drive import FOLDER, GOOGLE_DOC, SHORTCUT


def uid(number):
    return f"00000000-0000-4000-8000-{number:012d}"


def text(value):
    return {"plain_text": value}


def listing(values, cursor=None):
    return {"results": values, "has_more": bool(cursor), "next_cursor": cursor}


def page(number, title="Fixture", parent=None):
    value = {"object": "page", "id": uid(number), "url": "https://www.notion.so/fixture",
             "last_edited_time": "2026-10-06T10:00:00.000Z",
             "properties": {"Name": {"type": "title", "id": "title", "title": [text(title)]}}}
    if parent:
        value["parent"] = {"type": "page_id", "page_id": parent}
    return value


def block(number, kind="paragraph", value="", children=False):
    return {"id": uid(number), "type": kind, "has_children": children,
            kind: {"rich_text": [text(value)]}}


def mocked(handler):
    def checked(request):
        assert request.url.host in {"api.notion.com", "www.googleapis.com", "docs.googleapis.com"}
        result = handler(request)
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)
    return httpx.Client(transport=httpx.MockTransport(checked), follow_redirects=True)


def notion(handler, **kwargs):
    return NotionConnector("synthetic-secret", [uid(1)], "pilot", client=mocked(handler),
                           sleep=lambda _: None, **kwargs)


def notion_base(request):
    if request.url.path == f"/v1/pages/{uid(1)}":
        return page(1)
    if request.url.path == f"/v1/pages/{uid(1)}/properties/title":
        return listing([{"title": text("Fixture")}])
    raise AssertionError(request.url.path)


def test_notion_pagination_nested_blocks_child_pages_and_property_pagination():
    calls = []
    def handle(request):
        calls.append(str(request.url))
        path = request.url.path
        cursor = request.url.params.get("start_cursor")
        if path == f"/v1/blocks/{uid(1)}/children":
            if not cursor:
                return listing([block(2, "toggle", "Start", True)], "block-cursor")
            return listing([{"id": uid(4), "type": "child_page", "child_page": {"title": "Child"}}])
        if path == f"/v1/blocks/{uid(2)}/children":
            return listing([block(3, value="Nested complete text")])
        if path == f"/v1/pages/{uid(4)}":
            return page(4, "Child", uid(1))
        if path == f"/v1/pages/{uid(4)}/properties/title":
            return listing([{"title": text("Child")}])
        if path == f"/v1/blocks/{uid(4)}/children":
            return listing([block(5, value="Child content")])
        if path == f"/v1/pages/{uid(1)}/properties/title":
            return listing([{"title": text("Long ")}], "property-cursor") if not cursor else listing([{"title": text("title")}])
        return notion_base(request)
    result = notion(handle).snapshot()
    assert result.complete, result.errors
    by_id = {document["external_id"]: document for document in result.documents}
    assert by_id[uid(1)]["text"] == "Long title\nStart\nNested complete text\nChild"
    assert "Child content" in by_id[uid(4)]["text"]
    assert any("start_cursor=block-cursor" in call for call in calls)
    assert any("start_cursor=property-cursor" in call for call in calls)


def test_notion_current_database_queries_every_data_source():
    queried = []
    def handle(request):
        path = request.url.path
        assert request.headers["Notion-Version"] == "2026-03-11"
        if path == f"/v1/databases/{uid(10)}":
            return {"id": uid(10), "data_sources": [{"id": uid(11)}, {"id": uid(12)}]}
        if path.endswith("/query"):
            queried.append(path)
            assert request.method == "POST"
            assert json.loads(request.content)["page_size"] == 100
            return listing([])
        raise AssertionError(path)
    connector = NotionConnector("synthetic", [], "pilot", root_database_ids=[uid(10)],
                                 client=mocked(handle), sleep=lambda _: None)
    assert connector.snapshot().complete
    assert len(queried) == 2


def test_notion_capped_data_source_is_incomplete_even_without_next_cursor():
    def handle(request):
        if "/databases/" in request.url.path:
            return {"data_sources": [{"id": uid(11)}]}
        return {**listing([]), "request_status": {"type": "incomplete", "incomplete_reason": "query_result_limit_reached"}}
    connector = NotionConnector("synthetic", [], "pilot", root_database_ids=[uid(10)],
                                 client=mocked(handle), sleep=lambda _: None)
    result = connector.snapshot()
    assert not result.complete and result.quarantine_all
    assert result.errors[0]["code"] == "provider_query_incomplete"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_authorization_or_ambiguous_not_found_quarantines_without_deletion(status):
    connector = notion(lambda _: httpx.Response(status, text="synthetic-secret private source text"))
    result = connector.snapshot()
    assert result.authorization_failed and result.quarantine_all and not result.complete
    assert result.deleted_external_ids == []
    assert "private source text" not in json.dumps(result.to_dict())
    assert "synthetic-secret" not in json.dumps(result.to_dict())


def test_notion_block_failure_cannot_publish_truncated_page():
    def handle(request):
        if "/blocks/" in request.url.path:
            if request.url.params.get("start_cursor"):
                return httpx.Response(503)
            return listing([block(2, value="Only partial text")], "next")
        return notion_base(request)
    result = notion(handle).snapshot()
    assert not result.complete and result.quarantine_all
    assert result.documents == []
    assert result.deleted_external_ids == []


def test_notion_repeated_cursor_is_bounded_failure():
    def handle(request):
        if "/blocks/" in request.url.path:
            return listing([], "again")
        return notion_base(request)
    result = notion(handle).snapshot()
    assert not result.complete
    assert result.errors[0]["code"] == "invalid_pagination"


def test_notion_does_not_follow_external_links_synced_refs_or_attachment_urls():
    def handle(request):
        if "/blocks/" in request.url.path:
            assert request.url.path == f"/v1/blocks/{uid(1)}/children"
            return listing([
                {"id": uid(2), "type": "synced_block", "has_children": True,
                 "synced_block": {"synced_from": {"block_id": uid(88)}}},
                {"id": uid(3), "type": "link_to_page", "link_to_page": {"page_id": uid(99)}},
                {"id": uid(4), "type": "file", "file": {"external": {"url": "http://127.0.0.1/secret"}}},
            ])
        return notion_base(request)
    result = notion(handle).snapshot()
    assert result.complete
    assert result.metadata["counts"]["excluded_external_references"] == 2
    assert result.metadata["counts"]["excluded_media_blocks"] == 1


def test_notion_unknown_block_is_explicit_incomplete():
    def handle(request):
        if "/blocks/" in request.url.path:
            return listing([block(2, "new_content_kind", "hidden text")])
        return notion_base(request)
    result = notion(handle).snapshot()
    assert not result.complete and result.errors[0]["code"] == "unsupported_notion_block"


def file(file_id, mime="text/plain", parent="root", **extra):
    return {"id": file_id, "name": file_id, "mimeType": mime, "parents": [parent],
            "modifiedTime": "2026-10-06T10:00:00.000Z", "version": "1", "trashed": False,
            "capabilities": {"canDownload": True}, **extra}


def drive(handler, **kwargs):
    return DriveConnector("synthetic-secret", ["root"], "pilot", client=mocked(handler),
                           sleep=lambda _: None, **kwargs)


def test_drive_pagination_folder_scope_and_unsupported_shortcut():
    downloads = []
    def handle(request):
        path, params = request.url.path, request.url.params
        if path == "/drive/v3/files":
            query = params["q"]
            assert "trashed = false" in query
            if "'root'" in query:
                assert params["corpora"] == "drive" and params["driveId"] == "shared-drive"
                if not params.get("pageToken"):
                    return {"files": [{"id": "sub"}, {"id": "skip"}], "nextPageToken": "p2"}
                return {"files": [{"id": "a"}]}
            assert "'sub'" in query
            return {"files": [{"id": "b"}]}
        identity = path.rsplit("/", 1)[-1]
        if params.get("alt"):
            downloads.append(identity)
            return httpx.Response(200, text=f"Complete {identity}")
        return {"root": file("root", FOLDER, driveId="shared-drive"),
                "sub": file("sub", FOLDER), "a": file("a"), "b": file("b", parent="sub"),
                "skip": file("skip", SHORTCUT, shortcutDetails={"targetId": "private"})}[identity]
    result = drive(handle).snapshot()
    assert result.complete, result.errors
    assert {doc["external_id"] for doc in result.documents} == {"a", "b"}
    assert set(downloads) == {"a", "b"}
    assert result.metadata["counts"]["excluded_shortcuts"] == 1


def paragraph(value):
    return {"paragraph": {"elements": [{"textRun": {"content": value}}]}}


def test_google_docs_all_nested_tabs_tables_headers_and_footnotes():
    def handle(request):
        path = request.url.path
        if path == "/drive/v3/files":
            return {"files": [{"id": "doc"}]}
        if path == "/drive/v3/files/root":
            return file("root", FOLDER)
        if path == "/drive/v3/files/doc":
            return file("doc", GOOGLE_DOC)
        assert request.url.host == "docs.googleapis.com"
        assert request.url.params["includeTabsContent"] == "true"
        assert request.url.params["suggestionsViewMode"] == "PREVIEW_WITHOUT_SUGGESTIONS"
        return {"documentId": "doc", "tabs": [{
            "tabProperties": {"tabId": "t1", "title": "Main"},
            "documentTab": {"body": {"content": [paragraph("Alpha\n"),
                {"table": {"tableRows": [{"tableCells": [{"content": [paragraph("Cell\n")]}]}]}}]},
                "headers": {"h1": {"content": [paragraph("Header\n")]}}},
            "childTabs": [{"tabProperties": {"tabId": "t2", "title": "Nested"},
                "documentTab": {"body": {"content": [paragraph("Beta\n")]},
                    "footnotes": {"f1": {"content": [paragraph("Footnote\n")]}}}}],
        }]}
    result = drive(handle).snapshot()
    assert result.complete, result.errors
    document = result.documents[0]
    for expected in ("Alpha", "Cell", "Header", "Beta", "Footnote"):
        assert expected in document["text"]
    assert document["metadata"]["tab_ids"] == ["t1", "t2"]


def test_drive_incomplete_search_never_indicates_complete_snapshot():
    result = drive(lambda req: file("root", FOLDER) if req.url.path.endswith("root")
                   else {"files": [], "incompleteSearch": True}).snapshot()
    assert not result.complete and result.quarantine_all
    assert result.deleted_external_ids == []


def test_drive_stale_listing_move_out_of_scope_is_not_downloaded():
    def handle(request):
        if request.url.path.endswith("/root"):
            return file("root", FOLDER)
        if request.url.path.endswith("/files"):
            return {"files": [{"id": "moved"}]}
        assert request.url.params.get("alt") is None
        return file("moved", parent="private-folder")
    result = drive(handle).snapshot()
    assert not result.complete and result.documents == []
    assert result.errors[0]["code"] == "source_moved_out_of_scope"


def test_drive_revoked_download_prevents_export():
    def handle(request):
        if request.url.path.endswith("/root"):
            return file("root", FOLDER)
        if request.url.path.endswith("/files"):
            return {"files": [{"id": "denied"}]}
        assert request.url.params.get("alt") is None
        return file("denied", capabilities={"canDownload": False})
    result = drive(handle).snapshot()
    assert result.authorization_failed and result.quarantine_all
    assert result.documents == []


def test_redirect_not_followed_even_with_client_follow_redirects_enabled():
    calls = []
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8080/private"})
    result = drive(handle).snapshot()
    assert len(calls) == 1
    assert not result.complete


def test_retries_are_bounded_and_body_or_credentials_are_not_reported():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(429, text="synthetic-secret source text", headers={"Retry-After": "9999"})
    result = drive(handle).snapshot()
    assert len(calls) == 3
    assert result.errors[0]["status"] == 429
    assert "synthetic-secret" not in json.dumps(result.to_dict())


def test_response_size_and_request_limits_fail_closed():
    result = drive(lambda _: httpx.Response(200, text="a" * 50),
                   limits=Limits(max_response_bytes=10)).snapshot()
    assert result.errors[0]["code"] == "response_size_limit"
    result = drive(lambda _: file("root", FOLDER), limits=Limits(max_requests=1)).snapshot()
    assert result.errors[0]["code"] == "request_limit"


@pytest.mark.parametrize("identity", ["http://evil.test/root", "../private", "root' or trashed=true", "root?token=secret"])
def test_scope_identifiers_cannot_inject_urls_paths_or_drive_queries(identity):
    with pytest.raises(ConnectorError):
        DriveConnector("synthetic", [identity], "pilot")


def test_transport_rejects_arbitrary_origins_before_request():
    transport = ProviderHTTP("synthetic", client=mocked(lambda _: pytest.fail("network forbidden")))
    with pytest.raises(ConnectorError, match="disallowed_destination"):
        transport.request("GET", "http://localhost", "/private")


def test_notion_observed_edit_during_scan_does_not_publish_mixed_text():
    read = 0
    def handle(request):
        nonlocal read
        if request.url.path == f"/v1/pages/{uid(1)}":
            read += 1
            return {**page(1), "last_edited_time": "first" if read == 1 else "second"}
        if "/blocks/" in request.url.path:
            return listing([block(2, value="A version of the text")])
        return notion_base(request)
    result = notion(handle).snapshot()
    assert not result.complete and result.documents == []
    assert result.errors[0]["code"] == "source_changed_during_sync"


def test_archived_notion_root_has_explicit_tombstone():
    result = notion(lambda _: {**page(1), "archived": True}).snapshot()
    assert result.complete and not result.authorization_failed
    assert result.deleted_external_ids == [uid(1)] and result.documents == []


def test_nested_notion_page_moved_out_of_approved_tree_fails_scope():
    def handle(request):
        if request.url.path == f"/v1/pages/{uid(2)}":
            return page(2, parent=uid(99))
        if "/blocks/" in request.url.path:
            return listing([{"id": uid(2), "type": "child_page", "child_page": {"title": "Moved"}}])
        return notion_base(request)
    result = notion(handle).snapshot()
    assert not result.complete and not result.documents
    assert result.errors[0]["code"] == "source_moved_out_of_scope"


def test_successful_retry_preserves_all_content():
    calls = 0
    def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if request.url.path.endswith("/files"):
            return {"files": []}
        return file("root", FOLDER)
    result = drive(handle).snapshot()
    assert result.complete and calls == 4


def test_drive_mid_download_version_change_fails_and_retains_no_mixed_document():
    reads = 0
    def handle(request):
        nonlocal reads
        if request.url.path.endswith("/root"):
            return file("root", FOLDER)
        if request.url.path.endswith("/files"):
            return {"files": [{"id": "doc"}]}
        if request.url.params.get("alt"):
            return httpx.Response(200, text="Old text")
        reads += 1
        return file("doc", version=str(reads))
    result = drive(handle).snapshot()
    assert not result.complete and result.documents == []
    assert result.errors[0]["code"] == "source_changed_during_sync"


def test_google_docs_legacy_first_tab_response_cannot_be_reported_complete():
    def handle(request):
        if request.url.path.endswith("/root"):
            return file("root", FOLDER)
        if request.url.path.endswith("/files"):
            return {"files": [{"id": "doc"}]}
        if request.url.host == "docs.googleapis.com":
            return {"documentId": "doc", "body": {"content": [paragraph("Only first tab")]}}
        return file("doc", GOOGLE_DOC)
    result = drive(handle).snapshot()
    assert not result.complete and result.documents == []
    assert result.errors[0]["code"] == "missing_google_doc_tabs"


def test_drive_partial_root_success_followed_by_failure_preserves_incomplete_envelope():
    def handle(request):
        if request.url.path.endswith("/root"):
            return file("root", FOLDER)
        if request.url.path.endswith("/files"):
            return {"files": [{"id": "good"}, {"id": "bad"}]}
        if request.url.path.endswith("/bad"):
            return httpx.Response(403)
        if request.url.params.get("alt"):
            return httpx.Response(200, text="Synthetic good source")
        return file("good")
    result = drive(handle).snapshot()
    assert not result.complete and result.quarantine_all and result.authorization_failed
    assert len(result.documents) == 1  # Not a publishable complete replacement.
    assert result.deleted_external_ids == []


def test_document_limit_is_not_silent_truncation():
    def handle(request):
        if request.url.path.endswith("/root"):
            return file("root", FOLDER)
        if request.url.path.endswith("/files"):
            return {"files": [{"id": "a"}, {"id": "b"}]}
        if request.url.params.get("alt"):
            return httpx.Response(200, text="Text")
        return file(request.url.path.rsplit("/", 1)[-1])
    result = drive(handle, limits=Limits(max_documents=1)).snapshot()
    assert len(result.documents) == 1 and not result.complete and result.quarantine_all
    assert result.errors[0]["code"] == "document_limit"


def test_cumulative_response_limit_bounds_page_assembly():
    transport = ProviderHTTP("synthetic", client=mocked(lambda _: httpx.Response(200, text="x" * 8)),
                             limits=Limits(max_total_response_bytes=10))
    assert transport.request("GET", "https://api.notion.com", "/v1/test") == b"xxxxxxxx"
    with pytest.raises(ConnectorError, match="total_response_size_limit"):
        transport.request("GET", "https://api.notion.com", "/v1/test")
