from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

from codeview.models import Location, Relation, RelationKind, SourceSnippet, Symbol, SymbolKind
from codeview.providers.base import GraphProvider
from codeview.providers.scip import scip_pb2

# SCIP SymbolRole bits (see scip.proto)
ROLE_DEFINITION = 1
ROLE_IMPORT = 2
ROLE_WRITE = 4
ROLE_READ = 8


def _stable_id(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _display_name(scip_symbol: str) -> str:
    # Rough: take last meaningful descriptor piece.
    # Example: .../`Class`#`method`() → method or Class
    parts = re.findall(r"`([^`]+)`", scip_symbol)
    if parts:
        return parts[-1]
    return scip_symbol.split("/")[-1] or scip_symbol


def _kind_from_symbol(scip_symbol: str, display: str) -> SymbolKind:
    s = scip_symbol.lower()
    if "#`" in scip_symbol or "()." in scip_symbol or scip_symbol.endswith("()."):
        return SymbolKind.METHOD
    if "()" in scip_symbol:
        return SymbolKind.FUNCTION
    if any(x in s for x in ("interface", "trait", "protocol")):
        return SymbolKind.INTERFACE
    if display[:1].isupper():
        return SymbolKind.CLASS
    return SymbolKind.UNKNOWN


def find_scip_index(root: Path, explicit: Path | None = None) -> Path | None:
    if explicit and explicit.is_file():
        return explicit
    candidates = [
        root / "index.scip",
        root / ".scip" / "index.scip",
        root / "scip" / "index.scip",
    ]
    for path in candidates:
        if path.is_file():
            return path
    # First *.scip under root (shallow)
    for path in sorted(root.glob("*.scip")):
        if path.is_file():
            return path
    return None


class ScipProvider(GraphProvider):
    """Consume an existing SCIP index — Codeview does not replace scip-java/python/etc."""

    name = "scip"
    languages = ("*",)
    owns_indexing = False
    precomputes_calls = False

    def __init__(self, index_path: Path | None = None) -> None:
        self.index_path = index_path
        self.pending_symbols: list[Symbol] = []
        self._index: scip_pb2.Index | None = None
        self._by_scip: dict[str, Symbol] = {}
        self._relations: list[Relation] = []

    def source_extensions(self) -> set[str]:
        return set()  # browse uses filesystem separately

    def source_globs(self) -> list[str]:
        return ["*.*"]

    def _load(self, root: Path) -> scip_pb2.Index:
        if self._index is not None:
            return self._index
        path = find_scip_index(root, self.index_path)
        if path is None:
            raise FileNotFoundError(
                "No SCIP index found. Generate one with scip-java / scip-python / "
                "scip-typescript / rust-analyzer, then place index.scip in the repo "
                "root (or pass --scip path)."
            )
        self.index_path = path
        data = path.read_bytes()
        index = scip_pb2.Index()
        index.ParseFromString(data)
        self._index = index
        return index

    def index(self, root: Path) -> Iterable[Symbol]:
        root = root.resolve()
        index = self._load(root)
        self._by_scip.clear()
        self._relations = []
        symbols: list[Symbol] = []

        for doc in index.documents:
            rel = doc.relative_path
            language = (doc.language or Path(rel).suffix.lstrip(".") or "text").lower()
            module_id = _stable_id("scip-file", rel)
            module = Symbol(
                id=module_id,
                name=Path(rel).name,
                kind=SymbolKind.MODULE,
                location=Location(path=rel, line=1, column=0),
                qualname=f"file:{rel}",
                language=language,
                signature="file",
            )
            symbols.append(module)
            self._by_scip[f"file:{rel}"] = module

            # Definitions from occurrences
            defined: dict[str, tuple[int, int]] = {}
            for occ in doc.occurrences:
                if occ.symbol_roles & ROLE_DEFINITION:
                    line = (occ.range[0] + 1) if occ.range else 1
                    col = occ.range[1] if len(occ.range) > 1 else 0
                    defined[occ.symbol] = (line, col)

            for info in doc.symbols:
                scip_sym = info.symbol
                display = _display_name(scip_sym)
                line, col = defined.get(scip_sym, (1, 0))
                kind = _kind_from_symbol(scip_sym, display)
                sym = Symbol(
                    id=_stable_id("scip", scip_sym),
                    name=display,
                    kind=kind,
                    location=Location(path=rel, line=line, column=col),
                    qualname=scip_sym,
                    language=language,
                    signature=(info.documentation[0][:160] if info.documentation else None),
                    docstring="\n".join(info.documentation) if info.documentation else None,
                    container_id=module_id,
                )
                symbols.append(sym)
                self._by_scip[scip_sym] = sym
                self._relations.append(
                    Relation(kind=RelationKind.CONTAINED_IN, from_id=sym.id, to_id=module_id)
                )
                self._relations.append(
                    Relation(kind=RelationKind.CONTAINS, from_id=module_id, to_id=sym.id)
                )

                for reln in info.relationships:
                    other = reln.symbol
                    # Ensure placeholder for external symbols
                    if other not in self._by_scip:
                        od = _display_name(other)
                        placeholder = Symbol(
                            id=_stable_id("scip", other),
                            name=od,
                            kind=_kind_from_symbol(other, od),
                            location=Location(path=rel, line=line, column=0),
                            qualname=other,
                            language=language,
                            signature="external",
                        )
                        symbols.append(placeholder)
                        self._by_scip[other] = placeholder
                    target = self._by_scip[other]
                    if reln.is_implementation:
                        self._relations.append(
                            Relation(
                                kind=RelationKind.IMPLEMENTS,
                                from_id=sym.id,
                                to_id=target.id,
                            )
                        )
                        self._relations.append(
                            Relation(
                                kind=RelationKind.IMPLEMENTED_BY,
                                from_id=target.id,
                                to_id=sym.id,
                            )
                        )
                    if reln.is_type_definition or reln.is_definition:
                        self._relations.append(
                            Relation(
                                kind=RelationKind.PARENT_CLASS,
                                from_id=sym.id,
                                to_id=target.id,
                            )
                        )
                        self._relations.append(
                            Relation(
                                kind=RelationKind.CHILD_CLASS,
                                from_id=target.id,
                                to_id=sym.id,
                            )
                        )

            # Reference occurrences → referenced_by / references
            for occ in doc.occurrences:
                if not occ.symbol or (occ.symbol_roles & ROLE_DEFINITION):
                    continue
                target = self._by_scip.get(occ.symbol)
                if not target:
                    continue
                line = (occ.range[0] + 1) if occ.range else 1
                # Enclosing definition in this file: nearest prior definition line
                enclosure = module
                for info in doc.symbols:
                    dline = defined.get(info.symbol, (10**9, 0))[0]
                    if dline <= line:
                        cand = self._by_scip.get(info.symbol)
                        if cand and cand.location.line >= enclosure.location.line:
                            enclosure = cand
                loc = Location(path=rel, line=line, column=occ.range[1] if len(occ.range) > 1 else 0)
                self._relations.append(
                    Relation(
                        kind=RelationKind.REFERENCED_BY,
                        from_id=target.id,
                        to_id=enclosure.id,
                        location=loc,
                    )
                )
                self._relations.append(
                    Relation(
                        kind=RelationKind.REFERENCES,
                        from_id=enclosure.id,
                        to_id=target.id,
                        location=loc,
                    )
                )

        yield from symbols

    def structural_relations(self, root: Path, symbols: list[Symbol]) -> Iterable[Relation]:
        self._load(root)
        yield from self._relations

    def expand(
        self,
        root: Path,
        symbol: Symbol,
        kind: RelationKind,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        # Relations are materialised at index time from SCIP.
        return []

    def source_for(self, root: Path, symbol: Symbol, context_lines: int = 12) -> SourceSnippet:
        root = root.resolve()
        path = root / symbol.location.path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return SourceSnippet(path=symbol.location.path, start_line=1, end_line=1, text="", highlight_line=1)
        lines = text.splitlines()
        if not lines:
            return SourceSnippet(path=symbol.location.path, start_line=1, end_line=1, text="", highlight_line=1)
        start = max(1, symbol.location.line - context_lines)
        end = min(len(lines), symbol.location.line + context_lines)
        return SourceSnippet(
            path=symbol.location.path,
            start_line=start,
            end_line=end,
            text="\n".join(lines[start - 1 : end]),
            highlight_line=symbol.location.line,
        )
