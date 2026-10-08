from __future__ import annotations

import copy
import json

import httpx
import pytest

from company_brain.connectors import ConnectorError, Limits, LinearConnector
from company_brain.connectors.common import ProviderHTTP
from company_brain.connectors.linear import (
    COMMENTS_QUERY,
    REPLIES_QUERY,
    TEAM_QUERY,
    VERIFY_ISSUE_QUERY,
    VIEWER_QUERY,
)


def uid(number):
    return f"00000000-0000-4000-8000-{number:012d}"


def team(number=1, organization=9):
    return {"id": uid(number), "organization": {"id": uid(organization)}}


def issue(number=10, team_number=1, **changes):
    return {
        "id": uid(number), "identifier": f"ENG-{number}", "title": "Synthetic task",
        "description": "Full description", "url": f"https://linear.app/test/issue/ENG-{number}",
        "updatedAt": "2026-10-07T10:00:00Z", "archivedAt": None, "trashed": False,
        "team": team(team_number), "state": {"id": uid(30), "name": "Done", "type": "completed"},
        "priority": 2, "priorityLabel": "High", "assignee": {"id": uid(31), "name": "Ada"},
        "project": {"id": uid(32), "name": "Roadmap", "url": "https://linear.app/test/project/roadmap"},
        **changes,
    }


def comment(number=100, issue_number=10, parent=None, **changes):
    return {
        "id": uid(number), "body": "Comment body", "updatedAt": "2026-10-07T11:00:00Z",
        "archivedAt": None, "url": f"https://linear.app/test/issue/ENG-{issue_number}#comment-{number}",
        "issue": {"id": uid(issue_number), "team": team()},
        "user": {"id": uid(31), "name": "Ada"},
        "parent": {"id": uid(parent)} if parent else None, **changes,
    }


def connection(nodes, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


class Provider:
    def __init__(self, issues=None, comments=None):
        self.issues = issues if issues is not None else [issue()]
        self.comments = comments or []
        self.calls = []

    def __call__(self, query, variables):
        self.calls.append((query, variables))
        if query == VIEWER_QUERY:
            return {"viewer": {"organization": {"id": uid(9)}}}
        if query == TEAM_QUERY:
            selected = [value for value in self.issues if value["team"]["id"] == variables["id"]]
            return {"team": {"id": variables["id"], "organization": {"id": uid(9)},
                             "issues": connection(selected)}}
        if query in (COMMENTS_QUERY, VERIFY_ISSUE_QUERY):
            value = next(value for value in self.issues if value["id"] == variables["id"])
            value = copy.deepcopy(value)
            if query == COMMENTS_QUERY:
                value["comments"] = connection([value for value in self.comments
                                                if value["issue"]["id"] == variables["id"]
                                                and value["parent"] is None])
            return {"issue": value}
        if query == REPLIES_QUERY:
            value = next(value for value in self.comments if value["id"] == variables["id"])
            return {"comment": {**copy.deepcopy(value), "children": connection([
                value for value in self.comments if value["parent"] == {"id": variables["id"]}])}}
        raise AssertionError("unexpected operation")


def connector(handler, **options):
    auth = options.pop("expected_auth", "Bearer synthetic-secret")

    def transport(request):
        assert str(request.url) == "https://api.linear.app/graphql"
        assert request.method == "POST"
        assert request.headers["Authorization"] == auth
        payload = json.loads(request.content)
        data = handler(payload["query"], payload["variables"])
        return data if isinstance(data, httpx.Response) else httpx.Response(200, json={"data": data})

    return LinearConnector(
        "synthetic-secret", options.pop("team_ids", [uid(1)]), "pilot", organization_id=uid(9),
        client=httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=True),
        sleep=lambda _: None, **options,
    )


def assert_failed(result, code=None):
    assert not result.complete
    assert result.quarantine_all
    assert not result.deleted_external_ids
    assert result.errors
    if code:
        assert result.errors[0]["code"] == code


