# Codeview

Local-first code explorer. Point it at a project, search a symbol, expand callers and callees.

No cloud. No AI. No IDE required.

**Docs:** [abakahalexander.github.io/codeview](https://abakahalexander.github.io/codeview/)

## Install

```bash
pipx install 'git+https://github.com/AbakahAlexander/codeview.git'
```

Requires Python 3.10+, `git`, and a language toolchain for indexing (Node for JS/TS, JDK for Java, … — see docs). Private git URLs work via your local git credentials.

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

The UI starts immediately. Entry points are listed first. If an up-to-date index exists, it opens instantly; otherwise Codeview builds one in the background.

Indexes and tools live under `~/.codeview/`.

## Uninstall

```bash
codeview doctor --purge
pipx uninstall codeview
```

## Docs (contributors)

Docs are MkDocs + Material. Not installed with the CLI.

```bash
pip install -e '.[docs]'
mkdocs serve
```

GitHub Pages deploys from `main` via Actions.

## License

MIT
