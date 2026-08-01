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
from codeview.fsutil import browse, count_source_files
from codeview.models import IndexStats, RelationKind, Symbol, SymbolKind
from codeview.paths import indexes_dir, scip_cache_dir
from codeview.providers import get_provider
from codeview.providers.scip import find_scip_index
from codeview.revision import project_revision
from codeview.scip_index import BackgroundIndexer, generate_scip
from codeview.store import SymbolStore


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

        store.set_meta("root", str(root))
        if not store.get_meta("providers"):
            store.set_meta("providers", "scip")
            store.set_meta("provider", "scip")
            store.set_meta("index_mode", "eager")

        if has_graph and stored_rev == rev:
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
        path = scip_path or find_scip_index(root)
        if path is None and cached.is_file() and self._cache_matches(root, rev):
            path = cached
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
            if scip_path is None:
                scip_path = find_scip_index(root)
            if scip_path is None:
                cached = scip_cache_dir(root) / "index.scip"
                if cached.is_file() and self._cache_matches(root, rev):
                    scip_path = cached
            if scip_path is None:
                cached = scip_cache_dir(root) / "index.scip"
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

    def source(self, symbol_id: str):
        store = self.ensure_store()
        symbol = store.get_symbol(symbol_id)
        if not symbol:
            raise KeyError(f"Unknown symbol: {symbol_id}")
        root = Path(store.get_meta("root") or ".")
        provider = self.provider_for_symbol(symbol)
        return provider.source_for(root, symbol)

    def upsert_ephemeral(self, symbols: list[Symbol]) -> None:
        store = self.ensure_store()
        store.replace_symbols(symbols)