def test_paginates_issues_comments_and_replies_preserving_long_text_and_archive():
    long_description = "Anfang 🚀\n" + "description\n" * 4000 + "END-DESCRIPTION"
    long_comment = "comment\n" * 4000 + "END-COMMENT"
    base = Provider([issue(description=long_description),
                     issue(11, archivedAt="2026-10-06T10:00:00Z")],
                    [comment(body=long_comment), comment(101), comment(102, parent=100),
                     comment(103, parent=100), comment(104, parent=102)])

    def handle(query, variables):
        result = base(query, variables)
        if query == TEAM_QUERY:
            assert "includeArchived: true" in query and "includeSubTeams: false" in query
            assert "filter:" not in query  # Include every workflow state, including done/canceled.
            result["team"]["issues"] = (connection([base.issues[0]], "issue-page-2")
                                          if not variables["after"] else connection([base.issues[1]]))
        elif query == COMMENTS_QUERY and variables["id"] == uid(10):
            assert "includeArchived: true" in query and "parent: { null: true }" in query
            result["issue"]["comments"] = (connection([base.comments[0]], "comment-page-2")
                                              if not variables["after"]
                                              else connection([base.comments[1]]))
        elif query == REPLIES_QUERY and variables["id"] == uid(100):
            assert "includeArchived: true" in query
            result["comment"]["children"] = (connection([base.comments[2]], "reply-page-2")
                                                if not variables["after"]
                                                else connection([base.comments[3]]))
        return result

    result = connector(handle).snapshot()
    assert result.complete, result.errors
    first, archived = result.documents
    assert long_description in first["text"] and long_comment in first["text"]
    assert first["metadata"]["comments"] == 5
    assert first["modified_at"] == "2026-10-07T11:00:00Z"
    assert first["metadata"]["priority"] == 2
    assert first["metadata"]["assignee"]["name"] == "Ada"
    assert first["metadata"]["project"]["name"] == "Roadmap"
    assert archived["metadata"]["archived_at"]
    assert result.metadata["issue_states"] == "all"
    assert [call[0] for call in base.calls].count(VIEWER_QUERY) == 2


def test_only_explicit_teams_are_requested_and_empty_team_is_complete():
    provider = Provider([])
    result = connector(provider, team_ids=[uid(1), uid(2)]).snapshot()
    assert result.complete and not result.documents
    assert [variables["id"] for query, variables in provider.calls if query == TEAM_QUERY] == [uid(1), uid(2)]


@pytest.mark.parametrize("value", ["ENG", "ENG-123", "https://linear.app", "123", "{00000000-0000-4000-8000-000000000001}", None])
def test_requires_exact_team_and_workspace_uuid(value):
    with pytest.raises((ConnectorError, ValueError)):
        LinearConnector("synthetic", [value], "pilot", organization_id=uid(9))
    with pytest.raises((ConnectorError, ValueError)):
        LinearConnector("synthetic", [uid(1)], "pilot", organization_id=value)


@pytest.mark.parametrize("teams", [[], [uid(1), uid(1)], "not-a-list", [uid(1)] * 101])
def test_requires_bounded_unique_explicit_scope(teams):
    with pytest.raises((ConnectorError, ValueError)):
        LinearConnector("synthetic", teams, "pilot", organization_id=uid(9))


def test_personal_key_auth_is_explicit():
    result = connector(Provider([]), auth_type="api_key", expected_auth="synthetic-secret").snapshot()
    assert result.complete
    with pytest.raises(ValueError):
        LinearConnector("synthetic", [uid(1)], "pilot", organization_id=uid(9), auth_type="guess")


def test_wrong_workspace_stops_before_any_content_read():
    calls = []

    def handle(query, variables):
        calls.append(query)
        return {"viewer": {"organization": {"id": uid(8)}}}

    result = connector(handle).snapshot()
    assert_failed(result, "linear_workspace_mismatch")
    assert result.authorization_failed and not result.documents
    assert calls == [VIEWER_QUERY]


@pytest.mark.parametrize("mutation", ["missing_team", "wrong_team", "wrong_team_workspace", "wrong_issue_team", "wrong_comment_issue", "wrong_comment_parent"])
def test_scope_changes_quarantine(mutation):
    provider = Provider(comments=[comment()])

    def handle(query, variables):
        result = provider(query, variables)
        if query == TEAM_QUERY:
            if mutation == "missing_team":
                result["team"] = None
            elif mutation == "wrong_team":
                result["team"]["id"] = uid(2)
            elif mutation == "wrong_team_workspace":
                result["team"]["organization"]["id"] = uid(8)
            elif mutation == "wrong_issue_team":
                result["team"]["issues"]["nodes"][0]["team"] = team(2)
        if query == COMMENTS_QUERY:
            if mutation == "wrong_comment_issue":
                result["issue"]["comments"]["nodes"][0]["issue"]["id"] = uid(11)
            elif mutation == "wrong_comment_parent":
                result["issue"]["comments"]["nodes"][0]["parent"] = {"id": uid(101)}
        return result

    result = connector(handle).snapshot()
    assert_failed(result)
    assert result.authorization_failed and not result.documents


@pytest.mark.parametrize("code,authorization", [("UNAUTHENTICATED", True), ("FORBIDDEN", True), ("INTERNAL_SERVER_ERROR", False)])
def test_graphql_errors_with_http_200_and_partial_data_are_never_accepted(code, authorization):
    provider = Provider()

    def handle(query, variables):
        data = provider(query, variables)
        if query == COMMENTS_QUERY:
            return httpx.Response(200, json={"data": data, "errors": [
                {"message": "sensitive provider response synthetic-secret", "extensions": {"code": code}}]})
        return data

    result = connector(handle).snapshot()
    assert_failed(result, "linear_graphql_error")
    assert result.authorization_failed is authorization
    assert not result.documents
    assert "sensitive" not in json.dumps(result.to_dict())
    assert "synthetic-secret" not in json.dumps(result.to_dict())


