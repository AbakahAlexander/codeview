from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from codeview.detect import (
    all_browse_extensions,
    language_for_extension,
    provider_name_for_path,
    provider_name_for_symbol_language,
)
from codeview.entrypoints import find_dunder_main_files, parse_project_scripts
from codeview.fsutil import browse, count_source_files
from codeview.models import IndexStats, RelationKind, Symbol, SymbolKind
from codeview.paths import indexes_dir, scip_cache_dir
from codeview.providers import get_provider
from codeview.providers.scip import find_scip_index
from codeview.revision import project_revision
from codeview.scip_index import BackgroundIndexer, generate_scip
from codeview.store import SymbolStore


# Bump when SCIP→SQLite mapping / call-edge rules change so stale DBs rebuild.
GRAPH_SCHEMA_VERSION = "3"

LAZY_KINDS = (
    RelationKind.CALLS,
    RelationKind.CALLED_BY,
    RelationKind.REFERENCES,
    RelationKind.REFERENCED_BY,
)


def default_db_path(root: Path) -> Path:
    indexes_dir().mkdir(parents=True, exist_ok=True)
    return indexes_dir() / f"{_safe_name(root)}.sqlite3"


def _safe_name(root: Path) -> str:
    resolved = str(root.resolve()).strip("/").replace("/", "__")
    return resolved[:180] or "index"


