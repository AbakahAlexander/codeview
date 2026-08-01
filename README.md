# Codeview

Local-first code explorer. Point it at a project, search a symbol, expand callers and callees.

No cloud. No AI. No IDE required.

## Install

```bash
pipx install 'git+https://github.com/AbakahAlexander/codeview.git'
```

Requires Python 3.10+, `git`, and Node.js (`npm`) for indexing.

## Use

Always purge before reinstalling so you never hit a stale local index:

```bash
codeview doctor --purge
pipx uninstall codeview
pipx install 'git+https://github.com/AbakahAlexander/codeview.git'
codeview serve .
# or
codeview serve https://github.com/OWNER/REPO
```

Open http://127.0.0.1:8765 — search, expand with `+`, click through source.

The UI starts immediately. Entry points (from packaging scripts and `__main__`) are listed first. If an up-to-date local index already exists, it opens instantly; otherwise Codeview builds one in the background and shows progress.

Indexes are stored under `~/.codeview/`.

## Uninstall

```bash
codeview doctor --purge
pipx uninstall codeview
```

## License

MIT
