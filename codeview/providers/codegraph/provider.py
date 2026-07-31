from __future__ import annotations

from pathlib import Path
from typing import Iterable

from codeview.models import Relation, RelationKind, SourceSnippet, Symbol
from codeview.providers.base import GraphProvider


class CodegraphProvider(GraphProvider):
    """Placeholder for Codegraph (https://github.com/sourcegraph/codegraph and similar).

    Codeview intentionally does not re-implement graph construction. Wire a Codegraph
    export/reader here when you have an index to consume.
    """

    name = "codegraph"
    languages = ("*",)
    owns_indexing = False

    def index(self, root: Path) -> Iterable[Symbol]:
        raise NotImplementedError(
            "Codegraph provider is a stub. Export or point at a Codegraph index, "
            "then implement this adapter — Codeview owns the exploration UI only."
        )

    def structural_relations(self, root: Path, symbols: list[Symbol]) -> Iterable[Relation]:
        return []

    def expand(
        self,
        root: Path,
        symbol: Symbol,
        kind: RelationKind,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        return []

    def source_for(self, root: Path, symbol: Symbol, context_lines: int = 12) -> SourceSnippet:
        return SourceSnippet(path=symbol.location.path, start_line=1, end_line=1, text="", highlight_line=1)
