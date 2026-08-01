# Reference

## SCIP

Codeview does not invent its own language analysis. It runs **[SCIP](https://github.com/sourcegraph/scip)** indexers, ingests the resulting `index.scip`, and serves exploration from a local SQLite graph.

Upstream projects: [SCIP](https://github.com/sourcegraph/scip), [scip-python](https://github.com/sourcegraph/scip-python), [scip-java](https://github.com/sourcegraph/scip-java), [scip-clang](https://github.com/sourcegraph/scip-clang), and related indexers for other languages.

## Languages

Indexed via SCIP when detection finds sources (dominant language first):

| Language | Notes |
|----------|--------|
| Python | Strong; scripts / `__main__` entry points |
| Java / Kotlin / Scala | Gradle/Maven + SemanticDB; `scip-java` fetched if needed |
| C / C++ / CUDA | Needs `compile_commands.json` (or CMake synth); `scip-clang` fetched on Linux/macOS. **Windows:** Docker Desktop required (`winget install -e --id Docker.DockerDesktop`) — same Linux indexer runs in a local container (first run builds image `codeview-scip-clang`). |
| JS / TS | `scip-typescript` via npm when sources/`package.json` dominate |
| Go, Rust, Ruby, C# | Supported when project layout matches the indexer |

Mixed repos pick the **dominant** language by file count (e.g. Iceberg → Java, not two generated `.py` files).

Git URL peeks use your local `git` credentials, so **private** GitHub repos work when you can already clone them.

## Entry points

Detection is split by ecosystem (`codeview/entrypoints/`), then mapped onto SCIP symbols:

```text
package/build evidence → EntryPointCandidate → SCIP symbol → EntryPoint
```

| Kind | How Codeview finds them |
|------|-------------------------|
| Python | `[project.scripts]`, `__main__.py`, `if __name__ == "__main__"` |
| JS / TS | HTML module scripts and `package.json` `bin` first; `main`/`module` if nothing stronger; Vite/Next bootstraps (`src/main.tsx`, `app/page.tsx`). `exports` only when no executable root. Thin bootstraps also list imported symbols. |
| CMake | Sources in `add_executable(...)` → `main` |
| Native/JVM apps | Indexed `main` / `WinMain` |
| JVM libraries | Public types under `api/src/main/java`, … |
| Monorepos | Also scans `packages/*`, `apps/*`, `services/*` |

Each resolved entry has a **category**, **confidence**, and **evidence** (see `EntryPoint` in `models.py`). If nothing matches, the tree falls back to searchable symbols.

## Data layout

All under `~/.codeview/`:

| Path | Purpose |
|------|---------|
| `indexes/` | SQLite graphs |
| `scip/` | Per-project SCIP artifacts |
| `bin/` | Downloaded indexers (`scip-java`, `scip-clang`, …) |
| `tools/` | Helper tool installs (e.g. npm-based) |
| `repos/` | Ephemeral clones for git URL peeks |

Indexer binaries are shared across projects (~100s of MB once you use Java and/or C++). They are **not** removed when a git peek exits. They **are** removed by `codeview doctor --purge`.

## License

MIT — [Alexander Abakah](https://github.com/AbakahAlexander)
