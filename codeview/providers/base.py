from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable

from codeview.models import Relation, RelationKind, SourceSnippet, Symbol


class GraphProvider(ABC):
    """Common interface for code-graph / indexing backends.

    Implementations may wrap SCIP, Tree-sitter tooling, Codegraph, Joern,
    language-specific indexers, or libraries such as Jedi.
    """

    name: str
    languages: tuple[str, ...]

    @abstractmethod
    def index(self, root: Path) -> Iterable[Symbol]:
        """Yield symbols discovered under ``root``."""

    @abstractmethod
    def structural_relations(self, root: Path, symbols: list[Symbol]) -> Iterable[Relation]:
        """Yield cheap structural edges (containment, inheritance) at index time."""

    @abstractmethod
    def expand(
        self,
        root: Path,
        symbol: Symbol,
        kind: RelationKind,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        """Lazily resolve relationship edges for a single symbol."""

    @abstractmethod
    def source_for(self, root: Path, symbol: Symbol, context_lines: int = 12) -> SourceSnippet:
        """Return source around a symbol definition."""

    def supports(self, language: str) -> bool:
        return language.lower() in {lang.lower() for lang in self.languages}
