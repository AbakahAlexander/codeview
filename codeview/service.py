from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from codeview.detect import (
    all_browse_extensions,
    detect_providers,
    language_for_extension,
    provider_name_for_path,
    provider_name_for_symbol_language,
)
from codeview.fsutil import browse, count_source_files, rg_files
from codeview.models import IndexStats, RelationKind, Symbol, SymbolKind
from codeview.providers import get_provider
from codeview.store import SymbolStore


LAZY_KINDS = (
    RelationKind.CALLS,
    RelationKind.CALLED_BY,
    RelationKind.REFERENCES,
    RelationKind.REFERENCED_BY,
)

PRECOMPUTED_CALL_KINDS = {RelationKind.CALLS, RelationKind.CALLED_BY}


def default_db_path(root: Path) -> Path:
    return Path.home() / ".codeview" / "indexes" / f"{_safe_name(root)}.sqlite3"


def _safe_name(root: Path) -> str:
    resolved = str(root.resolve()).strip("/").replace("/", "__")
    return resolved[:180] or "index"


class ExplorerService:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path
        self.store: SymbolStore | None = None
        self.provider_name = "auto"
        self._provider_cache: dict[str, object] = {}

    def open(self, db_path: Path) -> SymbolStore:
        if self.store:
            self.store.close()
        self.db_path = db_path
        self.store = SymbolStore(db_path)
        return self.store

    def ensure_store(self) -> SymbolStore:
        if not self.store:
            raise RuntimeError("No index loaded. Run index first or open an existing database.")
        return self.store

    def _providers_from_meta(self) -> list[str]:
        store = self.ensure_store()
        raw = store.get_meta("providers") or store.get_meta("provider") or self.provider_name
        if raw == "auto":
            return detect_providers(Path(store.get_meta("root") or "."))
        return [p.strip() for p in raw.split(",") if p.strip()]

    def _get_provider(self, name: str):
        if name not in self._provider_cache:
            self._provider_cache[name] = get_provider(name)
        return self._provider_cache[name]

    def provider_for_path(self, rel: str):
        name = provider_name_for_path(rel)
        if name:
            return self._get_provider(name)
        # Fall back to first configured provider that has parse_file.
        for pname in self._providers_from_meta():
            provider = self._get_provider(pname)
            if getattr(provider, "parse_file", None):
                return provider
        return None

    def provider_for_symbol(self, symbol: Symbol):
        name = provider_name_for_symbol_language(symbol.language)
        if not name:
            name = provider_name_for_path(symbol.location.path)
        if name:
            return self._get_provider(name)
        providers = self._providers_from_meta()
        if providers:
            return self._get_provider(providers[0])
        return get_provider("jedi-python")

    def index_path(
        self,
        root: Path,
        *,
        provider_name: str = "auto",
        db_path: Path | None = None,
        scip_path: Path | None = None,
    ) -> IndexStats:
        root = root.resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Not a directory: {root}")

        if scip_path is not None:
            provider_name = "scip"

        if provider_name in {"auto", "", "multi"}:
            provider_names = detect_providers(root)
            if not provider_names:
                raise ValueError(
                    "No supported languages detected and no index.scip found. "
                    "Generate a SCIP index, or pass --provider explicitly."
                )
        else:
            provider_names = [provider_name]

        self.provider_name = ",".join(provider_names)
        target_db = db_path or default_db_path(root)
        store = self.open(target_db)
        store.clear_index()
        started = time.perf_counter()

        providers = [
            get_provider(name, scip_path=scip_path) if name == "scip" else self._get_provider(name)
            for name in provider_names
        ]
        if scip_path is not None:
            self._provider_cache["scip"] = providers[0]
        lazy_only = all(getattr(p, "lazy_index", False) for p in providers)
        languages: list[str] = []
        for p in providers:
            languages.extend(p.languages)

        browse_exts = all_browse_extensions(provider_names)
        file_count = count_source_files(root, browse_exts)

        if lazy_only:
            store.set_meta("index_mode", "lazy")
            store.set_meta("calls_indexed", "0")
            store.set_meta("calls_indexed_langs", "")
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._write_index_meta(
                store,
                root=root,
                provider_names=provider_names,
                languages=languages,
                duration_ms=duration_ms,
                file_count=file_count,
            )
            return IndexStats(
                root=str(root),
                provider=",".join(provider_names),
                language=",".join(dict.fromkeys(languages)),
                symbol_count=0,
                file_count=file_count,
                duration_ms=duration_ms,
            )

        all_symbols: list[Symbol] = []
        all_relations = []
        calls_langs: list[str] = []
        for provider in providers:
            if getattr(provider, "lazy_index", False):
                # Lazy providers contribute browse extensions only at index time.
                continue
            symbols = list(provider.index(root))
            all_symbols.extend(symbols)
            all_relations.extend(list(provider.structural_relations(root, symbols)))
            if getattr(provider, "precomputes_calls", False):
                all_relations.extend(list(provider.index_call_relations(root, symbols)))
                calls_langs.extend(provider.languages)

        if all_symbols:
            store.replace_symbols(all_symbols)
        if all_relations:
            store.add_relations(all_relations, lazy=False)

        has_lazy = any(getattr(p, "lazy_index", False) for p in providers)
        store.set_meta("index_mode", "hybrid" if has_lazy else "eager")
        store.set_meta("calls_indexed", "1" if calls_langs else "0")
        store.set_meta("calls_indexed_langs", ",".join(dict.fromkeys(calls_langs)))
        # External indexes (SCIP) ship references/inheritance with the symbols.
        if any(not getattr(p, "owns_indexing", True) for p in providers) or "scip" in provider_names:
            store.set_meta("edges_ready", "1")
        else:
            store.set_meta("edges_ready", "0")

        duration_ms = int((time.perf_counter() - started) * 1000)
        self._write_index_meta(
            store,
            root=root,
            provider_names=provider_names,
            languages=languages,
            duration_ms=duration_ms,
            file_count=file_count or len({s.location.path for s in all_symbols}),
        )
        return IndexStats(
            root=str(root),
            provider=",".join(provider_names),
            language=",".join(dict.fromkeys(languages)),
            symbol_count=len(all_symbols),
            file_count=file_count or len({s.location.path for s in all_symbols}),
            duration_ms=duration_ms,
        )

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
        providers = self._providers_from_meta()
        extensions = all_browse_extensions(providers)
        children = browse(root, path, extensions)
        # Fix language tags using extension map.
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
        """Parse one file into SQLite if needed; return its top-level symbols."""
        store = self.ensure_store()
        root = Path(store.get_meta("root") or ".")

        def top_level(symbols: list[Symbol]) -> list[Symbol]:
            return [
                s
                for s in symbols
                if s.kind in {SymbolKind.FUNCTION, SymbolKind.CLASS, SymbolKind.INTERFACE, SymbolKind.METHOD}
                and (
                    s.container_id is None
                    or any(m.id == s.container_id and m.kind == SymbolKind.MODULE for m in symbols)
                )
            ]

        existing = top_level(store.symbols_in_path(rel))
        if existing:
            return existing

        provider = self.provider_for_path(rel)
        parse_file = getattr(provider, "parse_file", None) if provider else None
        if parse_file is None:
            return []

        symbols, relations = parse_file(root, rel)
        if symbols:
            store.replace_symbols(symbols)
            store.add_relations(relations, lazy=False)
        return top_level(symbols)

    def search_lazy(self, query: str, limit: int = 40) -> list[Symbol]:
        store = self.ensure_store()
        root = Path(store.get_meta("root") or ".")
        globs: list[str] = []
        for name in self._providers_from_meta():
            globs.extend(self._get_provider(name).source_globs())
        if not globs:
            globs = ["*.*"]
        files = rg_files(root, query, globs, limit=30)
        hits: list[Symbol] = []
        seen: set[str] = set()
        for rel in files:
            parsed = self.ensure_file(rel)
            for sym in parsed:
                if query.lower() not in sym.name.lower() and query.lower() not in sym.qualname.lower():
                    continue
                if sym.id in seen:
                    continue
                seen.add(sym.id)
                hits.append(sym)
                if len(hits) >= limit:
                    return hits
        for sym in store.search(query, limit=limit):
            if sym.id in seen:
                continue
            if sym.kind in {SymbolKind.DIRECTORY, SymbolKind.MODULE} and sym.signature in {"directory", "file"}:
                continue
            hits.append(sym)
            if len(hits) >= limit:
                break
        return hits

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
                self.ensure_file(symbol.location.path)
                store.mark_expanded(symbol_id, relation_kind)
                return store.relations_enriched(symbol_id, relation_kind, limit=limit)

            precomputed_langs = {
                x for x in (store.get_meta("calls_indexed_langs") or "").split(",") if x
            }
            # External indexes already materialised edges (SCIP references, etc.).
            if store.get_meta("edges_ready") == "1" and relation_kind in LAZY_KINDS:
                store.mark_expanded(symbol_id, relation_kind)
                return store.relations_enriched(symbol_id, relation_kind, limit=limit)

            # Use precomputed call graph when available — keep expands instant.
            if (
                relation_kind in PRECOMPUTED_CALL_KINDS
                and symbol.language in precomputed_langs
                and store.get_meta("calls_indexed") == "1"
            ):
                store.mark_expanded(symbol_id, relation_kind)
                return store.relations_enriched(symbol_id, relation_kind, limit=limit)

            if relation_kind not in LAZY_KINDS:
                store.mark_expanded(symbol_id, relation_kind)
                return store.relations_enriched(symbol_id, relation_kind, limit=limit)

            if store.is_expanded(symbol_id, relation_kind):
                enriched, total = store.relations_enriched(symbol_id, relation_kind, limit=limit)
                if total > 0 or relation_kind not in PRECOMPUTED_CALL_KINDS:
                    return enriched, total
                store._conn.execute(
                    "DELETE FROM expand_cache WHERE symbol_id = ? AND kind = ?",
                    (symbol_id, relation_kind.value),
                )
                store._conn.commit()

            root = Path(store.get_meta("root") or ".")
            provider = self.provider_for_symbol(symbol)
            symbols_by_id = {s.id: s for s in store.search(symbol.name, limit=500)}
            for s in store.symbols_in_path(symbol.location.path):
                symbols_by_id[s.id] = s
            symbols_by_id[symbol.id] = symbol

            relations = provider.expand(root, symbol, relation_kind, symbols_by_id)
            pending = getattr(provider, "pending_symbols", None) or []
            if pending:
                store.replace_symbols(pending)
            store.add_relations(relations, lazy=True)
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
