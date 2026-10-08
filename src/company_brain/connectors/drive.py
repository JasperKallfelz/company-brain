"""Folder-scoped Google Drive originals and complete Google Docs tab text."""

from __future__ import annotations

from typing import Any, Iterator

from .common import (ConnectorError, ProviderHTTP, Snapshot, add_document,
                     resource_id, source_url)

ORIGIN = "https://www.googleapis.com"
DOCS_ORIGIN = "https://docs.googleapis.com"
FOLDER = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
SHORTCUT = "application/vnd.google-apps.shortcut"
TEXT_TYPES = {"text/plain", "text/markdown", "text/x-markdown", "text/csv",
              "text/tab-separated-values", "application/json"}
FIELDS = "id,name,mimeType,parents,trashed,modifiedTime,version,driveId,webViewLink,size,capabilities(canDownload)"


class DriveConnector:
    """Read-only token supplied by the caller; never loads host credentials."""

    def __init__(self, token: str, root_folder_ids: list[str], collection: str, **http_options: Any):
        self.roots = [resource_id(value) for value in root_folder_ids]
        if not collection or not self.roots:
            raise ValueError("a collection and explicit Drive folder roots are required")
        self.collection = collection
        self.http = ProviderHTTP(token, **http_options)

    def close(self) -> None:
        self.http.close()

    def snapshot(self) -> Snapshot:
        self.http.reset()
        self.result = Snapshot("drive", self.collection, metadata={
            "root_folder_ids": self.roots, "coverage": "supported_text_types",
            "supported_mime_types": sorted(TEXT_TYPES | {GOOGLE_DOC}),
            "excluded_content": ["shortcuts", "pdf", "images", "office_binary_files",
                                 "sheets", "slides", "comments", "unaccepted_suggestions"],
        })
        self.visited: set[str] = set()
        for root in self.roots:
            try:
                self._visit(root, None, 0, root=True)
            except ConnectorError as error:
                self.result.fail(error, root)
            except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
                self.result.fail(ConnectorError("invalid_provider_response"), root)
        self.result.metadata["requests"] = self.http.requests
        return self.result

    def _metadata(self, file_id: str) -> dict[str, Any]:
        value = self.http.json("GET", ORIGIN, f"/drive/v3/files/{resource_id(file_id)}",
                               params={"fields": FIELDS, "supportsAllDrives": "true"})
        if value.get("id") != file_id:
            raise ConnectorError("invalid_provider_response")
        return value

    @staticmethod
    def _parent(file: dict[str, Any], expected: str | None) -> None:
        if expected is not None and expected not in file.get("parents", []):
            raise ConnectorError("source_moved_out_of_scope")

    def _children(self, folder: dict[str, Any]) -> Iterator[str]:
        cursor, seen = None, set()
        while True:
            params = {
                "q": f"'{resource_id(folder['id'])}' in parents and trashed = false",
                "pageSize": 1000, "fields": "nextPageToken,incompleteSearch,files(id)",
                "includeItemsFromAllDrives": "true", "supportsAllDrives": "true",
                "spaces": "drive", "corpora": "drive" if folder.get("driveId") else "user",
            }
            if folder.get("driveId"):
                params["driveId"] = resource_id(folder["driveId"])
            if cursor:
                params["pageToken"] = cursor
            value = self.http.json("GET", ORIGIN, "/drive/v3/files", params=params)
            if value.get("incompleteSearch"):
                raise ConnectorError("provider_query_incomplete")
            if not isinstance(value.get("files"), list):
                raise ConnectorError("invalid_pagination")
            for file in value["files"]:
                yield resource_id(file["id"])
            cursor = value.get("nextPageToken")
            if not cursor:
                return
            if not isinstance(cursor, str) or cursor in seen:
                raise ConnectorError("invalid_pagination")
            seen.add(cursor)

    def _visit(self, file_id: str, parent: str | None, depth: int, *, root: bool = False) -> None:
        if depth > self.http.limits.max_depth:
            raise ConnectorError("depth_limit")
        if file_id in self.visited:
            return
        self.visited.add(file_id)
        file = self._metadata(file_id)
        self._parent(file, parent)
        mime = file["mimeType"]
        if root and mime != FOLDER:
            raise ConnectorError("drive_root_must_be_folder")
        if file.get("trashed"):
            self.result.deleted_external_ids.append(file_id)
            return
        if mime == FOLDER:
            for child in self._children(file):
                self._visit(child, file_id, depth + 1)
            current = self._metadata(file_id)
            self._parent(current, parent)
            if current.get("trashed"):
                raise ConnectorError("source_changed_during_sync")
            self.result.count("folders")
            return
        if mime not in TEXT_TYPES | {GOOGLE_DOC}:
            self.result.count("excluded_shortcuts" if mime == SHORTCUT else "unsupported_files")
            types = self.result.metadata.setdefault("unsupported_mime_types", {})
            types[mime] = types.get(mime, 0) + 1
            return
        if file.get("capabilities", {}).get("canDownload") is not True:
            raise ConnectorError("download_not_authorized", status=403)
        if int(file.get("size", 0)) > self.http.limits.max_response_bytes:
            raise ConnectorError("response_size_limit")
        metadata: dict[str, Any] = {"mime_type": mime, "version": file.get("version"),
                                    "content_format": "plain_text"}
        if mime == GOOGLE_DOC:
            document = self.http.json("GET", DOCS_ORIGIN, f"/v1/documents/{file_id}", params={
                "includeTabsContent": "true", "suggestionsViewMode": "PREVIEW_WITHOUT_SUGGESTIONS",
            })
            if document.get("documentId") != file_id:
                raise ConnectorError("invalid_provider_response")
            text, tab_ids = self._google_doc(document)
            metadata["tab_ids"] = tab_ids
            metadata["revision_id"] = document.get("revisionId")
        else:
            raw = self.http.request("GET", ORIGIN, f"/drive/v3/files/{file_id}",
                                    params={"alt": "media", "supportsAllDrives": "true"})
            try:
                text = raw.decode("utf-8-sig", errors="strict")
            except UnicodeError:
                raise ConnectorError("unsupported_text_encoding") from None
        current = self._metadata(file_id)
        self._parent(current, parent)
        if (current.get("trashed") or current.get("version") != file.get("version") or
                current.get("modifiedTime") != file.get("modifiedTime") or
                current.get("capabilities", {}).get("canDownload") is not True):
            raise ConnectorError("source_changed_during_sync")
        add_document(self.result, {
            "source": "drive", "external_id": file_id, "collection": self.collection,
            "title": file["name"], "text": text,
            "url": source_url(file.get("webViewLink"), {"drive.google.com", "docs.google.com"},
                              f"https://drive.google.com/file/d/{file_id}/view"),
            "modified_at": file["modifiedTime"], "metadata": metadata,
        }, self.http.limits)

    def _google_doc(self, document: dict[str, Any]) -> tuple[str, list[str]]:
        # includeTabsContent=true is compulsory; accepting a legacy body here
        # could silently index just the first tab.
        if not isinstance(document.get("tabs"), list) or not document["tabs"]:
            raise ConnectorError("missing_google_doc_tabs")
        parts: list[str] = []
        tab_ids: list[str] = []

        def elements(content: list[dict[str, Any]], depth: int) -> None:
            if depth > self.http.limits.max_depth:
                raise ConnectorError("depth_limit")
            for element in content:
                if "paragraph" in element:
                    for inline in element["paragraph"].get("elements", []):
                        if "textRun" in inline:
                            parts.append(inline["textRun"].get("content", ""))
                        elif "person" in inline:
                            parts.append(inline["person"].get("personProperties", {}).get("name", ""))
                        elif "richLink" in inline:
                            parts.append(inline["richLink"].get("richLinkProperties", {}).get("title", ""))
                        elif any(key in inline for key in ("inlineObjectElement", "equation")):
                            self.result.count("excluded_doc_objects")
                        elif not any(key in inline for key in ("footnoteReference", "pageBreak", "columnBreak", "horizontalRule", "autoText")):
                            raise ConnectorError("unsupported_google_doc_element")
                elif "table" in element:
                    for row in element["table"].get("tableRows", []):
                        for cell in row.get("tableCells", []):
                            elements(cell.get("content", []), depth + 1)
                            parts.append("\t")
                        parts.append("\n")
                elif "tableOfContents" in element:
                    elements(element["tableOfContents"].get("content", []), depth + 1)
                elif "sectionBreak" not in element:
                    raise ConnectorError("unsupported_google_doc_element")

        def tabs(values: list[dict[str, Any]], depth: int) -> None:
            if depth > self.http.limits.max_depth:
                raise ConnectorError("depth_limit")
            for tab in values:
                properties = tab["tabProperties"]
                tab_id = properties["tabId"]
                if tab_id in tab_ids:
                    raise ConnectorError("duplicate_google_doc_tab")
                tab_ids.append(tab_id)
                parts.append(f"\n{properties.get('title', '')}\n")
                body = tab["documentTab"]
                elements(body.get("body", {}).get("content", []), depth + 1)
                for section in ("headers", "footers", "footnotes"):
                    for value in body.get(section, {}).values():
                        elements(value.get("content", []), depth + 1)
                tabs(tab.get("childTabs", []), depth + 1)

        tabs(document["tabs"], 0)
        return "".join(parts), tab_ids
