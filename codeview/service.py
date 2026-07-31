from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from codeview.models import IndexStats, RelationKind
from codeview.providers import get_provider
from codeview.store import SymbolStore


LAZY_KINDS = (
    RelationKind.CALLS,
    RelationKind.CALLED_BY,
    RelationKind.REFERENCES,
    RelationKind.REFERENCED_BY,
)


def default_db_path(root: Path) -> Path:
    return Path.home() / ".codeview" / "indexes" / f"{_safe_name(root)}.sqlite3"


def _safe_name(root: Path) -> str:
    resolved = str(root.resolve()).strip("/").replace("/", "__")
    return resolved[:180] or "index"


class ExplorerService:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path
        self.store: SymbolStore | None = None
        self.provider_name = "jedi-python"

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

    def index_path(
        self,
        root: Path,
        *,
        provider_name: str = "jedi-python",
        db_path: Path | None = None,
    ) -> IndexStats:
        root = root.resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Not a directory: {root}")

        provider = get_provider(provider_name)
        self.provider_name = provider_name
        target_db = db_path or default_db_path(root)
        store = self.open(target_db)
        store.clear_index()

        started = time.perf_counter()
        symbols = list(provider.index(root))
        store.replace_symbols(symbols)
        relations = list(provider.structural_relations(root, symbols))
        store.add_relations(relations, lazy=False)

        duration_ms = int((time.perf_counter() - started) * 1000)
        store.set_meta("root", str(root))
        store.set_meta("provider", provider_name)
        store.set_meta("language", ",".join(provider.languages))
        store.set_meta("indexed_at", datetime.now(timezone.utc).isoformat())
        store.set_meta("duration_ms", str(duration_ms))

        file_count = len({s.location.path for s in symbols})
        return IndexStats(
            root=str(root),
            provider=provider_name,
            language=",".join(provider.languages),
            symbol_count=len(symbols),
            file_count=file_count,
            duration_ms=duration_ms,
        )

    def expand(self, symbol_id: str, kind: str) -> list[dict]:
        store = self.ensure_store()
        symbol = store.get_symbol(symbol_id)
        if not symbol:
            raise KeyError(f"Unknown symbol: {symbol_id}")

        relation_kind = RelationKind(kind)
        if store.is_expanded(symbol_id, relation_kind):
            return store.relations_for(symbol_id, relation_kind)

        if relation_kind in LAZY_KINDS:
            root = Path(store.get_meta("root") or ".")
            provider = get_provider(store.get_meta("provider") or self.provider_name)
            relations = provider.expand(root, symbol, relation_kind, store.symbols_by_id())
            store.add_relations(relations, lazy=True)
            store.mark_expanded(symbol_id, relation_kind)
        else:
            store.mark_expanded(symbol_id, relation_kind)

        return store.relations_for(symbol_id, relation_kind)

    def source(self, symbol_id: str):
        store = self.ensure_store()
        symbol = store.get_symbol(symbol_id)
        if not symbol:
            raise KeyError(f"Unknown symbol: {symbol_id}")
        root = Path(store.get_meta("root") or ".")
        provider = get_provider(store.get_meta("provider") or self.provider_name)
        return provider.source_for(root, symbol)
