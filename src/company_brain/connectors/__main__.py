"""Export an approved source snapshot; no credentials are discovered implicitly."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from . import ConnectorError, DriveConnector, LinearConnector, NotionConnector


def main() -> int:
    parser = argparse.ArgumentParser(description="Export one explicitly scoped Company Brain source")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="Sensitive snapshot; written atomically with mode 0600")
    args = parser.parse_args()
    connector = None
    temporary = None
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if config.get("source") == "notion":
            connector = NotionConnector(
                os.environ.get("COMPANY_BRAIN_NOTION_TOKEN", ""), config.get("root_page_ids", []),
                config["collection"], root_database_ids=config.get("root_database_ids", []))
        elif config.get("source") == "drive":
            connector = DriveConnector(
                os.environ.get("COMPANY_BRAIN_DRIVE_TOKEN", ""), config.get("root_folder_ids", []),
                config["collection"])
        elif config.get("source") == "linear":
            connector = LinearConnector(
                os.environ.get("COMPANY_BRAIN_LINEAR_TOKEN", ""), config.get("team_ids", []),
                config["collection"], organization_id=config["organization_id"],
                auth_type=config.get("auth_type", "oauth"))
        else:
            raise ValueError("unsupported source")
        snapshot = connector.snapshot()
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".snapshot-",
                                         dir=args.output.parent, delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            json.dump(snapshot.to_dict(), handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, args.output)
        temporary = None
        print(json.dumps({"complete": snapshot.complete, "documents": len(snapshot.documents),
                          "errors": len(snapshot.errors), "quarantine_all": snapshot.quarantine_all}))
        # Failed snapshots MUST still be ingested, so stale documents are
        # quarantined. A shell `export && ingest` is unsafe for this reason.
        return 0 if snapshot.complete else 2
    except (ConnectorError, ValueError, OSError, KeyError, TypeError):
        print("Snapshot configuration or output failed; no provider details logged.")
        return 1
    finally:
        if connector:
            connector.close()
        if temporary:
            Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
