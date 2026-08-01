# Install

Requires **Python 3.10+** and **git**. Indexers for some languages also need **Node.js** / `npm` (JS/TS), a **JDK** (Java), or a C/C++ toolchain (`compile_commands.json` / CMake).

## Install

```bash
pipx install 'git+https://github.com/AbakahAlexander/codeview.git'
```

## Upgrade

Always purge local Codeview data before reinstalling so you do not keep a stale index:

```bash
codeview doctor --purge
pipx uninstall codeview
pipx install 'git+https://github.com/AbakahAlexander/codeview.git'
```

## Uninstall

```bash
codeview doctor --purge
pipx uninstall codeview
```

`doctor --purge` deletes everything under `~/.codeview/` — indexes, SCIP caches, and downloaded indexer binaries.
