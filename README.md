# Codeview

Lightweight, IDE-independent **source code explorer**.

Codeview indexes a codebase using existing open-source graph/indexing engines and provides a fast local web UI for navigating symbols, callers, callees, inheritance, implementations, and references. Instead of jumping through dozens of files or IDE panels, you explore a neighborhood through an interactive, expandable interface — with per-symbol URLs and investigation history.

> **Don't own the indexing. Own the experience.**

SCIP, Codegraph, Kythe, Tree-sitter, Jedi, and similar tools already solve symbol graphs. Codeview is the frontend that makes that data feel like browsing Wikipedia for code — not a thousand-node graph visualization.

## Install

```bash
uvx --from git+https://github.com/AbakahAlexander/codeview@main codeview --help
# or
pipx install 'git+https://github.com/AbakahAlexander/codeview.git'
```

Requires Python 3.10+ and `git`. **ripgrep is fetched automatically** on first
use into `~/.codeview/bin` (or reused from PATH if already installed).

## Quick start

```bash
# explore the current project (auto-detects languages / SCIP)
codeview serve .

# explore a public repository
codeview serve https://github.com/OWNER/REPO
```

Open http://127.0.0.1:8765 — search a symbol, expand **Called by / Calls / Inheritance / Implementations / References**, click through. Each symbol has a URL (`#/s/<id>`).

## Providers

| Provider | Role |
|----------|------|
| `scip` | **Preferred.** Consumes an existing `index.scip` (scip-java, scip-python, scip-typescript, rust-analyzer, …) |
| `treesitter-java` / `scala` / `cxx` | Built-in heuristic indexes when no SCIP is present |
| `jedi-python` | Built-in Python heuristic index |
| `codegraph` | Stub — wire your Codegraph export here |

Auto mode uses SCIP when `index.scip` is present; otherwise it composes language heuristics into one SQLite DB under `~/.codeview/indexes/`.

```bash
# after generating SCIP yourself
scip-java index   # or scip-python / scip-typescript …
codeview serve . --provider scip
```

## Design

- Localhost only — nothing is uploaded
- The **graph is an implementation detail**; the product is expandable exploration
- Providers implement a shared adapter (`index`, `expand`, `source_for`, plus helpers like `get_callers` / `get_references`)
- Heuristic callers/callees are approximate (name-based); prefer SCIP when you need precision
- The UI is a plain expandable tree (`+` / `−`) — fast, no heavy graph chrome

## Layout

```text
codeview/
├── providers/
│   ├── scip/           # consume SCIP indexes
│   ├── codegraph/      # stub for Codegraph-class tools
│   ├── tree_sitter/    # heuristic fallbacks
│   └── jedi/
├── frontend/           # local exploration UI
├── server/             # FastAPI backend
└── …
```

## License

MIT
