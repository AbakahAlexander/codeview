# Codeview

Local-first code explorer for large codebases.

Index a project on your machine, search for a symbol, and expand its neighborhood — members, callers, callees, and source — without uploading anything or relying on an IDE.

## Install

```bash
uvx --from git+https://github.com/AbakahAlexander/codeview@main codeview --help
```

Or:

```bash
pipx install 'git+https://github.com/AbakahAlexander/codeview.git'
```

Requires Python 3.10+, `git`, and [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) on your `PATH`.

## Usage

```bash
# explore the current directory
codeview serve .

# explore any public git repository
codeview serve https://github.com/OWNER/REPO
```

Then open http://127.0.0.1:8765.

```bash
codeview index .
codeview search SomeSymbol --db ~/.codeview/indexes/<name>.sqlite3
codeview providers
```

Indexes are stored under `~/.codeview/indexes/`.

## Languages

By default Codeview **auto-detects** languages and builds one combined index:

| Language | Status |
|----------|--------|
| Java | Full index + call graph |
| Scala | Symbols + call edges |
| Python | Full index |
| C / C++ / CUDA | Lazy index (good for very large trees) |

More languages can be added through the provider interface.

## Design

- Runs only on localhost; nothing leaves your machine
- Shows a symbol neighborhood, not a whole-repo graph dump
- Call edges are name-based (AST + search). When several symbols share a name, **all candidates are shown** so nothing is silently omitted
- Pluggable providers (`index`, `expand`, `source_for`) for future language backends

## License

MIT
