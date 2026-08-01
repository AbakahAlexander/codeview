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
| C / C++ / CUDA | Needs `compile_commands.json` (or CMake synth); `scip-clang` fetched if needed |
| JS / TS | `scip-typescript` via npm when sources/`package.json` dominate |
| Go, Rust, Ruby, C# | Supported when project layout matches the indexer |

Mixed repos pick the **dominant** language by file count (e.g. Iceberg → Java, not two generated `.py` files).

Git URL peeks use your local `git` credentials, so **private** GitHub repos work when you can already clone them.

## Entry points

| Kind | How Codeview finds them |
|------|-------------------------|
| Python | `[project.scripts]`, `__main__.py`, `if __name__ == "__main__"` → `main()` |
| JS / TS | `index.html` module scripts; `package.json` `bin` / `main` / `module` / `exports`; then Vite/Next-style roots (`src/main.tsx`, `app/page.tsx`, …). Thin bootstraps (e.g. `main.tsx`) also list imported in-project symbols (e.g. `Game`). Expand a file module for **uses** when contains/calls are empty. |
| Apps (C/C++/Java/…) | `main` / `WinMain` (non-test paths preferred) |
| CMake apps | Sources listed in `add_executable(...)` |
| JVM libraries (no `main`) | Public types under paths like `api/src/main/java` |

If nothing matches, the tree falls back to searchable symbols.

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
