from __future__ import annotations

import argparse
import atexit
import json
import sys
from pathlib import Path

from codeview import __version__
from codeview.paths import (
    codeview_home,
    is_ephemeral_clone,
    purge_codeview_data,
    purge_ephemeral_session,
)
from codeview.providers import list_providers
from codeview.repos import looks_like_git_url, resolve_target
from codeview.service import ExplorerService, default_db_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codeview",
        description="Local-first code explorer",
    )
    parser.add_argument("--version", action="version", version=f"codeview {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    index_p = sub.add_parser("index", help="Build a precise index into SQLite")
    index_p.add_argument("target", help="Local directory or public git URL")
    index_p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path (default: ~/.codeview/indexes/<root>.sqlite3)",
    )

    serve_p = sub.add_parser("serve", help="Start the local web UI (indexes in the background if needed)")
    serve_p.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Local directory or public git URL",
    )
    serve_p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database to open (optional if target is provided)",
    )
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8765)
    serve_p.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Deprecated: use positional target instead",
    )

    sub.add_parser("providers", help="List graph backends")

    doctor_p = sub.add_parser("doctor", help="Check tools or purge local Codeview data")
    doctor_p.add_argument(
        "--fetch-rg",
        action="store_true",
        help="Force download of a ripgrep binary into ~/.codeview/bin",
    )
    doctor_p.add_argument(
        "--purge",
        action="store_true",
        help=f"Delete all Codeview data under {codeview_home()} (indexes, caches, tools)",
    )

    search_p = sub.add_parser("search", help="Search symbols in an index")
    search_p.add_argument("query")
    search_p.add_argument("--db", type=Path, required=True)

    return parser


_ephemeral_cleaned: set[str] = set()


def _cleanup_ephemeral_serve(service: ExplorerService, root: Path, db: Path) -> None:
    """Drop peek clones + their index when the serve process exits."""
    key = str(root.resolve())
    if key in _ephemeral_cleaned or not is_ephemeral_clone(root):
        return
    _ephemeral_cleaned.add(key)
    try:
        if service.store is not None:
            service.store.close()
            service.store = None
    except Exception:
        pass
    result = purge_ephemeral_session(root=root, db_path=db)
    if result.get("purged"):
        print(f"Purged ephemeral peek: {root}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = ExplorerService()

    if args.command == "providers":
        print(json.dumps(list_providers(), indent=2))
        return 0

    if args.command == "doctor":
        if args.purge:
            try:
                result = purge_codeview_data()
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(json.dumps(result, indent=2))
            return 0
        from codeview.rgutil import ensure_rg

        try:
            rg = ensure_rg(force_download=bool(args.fetch_rg))
        except Exception as exc:
            print(f"ripgrep: ERROR {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"rg": rg, "ok": True, "home": str(codeview_home())}, indent=2))
        return 0

    if args.command == "index":
        try:
            root = resolve_target(args.target)
        except (FileNotFoundError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        db = args.db or default_db_path(root)
        try:
            stats = service.index_path(root, db_path=db)
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(stats.to_dict(), indent=2))
        print(f"Index written to {db}", file=sys.stderr)
        return 0

    if args.command == "search":
        store = service.open(args.db)
        hits = store.search(args.query)
        print(json.dumps([h.to_dict() for h in hits], indent=2))
        return 0

    if args.command == "serve":
        target = args.target or (str(args.root) if args.root else None)
        root: Path | None = None
        db: Path | None = None
        ephemeral = False
        if target:
            try:
                ephemeral = looks_like_git_url(str(target))
                root = resolve_target(target)
            except (FileNotFoundError, RuntimeError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            db = args.db or default_db_path(root)
            # UI first — index in background when missing or stale.
            status = service.prepare_serve(root, db_path=db)
            print(json.dumps(status, indent=2), file=sys.stderr)
            print(f"Index: {db}", file=sys.stderr)
            if ephemeral:
                print(
                    "Ephemeral peek: clone + index will be deleted when this server exits.",
                    file=sys.stderr,
                )
        elif args.db:
            service.open(args.db)
            service.indexer._set(status="ready", percent=100, has_graph=True)
        else:
            print(
                "Pass a local path, a git URL, or --db. Examples:\n"
                "  codeview serve .\n"
                "  codeview serve https://github.com/OWNER/REPO\n"
                "  codeview serve --db ~/.codeview/indexes/....sqlite3",
                file=sys.stderr,
            )
            return 2

        from codeview.server.app import create_app
        import uvicorn

        app = create_app(service)
        print(f"Codeview UI: http://{args.host}:{args.port}", file=sys.stderr)
        if root is not None and db is not None and ephemeral:
            atexit.register(_cleanup_ephemeral_serve, service, root, db)
        try:
            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        finally:
            if root is not None and db is not None and ephemeral:
                _cleanup_ephemeral_serve(service, root, db)
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
