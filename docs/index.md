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

Or peek a public repo (clone + index are removed when the server exits):

```bash
codeview serve https://github.com/OWNER/REPO
```

## What you get

- Tree rooted at **entry points**, not a file dump
- Callers / callees with **call site** and **definition** side by side
- Precise indexes via SCIP (downloaded on demand per language)
- Data under `~/.codeview/` — yours alone

## Docs vs install

This documentation is for the website and the git repo. Installing Codeview with `pipx` does **not** install MkDocs or ship these pages.
