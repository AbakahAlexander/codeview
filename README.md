# Codeview

Local-first code explorer. Point it at a project, search a symbol, expand callers and callees.

## Install

```bash
pipx install 'git+https://github.com/AbakahAlexander/codeview.git'
```

Requires Python 3.10+ and `git`.

## Use

```bash
codeview serve .
# or for public repos you don't want to install locally:
codeview serve https://github.com/OWNER/REPO
```

Open http://127.0.0.1:8765 — search, expand with `+`, click through source.

## License

MIT
