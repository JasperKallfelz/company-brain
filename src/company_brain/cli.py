import argparse
import json
import os
from pathlib import Path
from .config import Settings
from .embeddings import LocalEmbedder
from .models import Snapshot
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Company Brain administration (VM-local only)")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ["init-db", "status", "serve"]:
        sub.add_parser(command)
    p = sub.add_parser("collection-set")
    p.add_argument("collection")
    p.add_argument("--disabled", action="store_true")
    p = sub.add_parser("user-set")
    p.add_argument("subject")
    p.add_argument("--collection", action="append", default=[])
    p.add_argument("--disabled", action="store_true")
    p = sub.add_parser("user-revoke")
    p.add_argument("subject")
    p = sub.add_parser("ingest-snapshot")
    p.add_argument("path", type=Path)
    for command in ["source-quarantine", "source-revoke"]:
        p = sub.add_parser(command)
        p.add_argument("source", choices=["notion", "drive", "linear", "fixture"])
        p.add_argument("collection")
    p = sub.add_parser("sync-once")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.validate(serving=args.command == "serve")
    store = Store(settings)
    if args.command == "serve":
        from .server import create_app
        import uvicorn

        uvicorn.run(
            create_app(settings),
            host=os.environ.get("BRAIN_HOST", "127.0.0.1"),
            port=int(os.environ.get("BRAIN_PORT", "8000")),
            access_log=False,
            proxy_headers=False,
            limit_concurrency=32,
            timeout_keep_alive=5,
        )
        return
    if args.command == "sync-once":
        from .sync import SyncConfig, run_sync

        try:
            if args.config.stat().st_size > 65536:
                raise ValueError("Configuration exceeds 64KiB")
            config = SyncConfig.model_validate_json(args.config.read_text())
            outcome = run_sync(settings, config, args.state_dir, store=store)
            print(json.dumps(outcome))
            raise SystemExit(0 if outcome["success"] else 2)
        except (ValueError, OSError):
            print(json.dumps({"success": False, "error_code": "sync_configuration_or_lock_failed"}))
            raise SystemExit(1) from None
    if args.command == "init-db":
        store.init_db()
    elif args.command == "collection-set":
        store.configure_collection(args.collection, not args.disabled)
    elif args.command == "user-set":
        store.configure_user(args.subject, args.collection, not args.disabled)
    elif args.command == "user-revoke":
        store.revoke_user(args.subject)
    elif args.command == "status":
        print(json.dumps(store.status(), default=str))
    elif args.command == "ingest-snapshot":
        if args.path.stat().st_size > 100 * 1024 * 1024:
            parser.error("Snapshot exceeds 100 MiB; split explicitly into separate collections")
        snapshot = Snapshot.model_validate_json(args.path.read_text())
        print(
            json.dumps(
                store.ingest(
                    snapshot, LocalEmbedder(settings.embedding_model, settings.embedding_revision)
                )
            )
        )
    elif args.command.startswith("source-"):
        store.source_state(
            args.source,
            args.collection,
            "revoked" if args.command == "source-revoke" else "quarantined",
        )


if __name__ == "__main__":
    main()
