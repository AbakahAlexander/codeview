# Codeview

Local-first code explorer. Point it at a project, search a symbol, expand callers and callees.

## Install

```bash
pipx install 'git+https://github.com/AbakahAlexander/codeview.git'
```

Requires Python 3.10+, `git`, and Node.js (`npm`) for indexing.

## Use

```bash
codeview serve .
# or for public repos you don't want to install locally:
codeview serve https://github.com/OWNER/REPO
```

Open http://127.0.0.1:8765 — search, expand with `+`, click through source.

The UI starts immediately. If an up-to-date local index already exists, it opens instantly. Otherwise Codeview builds one in the background and shows progress.

Indexes are stored under `~/.codeview/`.

## Uninstall

Purge local indexes/caches, then remove the tool:

```bash
codeview doctor --purge
pipx uninstall codeview
```

## License

MIT
