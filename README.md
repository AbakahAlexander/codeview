# Codeview

Local-first, IDE-independent code explorer.

Point Codeview at a codebase, search for a class, function, method, or symbol, and interactively explore nearby relationships — without dumping the entire repository into one giant graph.

## Features

- Local analysis only (no repository upload, no AI dependency)
- SQLite-backed indexes under `~/.codeview/indexes`
- Lazy relationship loading when you expand a branch
- Neighborhood exploration: definition, callers, callees, references, inheritance, overrides, source
- Breadcrumbs, browser-style history, and saved exploration paths
- Pluggable graph providers (starts with Python via Jedi)

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Index a local Python project and open the UI
codeview serve --root /path/to/python/repo --port 8765
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

You can also index first, then serve:

```bash
codeview index /path/to/python/repo
codeview serve --db ~/.codeview/indexes/<index-name>.sqlite3
```

Or start the UI empty and paste a local path into the sidebar.

## Commands

```bash
codeview providers
codeview index <path> [--provider jedi-python] [--db path.sqlite3]
codeview search <query> --db path.sqlite3
codeview serve [--root <path>] [--db path.sqlite3] [--host 127.0.0.1] [--port 8765]
```

## Provider interface

Codeview talks to indexers through a common `GraphProvider` API:

- `index(root)` → symbols
- `structural_relations(root, symbols)` → cheap edges (containment, inheritance, overrides)
- `expand(root, symbol, kind, symbols_by_id)` → lazy edges (calls, callers, references)
- `source_for(root, symbol)` → source snippet

The first bundled provider is `jedi-python`. The same interface is intended for later SCIP, Tree-sitter-based tools, Codegraph, Joern, or language-specific indexers.

## Design notes

- The UI shows only the current symbol and expandable nearby relationships
- Relationship queries are resolved on demand and cached in SQLite
- All processing stays on the machine running Codeview

## License

MIT
