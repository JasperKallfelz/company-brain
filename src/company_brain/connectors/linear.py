"""Read-only Linear issue snapshots restricted to approved workspace/team UUIDs."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterator

from .common import ConnectorError, ProviderHTTP, Snapshot, add_document, source_url

ORIGIN = "https://api.linear.app"
PAGE_SIZE = 50
VIEWER_QUERY = "query BrainLinearViewer { viewer { organization { id } } }"
ISSUE_FIELDS = """
  id identifier title description url updatedAt archivedAt trashed
  team { id organization { id } }
  state { id name type }
  priority priorityLabel
  assignee { id name }
  project { id name url }
"""
COMMENT_FIELDS = """
  id body url updatedAt archivedAt user { id name } parent { id }
  issue { id team { id organization { id } } }
"""
TEAM_QUERY = """
query BrainLinearTeam($id: String!, $after: String) {
  team(id: $id) {
    id organization { id }
    issues(first: 50, after: $after, includeArchived: true, includeSubTeams: false,
           orderBy: createdAt) {
      nodes { ISSUE_FIELDS }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""".replace("ISSUE_FIELDS", ISSUE_FIELDS)
COMMENTS_QUERY = """
query BrainLinearComments($id: String!, $after: String) {
  issue(id: $id) {
    ISSUE_FIELDS
    comments(first: 50, after: $after, includeArchived: true, orderBy: createdAt,
             filter: { parent: { null: true } }) {
      nodes { COMMENT_FIELDS }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""".replace("ISSUE_FIELDS", ISSUE_FIELDS).replace("COMMENT_FIELDS", COMMENT_FIELDS)
REPLIES_QUERY = """
query BrainLinearReplies($id: String!, $after: String) {
  comment(id: $id) {
    COMMENT_FIELDS
    children(first: 50, after: $after, includeArchived: true, orderBy: createdAt) {
      nodes { COMMENT_FIELDS }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""".replace("COMMENT_FIELDS", COMMENT_FIELDS)
VERIFY_ISSUE_QUERY = """
query BrainLinearVerifyIssue($id: String!) { issue(id: $id) { ISSUE_FIELDS } }
""".replace("ISSUE_FIELDS", ISSUE_FIELDS)
READ_QUERIES = frozenset({VIEWER_QUERY, TEAM_QUERY, COMMENTS_QUERY, REPLIES_QUERY,
                          VERIFY_ISSUE_QUERY})


def linear_id(value: Any) -> str:
    """Require a full UUID, never a slug, issue shorthand, URL or query input."""
    if not isinstance(value, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        raise ConnectorError("invalid_linear_id")
    return value.lower()


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise ConnectorError("invalid_provider_response")
    return value


def _date(value: Any) -> datetime:
    date = datetime.fromisoformat(_string(value).replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ConnectorError("invalid_provider_response")
    return date


class LinearConnector:
    """Not thread-safe. OAuth is default; personal keys require auth_type=api_key."""

    def __init__(self, token: str, team_ids: list[str], collection: str, *,
                 organization_id: str, auth_type: str = "oauth", **http_options: Any):
        if not isinstance(team_ids, list) or not 1 <= len(team_ids) <= 100:
            raise ValueError("one to 100 explicit Linear team UUIDs are required")
        self.teams = [linear_id(value) for value in team_ids]
        self.organization_id = linear_id(organization_id)
        if len(set(self.teams)) != len(self.teams) or not isinstance(collection, str) or not collection:
            raise ValueError("a collection and unique explicit Linear teams are required")
        if auth_type not in {"oauth", "api_key"}:
            raise ValueError("Linear auth_type must be oauth or api_key")
        self.collection = collection
        self.http = ProviderHTTP(token, **http_options)
        if auth_type == "api_key":
            self.http.headers["Authorization"] = token

    def close(self) -> None:
        self.http.close()

    def snapshot(self) -> Snapshot:
        self.http.reset()
        self.result = Snapshot("linear", self.collection, metadata={
            "organization_id": self.organization_id, "team_ids": self.teams,
            "coverage": "issues_with_markdown_and_comment_threads",
            "include_archived": True, "include_subteams": False, "issue_states": "all",
            "excluded_content": ["trashed_issues", "attachments_and_media_bytes",
                                 "external_link_targets", "history", "project_documents"],
        })
        self.issue_ids: set[str] = set()
        try:
            self._verify_workspace()
            for team_id in self.teams:
                for issue in self._pages(TEAM_QUERY, "team", "issues", team_id,
                                         lambda item: self._check_team(item, team_id)):
                    issue_id = self._check_issue(issue, team_id)
                    if issue_id in self.issue_ids:
                        raise ConnectorError("duplicate_issue_during_sync")
                    self.issue_ids.add(issue_id)
                    if issue["trashed"] is not None and not isinstance(issue["trashed"], bool):
                        raise ConnectorError("invalid_provider_response")
                    if issue["trashed"]:
                        self.result.count("excluded_trashed_issues")
                        continue
                    self._document(issue, team_id)
            self._verify_workspace()
        except ConnectorError as error:
            self.result.fail(error)
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            self.result.fail(ConnectorError("invalid_provider_response"))
        self.result.metadata["requests"] = self.http.requests
        return self.result

    def _query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.http.json("POST", ORIGIN, "/graphql",
                                  body={"query": query, "variables": variables or {}})
        errors = response.get("errors", [])
        if not isinstance(errors, list):
            raise ConnectorError("invalid_provider_response")
        if errors:
            # Never include provider messages (which can contain source text or
            # credentials). Even data+errors with HTTP 200 is incomplete.
            status = None
            for error in errors:
                if not isinstance(error, dict):
                    continue
                extensions = error.get("extensions", {})
                if not isinstance(extensions, dict):
                    continue
                code = extensions.get("code")
                if code in {"UNAUTHENTICATED", "AUTHENTICATION_ERROR"}:
                    status = 401
                elif code in {"FORBIDDEN", "PERMISSION_DENIED", "NOT_FOUND"}:
                    status = 403
                elif isinstance(extensions.get("statusCode"), int) and extensions["statusCode"] in (
                        401, 403, 404):
                    status = extensions["statusCode"]
            raise ConnectorError("linear_graphql_error", status=status)
        if not isinstance(response.get("data"), dict):
            raise ConnectorError("invalid_provider_response")
        return response["data"]

    def _organization(self, item: dict[str, Any]) -> None:
        if linear_id(item["organization"]["id"]) != self.organization_id:
            raise ConnectorError("linear_workspace_mismatch", status=403)

    def _verify_workspace(self) -> None:
        viewer = self._query(VIEWER_QUERY).get("viewer")
        if not isinstance(viewer, dict):
            raise ConnectorError("linear_viewer_unavailable", status=403)
        self._organization(viewer)

    def _check_team(self, team: dict[str, Any], expected: str) -> None:
        if linear_id(team["id"]) != expected:
            raise ConnectorError("linear_team_mismatch", status=403)
        self._organization(team)

    def _check_issue(self, issue: dict[str, Any], team_id: str) -> str:
        issue_id = linear_id(issue["id"])
        self._check_team(issue["team"], team_id)
        return issue_id

    def _same_issue(self, current: dict[str, Any], original: dict[str, Any], team_id: str) -> None:
        self._check_issue(current, team_id)
        if {key: current[key] for key in original} != original:
            raise ConnectorError("source_changed_during_sync")

    def _pages(self, query: str, entity: str, field: str, external_id: str,
               check: Any) -> Iterator[dict[str, Any]]:
        cursor, cursors, seen = None, set(), set()
        while True:
            parent = self._query(query, {"id": external_id, "after": cursor}).get(entity)
            if not isinstance(parent, dict):
                raise ConnectorError("linear_resource_unavailable", status=403)
            check(parent)
            connection = parent[field]
            nodes, info = connection["nodes"], connection["pageInfo"]
            if (not isinstance(nodes, list) or len(nodes) > PAGE_SIZE
                    or not isinstance(info.get("hasNextPage"), bool)):
                raise ConnectorError("invalid_pagination")
            for node in nodes:
                node_id = linear_id(node["id"])
                if node_id in seen:
                    raise ConnectorError("duplicate_resource_during_sync")
                seen.add(node_id)
                yield node
            if not info["hasNextPage"]:
                return
            cursor = info.get("endCursor")
            if not nodes or not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ConnectorError("invalid_pagination")
            cursors.add(cursor)

    def _comments(self, issue: dict[str, Any], team_id: str) -> Iterator[dict[str, Any]]:
        seen: set[str] = set()

        def visit(comment: dict[str, Any], parent_id: str | None, depth: int):
            if depth > self.http.limits.max_depth:
                raise ConnectorError("depth_limit")
            comment_id = linear_id(comment["id"])
            self._check_issue(comment["issue"], team_id)
            if (linear_id(comment["issue"]["id"]) != linear_id(issue["id"])
                    or (linear_id(comment["parent"]["id"]) if comment["parent"] else None)
                    != parent_id):
                raise ConnectorError("linear_comment_scope_mismatch", status=403)
            if comment_id in seen:
                raise ConnectorError("duplicate_comment_during_sync")
            seen.add(comment_id)
            yield comment

            def check(current):
                if {key: current[key] for key in comment} != comment:
                    raise ConnectorError("source_changed_during_sync")

            for child in self._pages(REPLIES_QUERY, "comment", "children", comment_id, check):
                yield from visit(child, comment_id, depth + 1)

        for root in self._pages(COMMENTS_QUERY, "issue", "comments", issue["id"],
                                lambda current: self._same_issue(current, issue, team_id)):
            yield from visit(root, None, 0)

    def _document(self, issue: dict[str, Any], team_id: str) -> None:
        title = f"{_string(issue['identifier'])}: {_string(issue['title'])}"
        description = issue["description"]
        if description is not None:
            _string(description)
        modified = _string(issue["updatedAt"])
        _date(modified)
        url = source_url(issue["url"], {"linear.app"},
                         "https://linear.app/issue/" + linear_id(issue["id"]))
        state = issue["state"]
        linear_id(state["id"])
        assignee, project = issue["assignee"], issue["project"]
        for value in (assignee, project):
            if value is not None:
                linear_id(value["id"])
                _string(value["name"])
        if project is not None:
            project = {**project, "url": source_url(project["url"], {"linear.app"}, url)}
        priority = issue["priority"]
        if isinstance(priority, bool) or not isinstance(priority, (int, float)) or priority not in range(5):
            raise ConnectorError("invalid_provider_response")
        lines = [title, f"Status: {_string(state['name'])} ({_string(state['type'])})",
                 f"Priority: {_string(issue['priorityLabel'])} ({priority})",
                 f"Assignee: {assignee['name'] if assignee else 'Unassigned'}",
                 f"Project: {project['name'] if project else 'None'}",
                 f"Archived: {issue['archivedAt'] or 'No'}", "", description or ""]
        count = 0
        for comment in self._comments(issue, team_id):
            body, date = _string(comment["body"]), _string(comment["updatedAt"])
            if _date(date) > _date(modified):
                modified = date
            user = comment["user"]
            if user is not None:
                linear_id(user["id"])
                _string(user["name"])
            comment_url = source_url(comment["url"], {"linear.app"}, url)
            lines.extend(["", f"Comment {comment['id']} ({user['name'] if user else 'Unknown author'}; "
                          f"{date})", f"Source: {comment_url}", body])
            count += 1
        current = self._query(VERIFY_ISSUE_QUERY, {"id": issue["id"]}).get("issue")
        if not isinstance(current, dict):
            raise ConnectorError("linear_resource_unavailable", status=403)
        self._same_issue(current, issue, team_id)
        add_document(self.result, {
            "source": "linear", "external_id": linear_id(issue["id"]),
            "collection": self.collection, "title": title, "text": "\n".join(lines),
            "url": url,
            "modified_at": modified,
            "metadata": {"content_format": "markdown", "identifier": issue["identifier"],
                         "organization_id": self.organization_id, "team_id": team_id,
                         "status": state, "priority": priority, "priority_label": issue["priorityLabel"],
                         "assignee": assignee, "project": project, "archived_at": issue["archivedAt"],
                         "issue_updated_at": issue["updatedAt"], "comments": count},
        }, self.http.limits)
        self.result.count("comments", count)
