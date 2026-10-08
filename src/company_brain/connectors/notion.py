"""Notion text snapshots rooted only in explicitly approved pages/databases."""

from __future__ import annotations

import json
from typing import Any, Iterator
from urllib.parse import quote, unquote

from .common import (ConnectorError, ProviderHTTP, Snapshot, add_document,
                     notion_id, source_url)

ORIGIN = "https://api.notion.com"
API_VERSION = "2026-03-11"
TEXT_BLOCKS = {"paragraph", "heading_1", "heading_2", "heading_3", "heading_4",
               "bulleted_list_item", "numbered_list_item", "quote", "to_do",
               "toggle", "code", "callout", "template", "tab"}
STRUCTURAL_BLOCKS = {"column", "column_list", "table", "divider", "breadcrumb",
                     "table_of_contents"}
MEDIA_BLOCKS = {"image", "video", "audio", "pdf", "file", "embed", "bookmark",
                "link_preview"}


def rich_text(items: list[dict[str, Any]]) -> str:
    return "".join(item.get("plain_text", item.get("text", {}).get("content", ""))
                   for item in items)


class NotionConnector:
    """Not thread-safe. Use a separate connector instance per running sync."""

    def __init__(self, token: str, root_page_ids: list[str], collection: str, *,
                 root_database_ids: list[str] | None = None, **http_options: Any):
        self.roots = [notion_id(value) for value in root_page_ids]
        self.databases = [notion_id(value) for value in root_database_ids or []]
        if not collection or not (self.roots or self.databases):
            raise ValueError("a collection and explicit Notion roots are required")
        self.collection = collection
        http_options.setdefault("interval", 0.34)
        self.http = ProviderHTTP(token, headers={"Notion-Version": API_VERSION}, **http_options)

    def close(self) -> None:
        self.http.close()

    def snapshot(self) -> Snapshot:
        self.http.reset()
        self.result = Snapshot("notion", self.collection, metadata={
            "api_version": API_VERSION, "root_page_ids": self.roots,
            "root_database_ids": self.databases, "coverage": "page_text_and_properties",
            "excluded_content": ["comments", "media_bytes", "external_link_targets",
                                 "synced_blocks_from_other_pages"],
        })
        self.visited_pages: set[str] = set()
        self.visited_databases: set[str] = set()
        for is_database, root in [(False, value) for value in self.roots] + [
                (True, value) for value in self.databases]:
            try:
                if is_database:
                    self._database(root, 0)
                else:
                    self._page(root, 0)
            except ConnectorError as error:
                self.result.fail(error, root)
            except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
                self.result.fail(ConnectorError("invalid_provider_response"), root)
        self.result.metadata["requests"] = self.http.requests
        return self.result

    def _depth(self, depth: int) -> None:
        if depth > self.http.limits.max_depth:
            raise ConnectorError("depth_limit")

    def _get(self, path: str) -> dict[str, Any]:
        return self.http.json("GET", ORIGIN, path)

    def _list(self, path: str, *, post: bool = False) -> Iterator[dict[str, Any]]:
        cursor, cursors = None, set()
        while True:
            values: dict[str, Any] = {"page_size": 100}
            if cursor:
                values["start_cursor"] = cursor
            response = self.http.json("POST" if post else "GET", ORIGIN, path,
                                      **({"body": values} if post else {"params": values}))
            if response.get("request_status", {}).get("type") == "incomplete":
                raise ConnectorError("provider_query_incomplete")
            if not isinstance(response.get("results"), list) or not isinstance(response.get("has_more"), bool):
                raise ConnectorError("invalid_pagination")
            for result in response["results"]:
                if not isinstance(result, dict):
                    raise ConnectorError("invalid_provider_response")
                yield result
            if not response["has_more"]:
                return
            cursor = response.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ConnectorError("invalid_pagination")
            cursors.add(cursor)

    @staticmethod
    def _check_parent(item: dict[str, Any], expected: str | None) -> None:
        if expected is None:
            return
        parent = item.get("parent", {})
        value = parent.get(parent.get("type", ""))
        if not isinstance(value, str) or notion_id(value) != expected:
            raise ConnectorError("source_moved_out_of_scope")

    def _properties(self, page: dict[str, Any]) -> tuple[str, list[str]]:
        title, lines = "", []
        for name, prop in page.get("properties", {}).items():
            kind = prop["type"]
            value = prop.get(kind)
            if kind in {"title", "rich_text", "relation", "people"}:
                # Retrieve these even when the page response looks short: inline
                # property values can be truncated at 25 references.
                property_id = unquote(str(prop["id"]))
                if property_id in {".", ".."} or not property_id:
                    raise ConnectorError("invalid_property_id")
                path = f"/v1/pages/{page['id']}/properties/{quote(property_id, safe='')}"
                items = list(self._list(path))
                values = [item[kind] for item in items]
                if kind in {"title", "rich_text"}:
                    text = rich_text(values)
                elif kind == "relation":
                    text = ", ".join(notion_id(item["id"]) for item in values)
                else:
                    text = ", ".join(item.get("name", item.get("id", "")) for item in values)
                if kind == "title":
                    title = text
                else:
                    lines.append(f"{name}: {text}")
            elif kind in {"select", "status"}:
                lines.append(f"{name}: {(value or {}).get('name', '')}")
            elif kind == "multi_select":
                lines.append(f"{name}: {', '.join(item['name'] for item in value or [])}")
            elif kind in {"number", "checkbox", "date", "url", "email", "phone_number",
                          "created_time", "last_edited_time", "unique_id"}:
                lines.append(f"{name}: {json.dumps(value, ensure_ascii=False)}")
            elif kind in {"formula", "rollup"}:
                # Derived/relation-dependent values can be incomplete without
                # access to their source databases. Do not claim them as facts.
                self.result.count("excluded_derived_properties")
            else:
                self.result.count("excluded_other_properties")
        return title or "Untitled", lines

    def _page(self, page_id: str, depth: int, expected_parent: str | None = None) -> None:
        self._depth(depth)
        page_id = notion_id(page_id)
        if page_id in self.visited_pages:
            return
        self.visited_pages.add(page_id)
        page = self._get(f"/v1/pages/{page_id}")
        if notion_id(page["id"]) != page_id or page.get("object") != "page":
            raise ConnectorError("invalid_provider_response")
        self._check_parent(page, expected_parent)
        if page.get("archived") or page.get("in_trash"):
            self.result.deleted_external_ids.append(page_id)
            return
        title, lines = self._properties(page)
        lines.extend(self._blocks(page_id, depth + 1, page_id, set()))
        # Reject a known mid-scan edit/move instead of marking a mixed document
        # complete. Provider listing APIs are not transactional snapshots.
        current = self._get(f"/v1/pages/{page_id}")
        self._check_parent(current, expected_parent)
        if current.get("last_edited_time") != page.get("last_edited_time") or current.get("archived") or current.get("in_trash"):
            raise ConnectorError("source_changed_during_sync")
        add_document(self.result, {
            "source": "notion", "external_id": page_id, "collection": self.collection,
            "title": title, "text": "\n".join([title, *lines]),
            "url": source_url(page.get("url"), {"www.notion.so", "notion.so", "www.notion.com"},
                              "https://www.notion.so/" + page_id.replace("-", "")),
            "modified_at": page["last_edited_time"],
            "metadata": {"content_format": "plain_text", "coverage": "page_text_and_properties"},
        }, self.http.limits)

    def _database(self, database_id: str, depth: int, expected_parent: str | None = None) -> None:
        self._depth(depth)
        database_id = notion_id(database_id)
        if database_id in self.visited_databases:
            return
        self.visited_databases.add(database_id)
        database = self._get(f"/v1/databases/{database_id}")
        self._check_parent(database, expected_parent)
        if database.get("archived") or database.get("in_trash"):
            self.result.count("archived_databases")
            return
        sources = database.get("data_sources")
        if not isinstance(sources, list):
            raise ConnectorError("missing_data_sources")
        for source in sources:
            source_id = notion_id(source["id"])
            for page in self._list(f"/v1/data_sources/{source_id}/query", post=True):
                self._page(page["id"], depth + 1, source_id)

    def _blocks(self, block_id: str, depth: int, page_id: str,
                active: set[str]) -> list[str]:
        self._depth(depth)
        block_id = notion_id(block_id)
        if block_id in active:
            raise ConnectorError("block_cycle")
        active = {*active, block_id}
        lines = []
        seen = set()
        for block in self._list(f"/v1/blocks/{block_id}/children"):
            child_id = notion_id(block["id"])
            if child_id in seen:
                raise ConnectorError("duplicate_block_during_sync")
            seen.add(child_id)
            if block.get("archived") or block.get("in_trash"):
                continue
            kind = block["type"]
            data = block.get(kind, {})
            self.result.count("blocks")
            if kind == "child_page":
                self._page(child_id, depth + 1, page_id)
                lines.append(data.get("title", ""))
                continue
            if kind == "child_database":
                self._database(child_id, depth + 1, page_id)
                lines.append(data.get("title", ""))
                continue
            if kind == "link_to_page":
                self.result.count("excluded_external_references")
                continue
            if kind == "synced_block" and data.get("synced_from"):
                # Never follow references to a page outside the approved tree.
                self.result.count("excluded_external_references")
                continue
            if kind in TEXT_BLOCKS:
                text = rich_text(data.get("rich_text", []))
                if kind == "to_do":
                    text = ("[x] " if data.get("checked") else "[ ] ") + text
                lines.append(text)
            elif kind == "table_row":
                lines.append(" | ".join(rich_text(cell) for cell in data.get("cells", [])))
            elif kind == "equation":
                lines.append(data.get("expression", ""))
            elif kind in MEDIA_BLOCKS:
                lines.append(rich_text(data.get("caption", [])))
                self.result.count("excluded_media_blocks")
            elif kind not in STRUCTURAL_BLOCKS | {"synced_block"}:
                # Unknown text-like blocks could hide source text. Fail closed.
                raise ConnectorError("unsupported_notion_block")
            if block.get("has_children"):
                lines.extend(self._blocks(child_id, depth + 1, page_id, active))
        return lines
