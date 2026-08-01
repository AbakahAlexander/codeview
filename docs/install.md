# Install

Requires **Python 3.10+** and **git**. Indexers for some languages also need **Node.js** / `npm` (JS/TS), a **JDK** (Java), or a C/C++ toolchain (`compile_commands.json` / CMake).

On **Windows**, C/C++ indexing also needs **Docker Desktop** (running). Codeview builds a small Linux image with `scip-clang` on first C/C++ open and indexes inside the container. Other languages on Windows do not need Docker. We're working on removing the Docker requirement for C/C++.

Install and start Docker Desktop:

```bash
winget install -e --id Docker.DockerDesktop
docker desktop start
```

Or download the installer from [Docker’s Windows install page](https://docs.docker.com/desktop/setup/install/windows-install/), then run `docker desktop start`.

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
