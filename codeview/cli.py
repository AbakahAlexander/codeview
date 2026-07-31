from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from codeview import __version__
from codeview.providers import list_providers
from codeview.repos import resolve_target
from codeview.service import ExplorerService, default_db_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codeview",
        description="Local-first multi-language code explorer",
    )
    parser.add_argument("--version", action="version", version=f"codeview {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    index_p = sub.add_parser("index", help="Index a local path or public git URL into SQLite")
    index_p.add_argument(
        "target",
        help="Local directory or public git URL (e.g. https://github.com/apache/iceberg)",
    )
    index_p.add_argument(
        "--provider",
        default="auto",
        help="Provider name, or 'auto' to detect languages (default: auto)",
    )
    index_p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path (default: ~/.codeview/indexes/<root>.sqlite3)",
    )

    serve_p = sub.add_parser(
        "serve",
        help="Index (optional) and start the local web UI",
    )
    serve_p.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Local directory or public git URL to index, e.g. . or https://github.com/apache/iceberg",
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
    serve_p.add_argument("--provider", default="auto")

    sub.add_parser("providers", help="List available graph providers")

    search_p = sub.add_parser("search", help="Search symbols in an index")
    search_p.add_argument("query")
    search_p.add_argument("--db", type=Path, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = ExplorerService()

    if args.command == "providers":
        print(json.dumps(list_providers(), indent=2))
        return 0

    if args.command == "index":
        try:
            root = resolve_target(args.target)
        except (FileNotFoundError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        db = args.db or default_db_path(root)
        try:
            stats = service.index_path(root, provider_name=args.provider, db_path=db)
        except ValueError as exc:
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
        if target:
            try:
                root = resolve_target(target)
            except (FileNotFoundError, RuntimeError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            db = args.db or default_db_path(root)
            try:
                stats = service.index_path(root, provider_name=args.provider, db_path=db)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(json.dumps(stats.to_dict(), indent=2), file=sys.stderr)
            print(f"Index: {db}", file=sys.stderr)
        elif args.db:
            service.open(args.db)
        else:
            print(
                "Pass a local path, a git URL, or --db. Examples:\n"
                "  codeview serve .\n"
                "  codeview serve https://github.com/apache/iceberg\n"
                "  codeview serve --db ~/.codeview/indexes/....sqlite3",
                file=sys.stderr,
            )
            return 2

        from codeview.server.app import create_app
        import uvicorn

        app = create_app(service)
        print(f"Codeview UI: http://{args.host}:{args.port}", file=sys.stderr)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
