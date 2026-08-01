"""Exploration providers — Codeview owns the experience, not the indexer.

Each provider adapts an existing indexing technology (SCIP, Tree-sitter, Jedi, …)
into a common exploration API used by the local UI.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable

from codeview.models import Relation, RelationKind, SourceSnippet, Symbol


# UI sections in the Wikipedia-style explorer. Providers may leave some empty.
EXPLORE_SECTIONS: tuple[RelationKind, ...] = (
    RelationKind.CONTAINS,       # members
    RelationKind.CALLED_BY,      # callers
    RelationKind.CALLS,          # callees
    RelationKind.PARENT_CLASS,   # inheritance (extends/implements upward)
    RelationKind.IMPLEMENTED_BY, # implementations / children
    RelationKind.CHILD_CLASS,
    RelationKind.REFERENCES,
    RelationKind.REFERENCED_BY,
)


class GraphProvider(ABC):
    """Adapter over an external or built-in code index.

    Prefer implementing the exploration helpers below. ``expand`` remains the
    single dispatch used by the service layer.
    """

    name: str
    languages: tuple[str, ...]

    # Owns only experience adapters — true indexes come from SCIP/etc. when present.
    owns_indexing: bool = True

    lazy_index: bool = False
    precomputes_calls: bool = False

    @abstractmethod
    def index(self, root: Path) -> Iterable[Symbol]:
        """Yield symbols discovered under ``root`` (or from an external index)."""

    @abstractmethod
    def structural_relations(self, root: Path, symbols: list[Symbol]) -> Iterable[Relation]:
        """Containment / inheritance edges available at index time."""

    @abstractmethod
    def expand(
        self,
        root: Path,
        symbol: Symbol,
        kind: RelationKind,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        """Resolve one relationship kind for a symbol."""

    @abstractmethod
    def source_for(self, root: Path, symbol: Symbol, context_lines: int = 12) -> SourceSnippet:
        """Source around a definition."""

    # --- Exploration-shaped helpers (default through expand) -----------------

    def search_symbols(self, root: Path, query: str, symbols_by_id: dict[str, Symbol], *, limit: int = 40) -> list[Symbol]:
        q = query.lower().strip()
        if not q:
            return []
        hits = [s for s in symbols_by_id.values() if q in s.name.lower() or q in s.qualname.lower()]
        hits.sort(key=lambda s: (0 if s.name.lower() == q else 1, len(s.name), s.name))
        return hits[:limit]

    def get_callers(self, root: Path, symbol: Symbol, symbols_by_id: dict[str, Symbol]) -> list[Relation]:
        return self.expand(root, symbol, RelationKind.CALLED_BY, symbols_by_id)

    def get_callees(self, root: Path, symbol: Symbol, symbols_by_id: dict[str, Symbol]) -> list[Relation]:
        return self.expand(root, symbol, RelationKind.CALLS, symbols_by_id)

    def get_references(self, root: Path, symbol: Symbol, symbols_by_id: dict[str, Symbol]) -> list[Relation]:
        return self.expand(root, symbol, RelationKind.REFERENCED_BY, symbols_by_id)

    def get_inheritance(self, root: Path, symbol: Symbol, symbols_by_id: dict[str, Symbol]) -> list[Relation]:
        return self.expand(root, symbol, RelationKind.PARENT_CLASS, symbols_by_id)

    def get_implementations(self, root: Path, symbol: Symbol, symbols_by_id: dict[str, Symbol]) -> list[Relation]:
        out = self.expand(root, symbol, RelationKind.IMPLEMENTED_BY, symbols_by_id)
        out.extend(self.expand(root, symbol, RelationKind.CHILD_CLASS, symbols_by_id))
        return out

    def supports(self, language: str) -> bool:
        return language.lower() in {lang.lower() for lang in self.languages}

    def index_call_relations(self, root: Path, symbols: list[Symbol]) -> Iterable[Relation]:
        return []

    def source_globs(self) -> list[str]:
        return ["*.*"]

    def source_extensions(self) -> set[str]:
        return set()
