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


def _stable_id(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _descriptor_tail(scip_symbol: str) -> str:
    if not scip_symbol or scip_symbol.startswith("local "):
        return scip_symbol
    return scip_symbol.rsplit("/", 1)[-1]


def _parse_symbol(scip_symbol: str) -> tuple[str, SymbolKind, bool]:
    """Return (display_name, kind, navigable_in_tree)."""
    if not scip_symbol or scip_symbol.startswith("local "):
        return (scip_symbol or "local"), SymbolKind.UNKNOWN, False

    tail = _descriptor_tail(scip_symbol)

    if re.search(r"\(\)\.\([^)]+\)$", tail):
        name = tail.rsplit(".(", 1)[-1].rstrip(")")
        return name or "param", SymbolKind.PARAMETER, False

    m = re.match(r"^([^#]+)#([^#.]+)\(\)\.$", tail)
    if m:
        return m.group(2), SymbolKind.METHOD, True

    m = re.match(r"^([^#.]+)\(\)\.$", tail)
    if m:
        return m.group(1), SymbolKind.FUNCTION, True

    m = re.match(r"^([^#.]+)#$", tail)
    if m:
        return m.group(1), SymbolKind.CLASS, True

    m = re.match(r"^([^#]+)#([^#.]+)\.$", tail)
    if m:
        return m.group(2), SymbolKind.PROPERTY, False

    m = re.match(r"^([^#.]+)\.$", tail)
    if m:
        return m.group(1), SymbolKind.VARIABLE, False

    m = re.match(r"^([^:]+):$", tail)
    if m:
        return m.group(1), SymbolKind.MODULE, False

    ticks = re.findall(r"`([^`]+)`", scip_symbol)
    if ticks:
        return ticks[-1], SymbolKind.UNKNOWN, False
    return tail or scip_symbol, SymbolKind.UNKNOWN, False


def _enclosing_type_name(scip_symbol: str) -> str | None:
    tail = _descriptor_tail(scip_symbol)
    m = re.match(r"^([^#]+)#", tail)
    return m.group(1) if m else None


def find_scip_index(root: Path, explicit: Path | None = None) -> Path | None:
    if explicit and explicit.is_file():
        return explicit
    for path in (
        root / "index.scip",
        root / ".scip" / "index.scip",
        root / "scip" / "index.scip",
    ):
        if path.is_file():
            return path
    for path in sorted(root.glob("*.scip")):
        if path.is_file():
            return path
    return None


class ScipProvider(GraphProvider):
    """Consume a SCIP index into Codeview's exploration store."""

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
        return set()

    def source_globs(self) -> list[str]:
        return ["*.*"]

    def _load(self, root: Path) -> scip_pb2.Index:
        if self._index is not None:
            return self._index
        path = find_scip_index(root, self.index_path)
        if path is None:
            raise FileNotFoundError(
                "No project index is available yet. Codeview builds one automatically on serve."
            )
        self.index_path = path
        index = scip_pb2.Index()
        index.ParseFromString(path.read_bytes())
        self._index = index
        return index

    def index(self, root: Path) -> Iterable[Symbol]:
        root = root.resolve()
        index = self._load(root)
        self._by_scip.clear()
        self._relations = []
        symbols: list[Symbol] = []
        type_by_file: dict[tuple[str, str], Symbol] = {}
        navigable_ids: set[str] = set()

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

            defined: dict[str, tuple[int, int]] = {}
            for occ in doc.occurrences:
                if occ.symbol_roles & ROLE_DEFINITION:
                    line = (occ.range[0] + 1) if occ.range else 1
                    col = occ.range[1] if len(occ.range) > 1 else 0
                    defined[occ.symbol] = (line, col)

            file_syms: list[tuple[Symbol, bool]] = []
            for info in doc.symbols:
                scip_sym = info.symbol
                display, kind, navigable = _parse_symbol(scip_sym)
                line, col = defined.get(scip_sym, (1, 0))
                docs = list(info.documentation) if info.documentation else []
                signature = None
                for line_doc in docs:
                    if "def " in line_doc or "class " in line_doc:
                        signature = line_doc.strip()[:160]
                        break
                if signature is None and docs:
                    signature = docs[0][:160]

                sym = Symbol(
                    id=_stable_id("scip", scip_sym),
                    name=display,
                    kind=kind,
                    location=Location(path=rel, line=line, column=col),
                    qualname=scip_sym,
                    language=language,
                    signature=signature,
                    docstring="\n".join(docs) if docs else None,
                    container_id=module_id,
                )
                symbols.append(sym)
                file_syms.append((sym, navigable))
                self._by_scip[scip_sym] = sym
                if navigable:
                    navigable_ids.add(sym.id)
                if kind == SymbolKind.CLASS:
                    type_by_file[(rel, display)] = sym

            # Containment: file → class/function; class → method
            for sym, navigable in file_syms:
                parent_type = _enclosing_type_name(sym.qualname)
                parent = type_by_file.get((rel, parent_type)) if parent_type else None
                if sym.kind == SymbolKind.METHOD and parent is not None:
                    sym.container_id = parent.id
                    self._relations.append(
                        Relation(kind=RelationKind.CONTAINED_IN, from_id=sym.id, to_id=parent.id)
                    )
                    self._relations.append(
                        Relation(kind=RelationKind.CONTAINS, from_id=parent.id, to_id=sym.id)
                    )
                elif navigable and sym.kind in {
                    SymbolKind.CLASS,
                    SymbolKind.FUNCTION,
                    SymbolKind.INTERFACE,
                }:
                    self._relations.append(
                        Relation(kind=RelationKind.CONTAINED_IN, from_id=sym.id, to_id=module_id)
                    )
                    self._relations.append(
                        Relation(kind=RelationKind.CONTAINS, from_id=module_id, to_id=sym.id)
                    )

            for info in doc.symbols:
                sym = self._by_scip.get(info.symbol)
                if not sym:
                    continue
                for reln in info.relationships:
                    other = reln.symbol
                    if other not in self._by_scip:
                        od, ok, _nav = _parse_symbol(other)
                        placeholder = Symbol(
                            id=_stable_id("scip", other),
                            name=od,
                            kind=ok,
                            location=Location(path=rel, line=sym.location.line, column=0),
                            qualname=other,
                            language=language,
                            signature="external",
                        )
                        symbols.append(placeholder)
                        self._by_scip[other] = placeholder
                    target = self._by_scip[other]
                    if reln.is_implementation:
                        self._relations.append(
                            Relation(kind=RelationKind.IMPLEMENTS, from_id=sym.id, to_id=target.id)
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

            def_entries: list[tuple[int, Symbol]] = []
            for scip_sym, (dline, _) in defined.items():
                cand = self._by_scip.get(scip_sym)
                if cand and cand.kind in {
                    SymbolKind.FUNCTION,
                    SymbolKind.METHOD,
                    SymbolKind.CLASS,
                }:
                    def_entries.append((dline, cand))
            def_entries.sort(key=lambda x: x[0])

            for occ in doc.occurrences:
                if not occ.symbol or (occ.symbol_roles & ROLE_DEFINITION):
                    continue
                target = self._by_scip.get(occ.symbol)
                if not target:
                    continue
                if target.kind == SymbolKind.PARAMETER:
                    continue
                if target.qualname.startswith("local "):
                    continue

                line = (occ.range[0] + 1) if occ.range else 1
                enclosure = module
                for dline, cand in def_entries:
                    if dline <= line:
                        enclosure = cand
                    else:
                        break

                loc = Location(
                    path=rel,
                    line=line,
                    column=occ.range[1] if len(occ.range) > 1 else 0,
                )
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

                is_import = bool(occ.symbol_roles & ROLE_IMPORT)
                if (
                    not is_import
                    and target.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD}
                    and enclosure.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD}
                    and enclosure.id != target.id
                ):
                    self._relations.append(
                        Relation(
                            kind=RelationKind.CALLED_BY,
                            from_id=target.id,
                            to_id=enclosure.id,
                            location=loc,
                        )
                    )
                    self._relations.append(
                        Relation(
                            kind=RelationKind.CALLS,
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
        return []

    def source_for(self, root: Path, symbol: Symbol, context_lines: int = 12) -> SourceSnippet:
        root = root.resolve()
        path = root / symbol.location.path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return SourceSnippet(
                path=symbol.location.path, start_line=1, end_line=1, text="", highlight_line=1
            )
        lines = text.splitlines()
        if not lines:
            return SourceSnippet(
                path=symbol.location.path, start_line=1, end_line=1, text="", highlight_line=1
            )
        start = max(1, symbol.location.line - context_lines)
        end = min(len(lines), symbol.location.line + context_lines)
        return SourceSnippet(
            path=symbol.location.path,
            start_line=start,
            end_line=end,
            text="\n".join(lines[start - 1 : end]),
            highlight_line=symbol.location.line,
        )
