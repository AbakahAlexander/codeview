# Codeview

Local-first code explorer. Point it at a project, open from entry points, expand callers and callees, read source.

No cloud. No AI. No IDE required.

## Quick start

```bash
pipx install 'git+https://github.com/AbakahAlexander/codeview.git'
codeview doctor --purge   # first install / after upgrades
codeview serve .
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

Or peek a git repo (public or private — uses your local `git` credentials). Clone + index are removed when the server exits:

```bash
codeview serve https://github.com/OWNER/REPO
```

## What you get

- Tree rooted at **entry points** (Python, JS/TS, JVM, C/C++, …), not a file dump
- Callers / callees with **call site** and **definition** side by side
- Precise indexes via [SCIP](https://github.com/sourcegraph/scip) (indexers downloaded on demand)
- Data under `~/.codeview/` — yours alone

## Credits

Codeview builds on **[SCIP](https://github.com/sourcegraph/scip)** (Source Code Intelligence Protocol) and language indexers such as [`scip-python`](https://github.com/sourcegraph/scip-python), [`scip-typescript`](https://github.com/sourcegraph/scip-typescript), [`scip-java`](https://github.com/sourcegraph/scip-java), and [`scip-clang`](https://github.com/sourcegraph/scip-clang). Those tools produce the precise symbol graph; Codeview stores and explores it locally.

## Docs vs install

This documentation is for the website and the git repo. Installing Codeview with `pipx` does **not** install MkDocs or ship these pages.
