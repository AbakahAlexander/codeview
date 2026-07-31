from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

from tree_sitter import Language, Node, Parser, Query, QueryCursor
import tree_sitter_scala as tsscala

from codeview.fsutil import path_is_skipped, rg_call_sites
from codeview.models import Location, Relation, RelationKind, SourceSnippet, Symbol, SymbolKind
from codeview.providers.base import GraphProvider

EXTENSIONS = {".scala", ".sc"}

SYMBOL_QUERY = """
(class_definition
  name: (identifier) @class.name) @class.def

(object_definition
  name: (identifier) @object.name) @object.def

(trait_definition
  name: (identifier) @trait.name) @trait.def

(function_definition
  name: (identifier) @func.name) @func.def
"""

CALL_QUERY = """
(call_expression
  function: (identifier) @call.name)

(call_expression
  function: (field_expression
    field: (identifier) @call.field))
"""


def _stable_id(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _iter_scala_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in EXTENSIONS:
            continue
        if path_is_skipped(path):
            continue
        if path.is_file():
            yield path


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _loc(rel: str, node: Node) -> Location:
    return Location(
        path=rel,
        line=node.start_point[0] + 1,
        column=node.start_point[1],
        end_line=node.end_point[0] + 1,
        end_column=node.end_point[1],
    )


class TreeSitterScalaProvider(GraphProvider):
    """Thin Scala provider: symbols + approximate call edges via AST/rg."""

    name = "treesitter-scala"
    languages = ("scala",)

    def __init__(self) -> None:
        self._language = Language(tsscala.language())
        self._parser = Parser(self._language)
        self._symbol_query = Query(self._language, SYMBOL_QUERY)
        self._call_query = Query(self._language, CALL_QUERY)
        self.pending_symbols: list[Symbol] = []

    def source_globs(self) -> list[str]:
        return ["*.scala", "*.sc"]

    def source_extensions(self) -> set[str]:
        return set(EXTENSIONS)

    def _parse(self, source: bytes) -> Node:
        return self._parser.parse(source).root_node

    def _captures(self, query: Query, root: Node) -> dict[str, list[Node]]:
        caps = QueryCursor(query).captures(root)
        if isinstance(caps, dict):
            return caps
        out: dict[str, list[Node]] = {}
        for node, name in caps:
            out.setdefault(name, []).append(node)
        return out

    def index(self, root: Path) -> Iterable[Symbol]:
        root = root.resolve()
        for path in _iter_scala_files(root):
            rel = _rel(root, path)
            symbols, _ = self.parse_file(root, rel)
            yield from symbols

    def structural_relations(self, root: Path, symbols: list[Symbol]) -> Iterable[Relation]:
        for sym in symbols:
            if sym.container_id:
                yield Relation(kind=RelationKind.CONTAINED_IN, from_id=sym.id, to_id=sym.container_id)
                yield Relation(kind=RelationKind.CONTAINS, from_id=sym.container_id, to_id=sym.id)

    def parse_file(self, root: Path, rel: str) -> tuple[list[Symbol], list[Relation]]:
        root = root.resolve()
        path = root / rel
        source = _read_bytes(path)
        if not source:
            return [], []

        tree = self._parse(source)
        module_id = _stable_id("file", rel)
        symbols: list[Symbol] = [
            Symbol(
                id=module_id,
                name=path.name,
                kind=SymbolKind.MODULE,
                location=Location(path=rel, line=1, column=0),
                qualname=f"file:{rel}",
                language="scala",
                signature="file",
            )
        ]
        relations: list[Relation] = []
        caps = self._captures(self._symbol_query, tree)

        type_pairs = (
            list(zip(caps.get("class.name", []), caps.get("class.def", [])))
            + list(zip(caps.get("object.name", []), caps.get("object.def", [])))
            + list(zip(caps.get("trait.name", []), caps.get("trait.def", [])))
        )
        type_ranges: list[tuple[Symbol, Node]] = []
        for name_node, def_node in type_pairs:
            name = _text(source, name_node)
            kind = SymbolKind.INTERFACE if name_node in caps.get("trait.name", []) else SymbolKind.CLASS
            sym = Symbol(
                id=_stable_id(kind.value, rel, name_node.start_point[0] + 1, name),
                name=name,
                kind=kind,
                location=_loc(rel, def_node),
                qualname=f"{rel}::{name}",
                language="scala",
                signature=f"{kind.value} {name}",
                container_id=module_id,
            )
            type_ranges.append((sym, def_node))
            symbols.append(sym)

        for name_node, def_node in zip(caps.get("func.name", []), caps.get("func.def", [])):
            name = _text(source, name_node)
            container_id = module_id
            qualname = f"{rel}::{name}"
            kind = SymbolKind.FUNCTION
            for type_sym, type_node in type_ranges:
                if type_node.start_byte <= name_node.start_byte <= type_node.end_byte:
                    container_id = type_sym.id
                    qualname = f"{type_sym.qualname}::{name}"
                    kind = SymbolKind.METHOD
                    break
            head = re.sub(r"\s+", " ", _text(source, def_node).split("{", 1)[0].split("=", 1)[0].strip())[:160]
            symbols.append(
                Symbol(
                    id=_stable_id(kind.value, rel, name_node.start_point[0] + 1, name),
                    name=name,
                    kind=kind,
                    location=_loc(rel, def_node),
                    qualname=qualname,
                    language="scala",
                    signature=head,
                    container_id=container_id,
                )
            )

        for sym in symbols:
            if sym.container_id:
                relations.append(Relation(kind=RelationKind.CONTAINED_IN, from_id=sym.id, to_id=sym.container_id))
                relations.append(Relation(kind=RelationKind.CONTAINS, from_id=sym.container_id, to_id=sym.id))
        return symbols, relations

    def expand(
        self,
        root: Path,
        symbol: Symbol,
        kind: RelationKind,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        self.pending_symbols = []
        root = root.resolve()
        if kind == RelationKind.CALLS:
            return self._callees(root, symbol, symbols_by_id)
        if kind == RelationKind.CALLED_BY:
            return self._callers(root, symbol, symbols_by_id)
        return []

    def _find_def_node(self, source: bytes, symbol: Symbol) -> Node | None:
        tree = self._parse(source)
        caps = self._captures(self._symbol_query, tree)
        for key in ("func.name", "class.name", "object.name", "trait.name"):
            for name_node in caps.get(key, []):
                if _text(source, name_node) != symbol.name:
                    continue
                if name_node.start_point[0] + 1 != symbol.location.line and (
                    symbol.location.end_line is None
                    or not (symbol.location.line <= name_node.start_point[0] + 1 <= symbol.location.end_line)
                ):
                    # Allow match by name within symbol span / unique name.
                    continue
                node: Node | None = name_node
                while node is not None:
                    if node.type in {
                        "function_definition",
                        "class_definition",
                        "object_definition",
                        "trait_definition",
                    }:
                        return node
                    node = node.parent
        # Unique name fallback.
        matches = []
        for key in ("func.name", "class.name", "object.name", "trait.name"):
            for name_node in caps.get(key, []):
                if _text(source, name_node) == symbol.name:
                    matches.append(name_node)
        if len(matches) == 1:
            node = matches[0]
            while node is not None:
                if node.type in {
                    "function_definition",
                    "class_definition",
                    "object_definition",
                    "trait_definition",
                }:
                    return node
                node = node.parent
        return None

    def _callees(
        self,
        root: Path,
        symbol: Symbol,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        path = root / symbol.location.path
        source = _read_bytes(path)
        if not source:
            return []
        def_node = self._find_def_node(source, symbol)
        if def_node is None:
            return []
        file_syms, _ = self.parse_file(root, symbol.location.path)
        self.pending_symbols.extend(file_syms)
        by_name: dict[str, list[Symbol]] = {}
        for s in list(symbols_by_id.values()) + file_syms:
            if s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS}:
                by_name.setdefault(s.name, []).append(s)

        relations: list[Relation] = []
        seen: set[str] = set()
        call_caps = self._captures(self._call_query, def_node)
        for key in ("call.name", "call.field"):
            for node in call_caps.get(key, []):
                call_name = _text(source, node)
                matches = by_name.get(call_name, [])
                ordered = [m for m in matches if m.location.path == symbol.location.path]
                ordered.extend(m for m in matches if m.location.path != symbol.location.path)
                if not ordered:
                    ordered = [
                        Symbol(
                            id=_stable_id("unresolved", symbol.location.path, call_name, node.start_point[0]),
                            name=call_name,
                            kind=SymbolKind.UNKNOWN,
                            location=_loc(symbol.location.path, node),
                            qualname=f"call:{call_name}",
                            language="scala",
                            signature="unresolved call",
                        )
                    ]
                    self.pending_symbols.extend(ordered)
                for target in ordered[:80]:
                    if target.id == symbol.id or target.id in seen:
                        continue
                    seen.add(target.id)
                    relations.append(
                        Relation(
                            kind=RelationKind.CALLS,
                            from_id=symbol.id,
                            to_id=target.id,
                            location=_loc(symbol.location.path, node),
                        )
                    )
        return relations

    def _callers(
        self,
        root: Path,
        symbol: Symbol,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        hits = rg_call_sites(root, symbol.name, self.source_globs(), limit=500)
        scoped = [
            s
            for s in symbols_by_id.values()
            if s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS}
        ]
        relations: list[Relation] = []
        seen: set[str] = set()
        for rel, line in hits:
            if rel == symbol.location.path and line == symbol.location.line:
                continue
            file_syms = [s for s in scoped if s.location.path == rel]
            if not file_syms:
                parsed, _ = self.parse_file(root, rel)
                self.pending_symbols.extend(parsed)
                file_syms = [s for s in parsed if s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS}]
                scoped.extend(file_syms)
            caller = self._enclosing(file_syms or scoped, rel, line)
            if not caller or caller.id == symbol.id or caller.id in seen:
                continue
            seen.add(caller.id)
            relations.append(
                Relation(
                    kind=RelationKind.CALLED_BY,
                    from_id=symbol.id,
                    to_id=caller.id,
                    location=Location(path=rel, line=line, column=0),
                )
            )
        return relations

    @staticmethod
    def _enclosing(scoped: list[Symbol], path: str, line: int) -> Symbol | None:
        candidates = [
            s
            for s in scoped
            if s.location.path == path
            and s.location.line <= line
            and (s.location.end_line is None or s.location.end_line >= line)
        ]
        if not candidates:
            earlier = [s for s in scoped if s.location.path == path and s.location.line <= line]
            if not earlier:
                return None
            earlier.sort(key=lambda s: s.location.line, reverse=True)
            return earlier[0]
        candidates.sort(key=lambda s: (s.location.end_line or 10**9) - s.location.line)
        return candidates[0]

    def source_for(self, root: Path, symbol: Symbol, context_lines: int = 12) -> SourceSnippet:
        root = root.resolve()
        path = root / symbol.location.path
        source = _read_bytes(path)
        lines = source.decode("utf-8", errors="replace").splitlines()
        if not lines:
            return SourceSnippet(path=symbol.location.path, start_line=1, end_line=1, text="", highlight_line=1)
        start = max(1, symbol.location.line - context_lines)
        end = min(len(lines), (symbol.location.end_line or symbol.location.line) + context_lines)
        return SourceSnippet(
            path=symbol.location.path,
            start_line=start,
            end_line=end,
            text="\n".join(lines[start - 1 : end]),
            highlight_line=symbol.location.line,
        )