@pytest.mark.parametrize("status", [401, 403, 404, 302, 429, 503])
def test_http_failures_redirects_and_retries_are_bounded(status):
    calls = []

    def handle(query, variables):
        calls.append(query)
        return httpx.Response(status, headers={"location": "https://example.com/leak", "Retry-After": "0"})

    result = connector(handle, limits=Limits(attempts=2)).snapshot()
    assert_failed(result)
    assert len(calls) == (2 if status in (429, 503) else 1)
    assert result.authorization_failed is (status in (401, 403, 404))


@pytest.mark.parametrize("bad", ["missing_page_info", "cursor_loop", "duplicate", "empty_next_page", "missing_description", "missing_comment_body", "invalid_data"])
def test_malformed_or_uncertain_completeness_is_quarantined(bad):
    provider = Provider(comments=[comment()])

    def handle(query, variables):
        data = provider(query, variables)
        if query == COMMENTS_QUERY:
            if bad == "missing_page_info":
                data["issue"]["comments"]["pageInfo"] = {}
            elif bad == "cursor_loop":
                number = 100 if not variables["after"] else 101
                provider.comments.append(comment(number))
                data["issue"]["comments"] = connection([comment(number)], "same-cursor")
            elif bad == "duplicate":
                data["issue"]["comments"] = connection([comment(), comment()])
            elif bad == "empty_next_page":
                data["issue"]["comments"] = connection([], "more")
            elif bad == "missing_comment_body":
                del data["issue"]["comments"]["nodes"][0]["body"]
        if bad == "missing_description" and query == TEAM_QUERY:
            del data["team"]["issues"]["nodes"][0]["description"]
        if bad == "invalid_data":
            return httpx.Response(200, json={"data": []})
        return data

    result = connector(handle).snapshot()
    assert_failed(result)


def test_edit_during_comment_scan_is_not_claimed_complete():
    provider = Provider()

    def handle(query, variables):
        data = provider(query, variables)
        if query == VERIFY_ISSUE_QUERY:
            data["issue"]["title"] = "Changed while reading"
        return data

    assert_failed(connector(handle).snapshot(), "source_changed_during_sync")


@pytest.mark.parametrize("limits,code", [
    (Limits(max_requests=1), "request_limit"),
    (Limits(max_documents=1), "document_limit"),
    (Limits(max_response_bytes=20), "response_size_limit"),
    (Limits(max_total_response_bytes=200), "total_response_size_limit"),
    (Limits(max_snapshot_text_bytes=20), "snapshot_size_limit"),
    (Limits(max_depth=1), "depth_limit"),
])
def test_limits_quarantine_without_deletions(limits, code):
    provider = Provider([issue(), issue(11)], [comment(), comment(101, parent=100), comment(102, parent=101)])
    result = connector(provider, limits=limits).snapshot()
    assert_failed(result, code)
    assert result.metadata["requests"] <= limits.max_requests


def test_unsafe_output_urls_fallback_without_fetching_external_destinations():
    provider = Provider([issue(url="javascript:unsafe")], [comment(url="https://example.com/secret")])
    result = connector(provider).snapshot()
    assert result.complete
    assert result.documents[0]["url"] == "https://linear.app/issue/" + uid(10)
    assert "Source: https://linear.app/issue/" in result.documents[0]["text"]


@pytest.mark.parametrize("query", ["mutation { issueDelete(id: \"x\") { success } }", "query { teams { nodes { id } } }", [], None])
def test_transport_rejects_mutations_and_arbitrary_graphql(query):
    def unreachable(request):
        raise AssertionError("Disallowed operation reached network")

    http = ProviderHTTP("synthetic", client=httpx.Client(transport=httpx.MockTransport(unreachable)))
    with pytest.raises(ConnectorError, match="disallowed_method"):
        http.json("POST", "https://api.linear.app", "/graphql", body={"query": query})


def test_export_cli_routes_linear_with_explicit_workspace(monkeypatch, tmp_path, capsys):
    from company_brain.connectors import __main__ as export

    config, output = tmp_path / "config.json", tmp_path / "snapshot.json"
    config.write_text(json.dumps({"source": "linear", "team_ids": [uid(1)],
                                  "organization_id": uid(9), "collection": "pilot"}))
    monkeypatch.setenv("COMPANY_BRAIN_LINEAR_TOKEN", "synthetic-secret")
    monkeypatch.setattr("sys.argv", ["export", "--config", str(config), "--output", str(output)])
    calls = []

    def factory(token, team_ids, collection, **kwargs):
        calls.append((token, team_ids, collection, kwargs))
        return connector(Provider([]))

    monkeypatch.setattr(export, "LinearConnector", factory)
    assert export.main() == 0
    assert calls == [("synthetic-secret", [uid(1)], "pilot", {"organization_id": uid(9), "auth_type": "oauth"})]
    assert json.loads(output.read_text())["source"] == "linear"
    assert output.stat().st_mode & 0o777 == 0o600
    assert "synthetic-secret" not in capsys.readouterr().out
