# Usage

## Serve

```bash
codeview serve .                          # local project
codeview serve https://github.com/OWNER/REPO
codeview serve --host 127.0.0.1 --port 8765 .
```

The UI starts immediately. If an index is missing or outdated, Codeview builds it in the background and shows progress as a percentage.

## Navigate

- Entry points appear first in the tree
- Expand with `+` for members / callers / callees (file modules also show **uses** for imports)
- Click a row to open source
- On call edges: **call site** above, **definition** below

## Index only

```bash
codeview index .
codeview index https://github.com/OWNER/REPO
```

## Search (CLI)

```bash
codeview search SYMBOL --db ~/.codeview/indexes/<name>.sqlite3
```

## Doctor

```bash
codeview doctor              # check tools
codeview doctor --fetch-rg   # fetch ripgrep into ~/.codeview/bin
codeview doctor --purge      # wipe all Codeview data
```

## Git URL peeks

`serve` / `index` on a git URL shallow-clones under `~/.codeview/repos/`. Private GitHub repos work when your machine can already `git clone` them (SSH key, `gh auth`, credential helper, …).

When `serve` exits, that clone and its index/cache are removed. Shared indexer binaries in `~/.codeview/bin/` stay for reuse. Local paths are never deleted on exit.