class ExplorerService:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path
        self.store: SymbolStore | None = None
        self.provider_name = "scip"
        self._provider_cache: dict[str, object] = {}
        self.indexer = BackgroundIndexer()

    def open(self, db_path: Path) -> SymbolStore:
        if self.store:
            self.store.close()
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = SymbolStore(db_path)
        return self.store

    def ensure_store(self) -> SymbolStore:
        if not self.store:
            raise RuntimeError("No index loaded. Run index first or open an existing database.")
        return self.store

    def index_status(self) -> dict:
        snap = self.indexer.snapshot()
        store = self.store
        if store:
            stats = store.stats()
            snap["has_graph"] = bool(stats.get("symbol_count"))
            snap["symbol_count"] = stats.get("symbol_count", 0)
            snap["root"] = store.get_meta("root")
        return snap

    def prepare_serve(self, root: Path, *, db_path: Path | None = None) -> dict:
        """Open UI-ready store; reuse fresh index or rebuild in the background."""
        root = root.resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Not a directory: {root}")

        target_db = db_path or default_db_path(root)
        store = self.open(target_db)
        rev = project_revision(root)
        stats = store.stats()
        has_graph = int(stats.get("symbol_count") or 0) > 0 and store.get_meta("provider")
        stored_rev = store.get_meta("content_revision")
        stored_schema = store.get_meta("graph_schema")
        schema_ok = stored_schema == GRAPH_SCHEMA_VERSION

        store.set_meta("root", str(root))
        if not store.get_meta("providers"):
            store.set_meta("providers", "scip")
            store.set_meta("provider", "scip")
            store.set_meta("index_mode", "eager")

        if has_graph and stored_rev == rev and schema_ok:
            self.indexer._set(
                status="ready",
                percent=100,
                message="",
                error=None,
                revision=rev,
                has_graph=True,
            )
            return self.index_status()

        # Stale or missing → background rebuild. Keep old graph visible if present.
        self.indexer._set(
            status="indexing",
            percent=0,
            message="Updating index…" if has_graph else "Indexing…",
            error=None,
            revision=stored_rev or rev,
            has_graph=bool(has_graph),
        )

        def job() -> None:
            try:
                def on_progress(pct: int, msg: str) -> None:
                    self.indexer._set(
                        status="indexing",
                        percent=pct,
                        message=msg,
                        has_graph=bool(has_graph),
                        revision=rev,
                    )

                self._build_precise_index(root, target_db, rev, on_progress=on_progress)
                self.indexer._set(
                    status="ready",
                    percent=100,
                    message="",
                    error=None,
                    revision=rev,
                    has_graph=True,
                )
            except Exception as exc:  # noqa: BLE001 — surface to UI
                self.indexer._set(
                    status="error",
                    percent=0,
                    message=str(exc),
                    error=str(exc),
                    has_graph=bool(has_graph),
                    revision=stored_rev or rev,
                )

        self.indexer.start(job)
        return self.index_status()

    def _build_precise_index(
        self,
        root: Path,
        db_path: Path,
        rev: str,
        *,
        on_progress=None,
        scip_path: Path | None = None,
    ) -> IndexStats:
        def progress(pct: int, msg: str) -> None:
            if on_progress:
                on_progress(pct, msg)

        progress(2, "Preparing…")
        cached = scip_cache_dir(root) / "index.scip"
        # Prefer Codeview's cache over a leftover index.scip in the project root
        # (scip indexers often write index.scip into cwd; a partial file there
        # would otherwise shadow the good cache and truncate the graph).
        path = scip_path
        if path is None and cached.is_file() and (
            self._cache_matches(root, rev) or cached.stat().st_size > 0
        ):
            path = cached
        if path is None:
            path = find_scip_index(root)
        if path is None:
            progress(8, "Building index…")
            path = generate_scip(root, out_path=cached, on_progress=on_progress)
        else:
            progress(80, "Loading index…")

        progress(85, "Importing…")
        # Build into a side DB so the live UI can keep serving the old graph.
        building = Path(str(db_path) + ".building")
        if building.exists():
            building.unlink()
        stats = self.index_path(
            root,
            provider_name="scip",
            db_path=building,
            scip_path=path,
            content_revision=rev,
            attach=False,
        )
        marker = scip_cache_dir(root) / "revision.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(rev, encoding="utf-8")
        # Swap into place.
        if self.store and self.db_path and Path(self.db_path).resolve() == db_path.resolve():
            self.store.close()
            self.store = None
        if db_path.exists():
            db_path.unlink()
        building.replace(db_path)
        self.open(db_path)
        progress(100, "Done")
        return stats

    @staticmethod
    def _cache_matches(root: Path, rev: str) -> bool:
        marker = scip_cache_dir(root) / "revision.txt"
        try:
            return marker.read_text(encoding="utf-8").strip() == rev
        except OSError:
            return False

    def _providers_from_meta(self) -> list[str]:
        store = self.ensure_store()
        raw = store.get_meta("providers") or store.get_meta("provider") or "scip"
        if raw in {"auto", "multi", ""}:
            return ["scip"]
        return [p.strip() for p in raw.split(",") if p.strip()] or ["scip"]

    def _get_provider(self, name: str):
        if name not in self._provider_cache:
            self._provider_cache[name] = get_provider(name)
        return self._provider_cache[name]

    def provider_for_path(self, rel: str):
        name = provider_name_for_path(rel) or "scip"
        return self._get_provider(name)

    def provider_for_symbol(self, symbol: Symbol):
        name = provider_name_for_symbol_language(symbol.language) or "scip"
        return self._get_provider(name)

    def index_path(
        self,
        root: Path,
        *,
        provider_name: str = "auto",
        db_path: Path | None = None,
        scip_path: Path | None = None,
        content_revision: str | None = None,
        on_progress=None,
        attach: bool = True,
    ) -> IndexStats:
        root = root.resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Not a directory: {root}")

        # Universal path: precise index only (generate if needed).
        if provider_name in {"auto", "", "multi", "scip"} or scip_path is not None:
            rev = content_revision or project_revision(root)
            cached = scip_cache_dir(root) / "index.scip"
            if scip_path is None:
                if cached.is_file() and self._cache_matches(root, rev):
                    scip_path = cached
                else:
                    found = find_scip_index(root)
                    # Prefer our cache over a smaller leftover index.scip in the repo.
                    if (
                        found is not None
                        and cached.is_file()
                        and cached.stat().st_size > found.stat().st_size
                    ):
                        scip_path = cached
                    else:
                        scip_path = found
            if scip_path is None:
                scip_path = generate_scip(root, out_path=cached, on_progress=on_progress)
                (scip_cache_dir(root) / "revision.txt").write_text(rev, encoding="utf-8")
            provider_names = ["scip"]
        else:
            provider_names = [provider_name]
            rev = content_revision or project_revision(root)

        self.provider_name = ",".join(provider_names)
        target_db = db_path or default_db_path(root)
        if attach:
            store = self.open(target_db)
        else:
            target_db.parent.mkdir(parents=True, exist_ok=True)
            store = SymbolStore(target_db)
        store.clear_index()
        started = time.perf_counter()

        providers = [
            get_provider(name, scip_path=scip_path) if name == "scip" else self._get_provider(name)
            for name in provider_names
        ]
        if attach and scip_path is not None:
            self._provider_cache["scip"] = providers[0]

        languages: list[str] = []
        for p in providers:
            languages.extend(p.languages)

        browse_exts = all_browse_extensions(provider_names)
        file_count = count_source_files(root, browse_exts)

        all_symbols: list[Symbol] = []
        all_relations = []
        for provider in providers:
            symbols = list(provider.index(root))
            all_symbols.extend(symbols)
            all_relations.extend(list(provider.structural_relations(root, symbols)))

        if all_symbols:
            store.replace_symbols(all_symbols)
        if all_relations:
            store.add_relations(all_relations, lazy=False)

        store.set_meta("index_mode", "eager")
        store.set_meta("calls_indexed", "1")
        store.set_meta("calls_indexed_langs", "*")
        store.set_meta("edges_ready", "1")
        store.set_meta("content_revision", rev)
        store.set_meta("graph_schema", GRAPH_SCHEMA_VERSION)

        duration_ms = int((time.perf_counter() - started) * 1000)
        self._write_index_meta(
            store,
            root=root,
            provider_names=provider_names,
            languages=languages,
            duration_ms=duration_ms,
            file_count=file_count or len({s.location.path for s in all_symbols}),
        )
        stats = IndexStats(
            root=str(root),
            provider=",".join(provider_names),
            language=",".join(dict.fromkeys(languages)),
            symbol_count=len(all_symbols),
            file_count=file_count or len({s.location.path for s in all_symbols}),
            duration_ms=duration_ms,
        )
        if not attach:
            store.close()
        return stats

    @staticmethod
    def _write_index_meta(
        store: SymbolStore,
        *,
        root: Path,
        provider_names: list[str],
        languages: list[str],
        duration_ms: int,
        file_count: int,
    ) -> None:
        store.set_meta("root", str(root))
        store.set_meta("provider", ",".join(provider_names))
        store.set_meta("providers", ",".join(provider_names))
        store.set_meta("language", ",".join(dict.fromkeys(languages)))
        store.set_meta("indexed_at", datetime.now(timezone.utc).isoformat())
        store.set_meta("duration_ms", str(duration_ms))
        store.set_meta("file_count", str(file_count))

    def entry_points(self, *, limit: int = 40) -> list[Symbol]:
        """Resolve packaging / ``__main__`` entry points to indexed symbols."""
        store = self.ensure_store()
        root = Path(store.get_meta("root") or ".")
        found: list[Symbol] = []
        seen: set[str] = set()

        def add(sym: Symbol | None) -> None:
            if not sym or sym.id in seen:
                return
            seen.add(sym.id)
            found.append(sym)

        for _cmd, module, func_name in parse_project_scripts(root):
            # module path: codeview.cli → codeview/cli.py
            rel = module.replace(".", "/") + ".py"
            hits = [
                s
                for s in store.search(func_name, limit=30)
                if s.name == func_name
                and s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD}
                and (
                    s.location.path == rel
                    or s.location.path.endswith("/" + rel)
                    or s.qualname.replace("`", "").find(module) >= 0
                )
            ]
            if hits:
                add(hits[0])
                continue
            # Fallback: any exact name match preferring the module path.
            loose = [s for s in store.search(func_name, limit=20) if s.name == func_name]
            prefer = [s for s in loose if rel in s.location.path]
            add((prefer or loose or [None])[0])

        for rel in find_dunder_main_files(root):
            # Package ``__main__.py``: keep the file module as a start node.
            if Path(rel).name == "__main__.py":
                modules = [
                    s
                    for s in store.symbols_in_path(rel)
                    if s.kind == SymbolKind.MODULE and s.signature == "file"
                ]
                if modules:
                    add(modules[0])
                continue
            # Other files with a main-guard: prefer ``main()`` in that file, and
            # skip the file module when packaging scripts already listed it.
            mains = [
                s
                for s in store.symbols_in_path(rel)
                if s.name == "main" and s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD}
            ]
            if mains:
                add(mains[0])
                continue
            modules = [
                s
                for s in store.symbols_in_path(rel)
                if s.kind == SymbolKind.MODULE and s.signature == "file"
            ]
            if modules:
                add(modules[0])

        return found[:limit]

    def browse(self, path: str = "") -> list[Symbol]:
        store = self.ensure_store()
        root = Path(store.get_meta("root") or ".")
        extensions = all_browse_extensions(self._providers_from_meta())
        children = browse(root, path, extensions)
        fixed: list[Symbol] = []
        for sym in children:
            if sym.kind == SymbolKind.MODULE and sym.signature == "file":
                lang = language_for_extension(Path(sym.location.path).suffix)
                fixed.append(
                    Symbol(
                        id=sym.id,
                        name=sym.name,
                        kind=sym.kind,
                        location=sym.location,
                        qualname=sym.qualname,
                        language=lang,
                        signature=sym.signature,
                        docstring=sym.docstring,
                        container_id=sym.container_id,
                    )
                )
            else:
                fixed.append(sym)
        return fixed

    def ensure_file(self, rel: str) -> list[Symbol]:
        """No per-file heuristic parse — precise index is the source of truth."""
        store = self.ensure_store()
        return [
            s
            for s in store.symbols_in_path(rel)
            if s.kind in {SymbolKind.FUNCTION, SymbolKind.CLASS, SymbolKind.INTERFACE, SymbolKind.METHOD}
        ]

    def search_lazy(self, query: str, limit: int = 40) -> list[Symbol]:
        store = self.ensure_store()
        return [
            s
            for s in store.search(query, limit=limit)
            if s.kind.value in {"class", "function", "method", "interface"}
        ]

    def expand(self, symbol_id: str, kind: str, *, limit: int = 80) -> tuple[list[dict], int]:
        store = self.ensure_store()
        with store._lock:
            symbol = store.get_symbol(symbol_id)
            if not symbol:
                raise KeyError(f"Unknown symbol: {symbol_id}")

            relation_kind = RelationKind(kind)

            if symbol.kind == SymbolKind.DIRECTORY:
                return [], 0

            if symbol.kind == SymbolKind.MODULE and symbol.signature == "file" and relation_kind == RelationKind.CONTAINS:
                store.mark_expanded(symbol_id, relation_kind)
                return store.relations_enriched(symbol_id, relation_kind, limit=limit)

            # Precise indexes materialise edges at ingest time.
            if store.get_meta("edges_ready") == "1":
                store.mark_expanded(symbol_id, relation_kind)
                return store.relations_enriched(symbol_id, relation_kind, limit=limit)

            if relation_kind not in LAZY_KINDS:
                store.mark_expanded(symbol_id, relation_kind)
                return store.relations_enriched(symbol_id, relation_kind, limit=limit)

            store.mark_expanded(symbol_id, relation_kind)
            return store.relations_enriched(symbol_id, relation_kind, limit=limit)

    def source(
        self,
        symbol_id: str,
        *,
        focus_line: int | None = None,
        focus_path: str | None = None,
        context_lines: int = 12,
    ):
        """Return source around a symbol, or around an explicit call-site line."""
        store = self.ensure_store()
        symbol = store.get_symbol(symbol_id)
        if not symbol:
            raise KeyError(f"Unknown symbol: {symbol_id}")
        root = Path(store.get_meta("root") or ".")
        if focus_line is not None and focus_line > 0:
            rel = focus_path or symbol.location.path
            return self._snippet_at(root, rel, focus_line, context_lines=context_lines)
        provider = self.provider_for_symbol(symbol)
        return provider.source_for(root, symbol, context_lines=context_lines)

    @staticmethod
    def _snippet_at(
        root: Path,
        rel: str,
        line: int,
        *,
        context_lines: int = 12,
    ):
        from codeview.models import SourceSnippet

        path = root / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return SourceSnippet(path=rel, start_line=1, end_line=1, text="", highlight_line=1)
        lines = text.splitlines()
        if not lines:
            return SourceSnippet(path=rel, start_line=1, end_line=1, text="", highlight_line=1)
        focus = min(max(1, line), len(lines))
        start = max(1, focus - context_lines)
        end = min(len(lines), focus + context_lines)
        return SourceSnippet(
            path=rel,
            start_line=start,
            end_line=end,
            text="\n".join(lines[start - 1 : end]),
            highlight_line=focus,
        )

    def upsert_ephemeral(self, symbols: list[Symbol]) -> None:
        store = self.ensure_store()
        store.replace_symbols(symbols)
