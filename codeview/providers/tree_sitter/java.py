from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

from tree_sitter import Language, Node, Parser, Query, QueryCursor
import tree_sitter_java as tsjava

from codeview.models import Location, Relation, RelationKind, SourceSnippet, Symbol, SymbolKind
from codeview.providers.base import GraphProvider

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".gradle",
    "build",
    "out",
    "bin",
    "target",
    "node_modules",
    ".indexes",
    "__pycache__",
}

SYMBOL_QUERY = """
(class_declaration
  name: (identifier) @class.name) @class.def

(interface_declaration
  name: (identifier) @iface.name) @iface.def

(enum_declaration
  name: (identifier) @enum.name) @enum.def

(record_declaration
  name: (identifier) @record.name) @record.def

(method_declaration
  name: (identifier) @method.name) @method.def

(constructor_declaration
  name: (identifier) @ctor.name) @ctor.def
"""

CALL_QUERY = """
(method_invocation
  name: (identifier) @call.name)

(object_creation_expression
  type: (type_identifier) @call.ctor)
"""

SUPER_QUERY = """
(superclass (type_identifier) @super.name)
(super_interfaces (type_list (type_identifier) @iface.name))
"""


def _stable_id(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _iter_java_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.java")):
        if any(part in SKIP_DIRS for part in path.parts):
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


class TreeSitterJavaProvider(GraphProvider):
    name = "treesitter-java"
    languages = ("java",)
    precomputes_calls = True

    def __init__(self) -> None:
        self._language = Language(tsjava.language())
        self._parser = Parser(self._language)
        self._symbol_query = Query(self._language, SYMBOL_QUERY)
        self._call_query = Query(self._language, CALL_QUERY)
        self._super_query = Query(self._language, SUPER_QUERY)
        self.pending_symbols: list[Symbol] = []

    def source_globs(self) -> list[str]:
        return ["*.java"]

    def source_extensions(self) -> set[str]:
        return {".java"}

    def parse_file(self, root: Path, rel: str) -> tuple[list[Symbol], list[Relation]]:
        """Parse one Java file (used by multi-provider ensure_file / hybrid browse)."""
        root = root.resolve()
        path = root / rel
        source = _read_bytes(path)
        if not source:
            return [], []
        symbols: list[Symbol] = []
        relations: list[Relation] = []
        tree = self._parse(source)
        module_id = _stable_id("module", rel)
        symbols.append(
            Symbol(
                id=module_id,
                name=path.name,
                kind=SymbolKind.MODULE,
                location=Location(path=rel, line=1, column=0),
                qualname=rel,
                language="java",
                signature="file",
            )
        )
        caps = self._captures(self._symbol_query, tree)
        type_pairs = (
            list(zip(caps.get("class.name", []), caps.get("class.def", [])))
            + list(zip(caps.get("iface.name", []), caps.get("iface.def", [])))
            + list(zip(caps.get("enum.name", []), caps.get("enum.def", [])))
            + list(zip(caps.get("record.name", []), caps.get("record.def", [])))
        )
        type_ranges: list[tuple[Symbol, Node]] = []
        for name_node, def_node in type_pairs:
            name = _text(source, name_node)
            is_iface = name_node in set(caps.get("iface.name", []))
            sym = Symbol(
                id=_stable_id("type", rel, name_node.start_point[0] + 1, name),
                name=name,
                kind=SymbolKind.INTERFACE if is_iface else SymbolKind.CLASS,
                location=_loc(rel, def_node),
                qualname=f"{rel}::{name}",
                language="java",
                signature=("interface " if is_iface else "class ") + name,
                container_id=module_id,
            )
            type_ranges.append((sym, def_node))
            symbols.append(sym)
        for name_node, def_node in zip(
            list(caps.get("method.name", [])) + list(caps.get("ctor.name", [])),
            list(caps.get("method.def", [])) + list(caps.get("ctor.def", [])),
        ):
            name = _text(source, name_node)
            container_id = module_id
            qualname = f"{rel}::{name}"
            for type_sym, type_node in type_ranges:
                if type_node.start_byte <= name_node.start_byte <= type_node.end_byte:
                    container_id = type_sym.id
                    qualname = f"{type_sym.qualname}::{name}"
                    break
            symbols.append(
                Symbol(
                    id=_stable_id("method", rel, name_node.start_point[0] + 1, name_node.start_point[1], name),
                    name=name,
                    kind=SymbolKind.METHOD,
                    location=_loc(rel, def_node),
                    qualname=qualname,
                    language="java",
                    signature=re.sub(r"\s+", " ", _text(source, def_node).split("{", 1)[0].strip())[:160],
                    container_id=container_id,
                )
            )
        for sym in symbols:
            if sym.container_id:
                relations.append(Relation(kind=RelationKind.CONTAINED_IN, from_id=sym.id, to_id=sym.container_id))
                relations.append(Relation(kind=RelationKind.CONTAINS, from_id=sym.container_id, to_id=sym.id))
        return symbols, relations

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
        for path in _iter_java_files(root):
            rel = _rel(root, path)
            source = _read_bytes(path)
            if not source:
                continue
            tree = self._parse(source)
            module_id = _stable_id("module", rel)
            yield Symbol(
                id=module_id,
                name=path.name,
                kind=SymbolKind.MODULE,
                location=Location(path=rel, line=1, column=0),
                qualname=rel,
                language="java",
            )

            caps = self._captures(self._symbol_query, tree)
            type_pairs = (
                list(zip(caps.get("class.name", []), caps.get("class.def", [])))
                + list(zip(caps.get("iface.name", []), caps.get("iface.def", [])))
                + list(zip(caps.get("enum.name", []), caps.get("enum.def", [])))
                + list(zip(caps.get("record.name", []), caps.get("record.def", [])))
            )

            class_ranges: list[tuple[Symbol, Node]] = []
            for name_node, def_node in type_pairs:
                name = _text(source, name_node)
                kind = SymbolKind.INTERFACE if name_node in caps.get("iface.name", []) else SymbolKind.CLASS
                # enum/record treated as class for exploration
                if name_node in caps.get("iface.name", []):
                    kind = SymbolKind.INTERFACE
                else:
                    kind = SymbolKind.CLASS
                sym = Symbol(
                    id=_stable_id(kind.value, rel, name_node.start_point[0] + 1, name_node.start_point[1], name),
                    name=name,
                    kind=kind,
                    location=_loc(rel, name_node),
                    qualname=f"{rel}::{name}",
                    language="java",
                    signature=_text(source, def_node).split("{", 1)[0].strip()[:160],
                    container_id=module_id,
                )
                class_ranges.append((sym, def_node))
                yield sym

            method_pairs = list(zip(caps.get("method.name", []), caps.get("method.def", []))) + list(
                zip(caps.get("ctor.name", []), caps.get("ctor.def", []))
            )
            for name_node, def_node in method_pairs:
                name = _text(source, name_node)
                container_id = module_id
                qualname = f"{rel}::{name}"
                for class_sym, class_node in class_ranges:
                    if class_node.start_byte <= name_node.start_byte <= class_node.end_byte:
                        container_id = class_sym.id
                        qualname = f"{class_sym.qualname}::{name}"
                        break
                sig = re.sub(r"\s+", " ", _text(source, def_node).split("{", 1)[0].strip())[:160]
                yield Symbol(
                    id=_stable_id("method", rel, name_node.start_point[0] + 1, name_node.start_point[1], name),
                    name=name,
                    kind=SymbolKind.METHOD,
                    location=_loc(rel, name_node),
                    qualname=qualname,
                    language="java",
                    signature=sig,
                    container_id=container_id,
                )

    def structural_relations(self, root: Path, symbols: list[Symbol]) -> Iterable[Relation]:
        root = root.resolve()
        by_file: dict[str, list[Symbol]] = {}
        for symbol in symbols:
            by_file.setdefault(symbol.location.path, []).append(symbol)

        classes_by_name: dict[str, list[Symbol]] = {}
        for symbol in symbols:
            if symbol.kind in {SymbolKind.CLASS, SymbolKind.INTERFACE}:
                classes_by_name.setdefault(symbol.name, []).append(symbol)

        for symbol in symbols:
            if symbol.container_id:
                yield Relation(kind=RelationKind.CONTAINED_IN, from_id=symbol.id, to_id=symbol.container_id)
                yield Relation(kind=RelationKind.CONTAINS, from_id=symbol.container_id, to_id=symbol.id)

        for rel_path, file_symbols in by_file.items():
            path = root / rel_path
            source = _read_bytes(path)
            if not source:
                continue
            tree = self._parse(source)
            file_types = {
                s.name: s
                for s in file_symbols
                if s.kind in {SymbolKind.CLASS, SymbolKind.INTERFACE}
            }
            caps = self._captures(self._symbol_query, tree)
            type_pairs = (
                list(zip(caps.get("class.name", []), caps.get("class.def", [])))
                + list(zip(caps.get("iface.name", []), caps.get("iface.def", [])))
                + list(zip(caps.get("enum.name", []), caps.get("enum.def", [])))
                + list(zip(caps.get("record.name", []), caps.get("record.def", [])))
            )
            for name_node, def_node in type_pairs:
                child = file_types.get(_text(source, name_node))
                if not child:
                    continue
                base_caps = self._captures(self._super_query, def_node)
                for key, rel_kind, inv_kind in (
                    ("super.name", RelationKind.PARENT_CLASS, RelationKind.CHILD_CLASS),
                    ("iface.name", RelationKind.IMPLEMENTS, RelationKind.IMPLEMENTED_BY),
                ):
                    for base_node in base_caps.get(key, []):
                        base_name = _text(source, base_node)
                        parents = classes_by_name.get(base_name, [])
                        parent = file_types.get(base_name) or (parents[0] if len(parents) == 1 else None)
                        if not parent:
                            continue
                        yield Relation(
                            kind=rel_kind,
                            from_id=child.id,
                            to_id=parent.id,
                            location=_loc(rel_path, base_node),
                        )
                        yield Relation(
                            kind=inv_kind,
                            from_id=parent.id,
                            to_id=child.id,
                            location=_loc(rel_path, base_node),
                        )

    def index_call_relations(self, root: Path, symbols: list[Symbol]) -> Iterable[Relation]:
        """One-pass call graph so expands are SQLite-only."""
        root = root.resolve()
        methods = [s for s in symbols if s.kind == SymbolKind.METHOD]
        by_file_name: dict[tuple[str, str], list[Symbol]] = {}
        by_name: dict[str, list[Symbol]] = {}
        for m in methods:
            by_file_name.setdefault((m.location.path, m.name), []).append(m)
            by_name.setdefault(m.name, []).append(m)

        for path in _iter_java_files(root):
            rel = _rel(root, path)
            source = _read_bytes(path)
            if not source:
                continue
            tree = self._parse(source)
            caps = self._captures(self._symbol_query, tree)
            method_pairs = list(zip(caps.get("method.name", []), caps.get("method.def", []))) + list(
                zip(caps.get("ctor.name", []), caps.get("ctor.def", []))
            )
            file_methods = {
                (name_node.start_point[0] + 1, _text(source, name_node)): def_node
                for name_node, def_node in method_pairs
            }

            for method in (m for m in methods if m.location.path == rel):
                def_node = file_methods.get((method.location.line, method.name))
                if def_node is None:
                    continue
                call_caps = self._captures(self._call_query, def_node)
                seen_targets: set[str] = set()
                for key in ("call.name", "call.ctor"):
                    for node in call_caps.get(key, []):
                        name = _text(source, node)
                        for target in self._resolve_calls_for_index(name, rel, by_file_name, by_name):
                            if target.id == method.id or target.id in seen_targets:
                                continue
                            seen_targets.add(target.id)
                            loc = _loc(rel, node)
                            yield Relation(
                                kind=RelationKind.CALLS,
                                from_id=method.id,
                                to_id=target.id,
                                location=loc,
                            )
                            yield Relation(
                                kind=RelationKind.CALLED_BY,
                                from_id=target.id,
                                to_id=method.id,
                                location=loc,
                            )

    # Cap how many same-name targets we attach per call site when expanding.
    MAX_NAME_MATCHES = 80

    @staticmethod
    def _resolve_calls_for_index(
        name: str,
        rel_path: str,
        by_file_name: dict[tuple[str, str], list[Symbol]],
        by_name: dict[str, list[Symbol]],
    ) -> list[Symbol]:
        """Index-time resolution: same-file always; otherwise only an unambiguous global name.

        Ambiguous cross-file names are resolved at expand time so learners still see every
        candidate without exploding the SQLite call graph during index.
        """
        same = by_file_name.get((rel_path, name), [])
        if same:
            return same[: TreeSitterJavaProvider.MAX_NAME_MATCHES]
        matches = by_name.get(name, [])
        if len(matches) == 1:
            return matches
        return []

    @staticmethod
    def _resolve_calls_for_expand(
        name: str,
        rel_path: str,
        by_name: dict[str, list[Symbol]],
    ) -> list[Symbol]:
        matches = by_name.get(name, [])
        ordered = [m for m in matches if m.location.path == rel_path]
        ordered.extend(m for m in matches if m.location.path != rel_path)
        return ordered[: TreeSitterJavaProvider.MAX_NAME_MATCHES]

    def expand(
        self,
        root: Path,
        symbol: Symbol,
        kind: RelationKind,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        self.pending_symbols = []
        if kind in {
            RelationKind.PARENT_CLASS,
            RelationKind.CHILD_CLASS,
            RelationKind.CONTAINS,
            RelationKind.CONTAINED_IN,
            RelationKind.OVERRIDES,
            RelationKind.OVERRIDDEN_BY,
            RelationKind.IMPLEMENTS,
            RelationKind.IMPLEMENTED_BY,
        }:
            return []
        if kind == RelationKind.CALLS:
            return self._callees(root, symbol, symbols_by_id)
        if kind == RelationKind.CALLED_BY:
            return self._callers(root, symbol, symbols_by_id)
        if kind in {RelationKind.REFERENCES, RelationKind.REFERENCED_BY}:
            return []
        return []

    def _find_def_node(self, source: bytes, symbol: Symbol) -> Node | None:
        tree = self._parse(source)
        caps = self._captures(self._symbol_query, tree)
        buckets = [
            ("class.name", "class.def"),
            ("iface.name", "iface.def"),
            ("enum.name", "enum.def"),
            ("record.name", "record.def"),
            ("method.name", "method.def"),
            ("ctor.name", "ctor.def"),
        ]
        for name_key, def_key in buckets:
            for name_node, def_node in zip(caps.get(name_key, []), caps.get(def_key, [])):
                if _text(source, name_node) != symbol.name:
                    continue
                if name_node.start_point[0] + 1 == symbol.location.line:
                    return def_node
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

        by_name: dict[str, list[Symbol]] = {}
        for s in symbols_by_id.values():
            if s.kind in {SymbolKind.METHOD, SymbolKind.FUNCTION, SymbolKind.CLASS, SymbolKind.INTERFACE}:
                by_name.setdefault(s.name, []).append(s)

        relations: list[Relation] = []
        seen: set[str] = set()
        call_caps = self._captures(self._call_query, def_node)
        for key in ("call.name", "call.ctor"):
            for node in call_caps.get(key, []):
                name = _text(source, node)
                matches = by_name.get(name, [])
                ordered = self._resolve_calls_for_expand(name, symbol.location.path, by_name)
                if not ordered:
                    # Keep the call visible even when the target is outside the index slice.
                    target = Symbol(
                        id=_stable_id("unresolved", symbol.location.path, name, node.start_point[0]),
                        name=name,
                        kind=SymbolKind.UNKNOWN,
                        location=_loc(symbol.location.path, node),
                        qualname=f"call:{name}",
                        language="java",
                        signature="unresolved call",
                    )
                    self.pending_symbols.append(target)
                    ordered = [target]
                for target in ordered:
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
        # Name-based call scan; prefer unique or same-file matches via enclosing method.
        relations: list[Relation] = []
        seen: set[tuple[str, int]] = set()
        scoped = [
            s
            for s in symbols_by_id.values()
            if s.kind in {SymbolKind.METHOD, SymbolKind.FUNCTION, SymbolKind.CLASS}
        ]

        for path in _iter_java_files(root):
            rel = _rel(root, path)
            source = _read_bytes(path)
            if not source or symbol.name.encode("utf-8") not in source:
                continue
            tree = self._parse(source)
            call_caps = self._captures(self._call_query, tree)
            nodes = list(call_caps.get("call.name", [])) + list(call_caps.get("call.ctor", []))
            for node in nodes:
                if _text(source, node) != symbol.name:
                    continue
                line = node.start_point[0] + 1
                if rel == symbol.location.path and line == symbol.location.line:
                    continue
                caller = self._enclosing(scoped, rel, line)
                if not caller or caller.id == symbol.id:
                    continue
                key = (caller.id, line)
                if key in seen:
                    continue
                seen.add(key)
                relations.append(
                    Relation(
                        kind=RelationKind.CALLED_BY,
                        from_id=symbol.id,
                        to_id=caller.id,
                        location=_loc(rel, node),
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
        end = min(len(lines), symbol.location.line + context_lines)
        def_node = self._find_def_node(source, symbol)
        if def_node is not None:
            start = max(1, min(start, def_node.start_point[0] + 1))
            end = min(len(lines), max(end, def_node.end_point[0] + 1))
        elif symbol.location.end_line:
            end = min(len(lines), max(end, symbol.location.end_line))
        return SourceSnippet(
            path=symbol.location.path,
            start_line=start,
            end_line=end,
            text="\n".join(lines[start - 1 : end]),
            highlight_line=symbol.location.line,
        )
