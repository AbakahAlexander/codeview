from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

from tree_sitter import Language, Node, Parser, Query, QueryCursor
import tree_sitter_cpp as tscpp

from codeview.models import Location, Relation, RelationKind, SourceSnippet, Symbol, SymbolKind
from codeview.providers.base import GraphProvider

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "build",
    "cmake-build-debug",
    "cmake-build-release",
    "__pycache__",
    "node_modules",
    ".indexes",
}

EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".cu", ".cuh"}

SYMBOL_QUERY = """
(class_specifier
  name: (type_identifier) @class.name) @class.def

(struct_specifier
  name: (type_identifier) @struct.name) @struct.def

(function_definition
  declarator: (function_declarator
    declarator: (identifier) @func.name)) @func.def

(function_definition
  declarator: (function_declarator
    declarator: (qualified_identifier
      name: (identifier) @method.name))) @method.def

(function_definition
  declarator: (function_declarator
    declarator: (field_identifier) @method_field.name)) @method_field.def

(declaration
  declarator: (function_declarator
    declarator: (identifier) @decl.name)) @decl.def

(field_declaration
  declarator: (function_declarator
    declarator: (field_identifier) @field_method.name)) @field_method.def
"""

CALL_QUERY = """
(call_expression
  function: (identifier) @call.name)

(call_expression
  function: (field_expression
    field: (field_identifier) @call.field))

(call_expression
  function: (qualified_identifier
    name: (identifier) @call.qual))
"""

BASE_QUERY = """
(base_class_clause
  (type_identifier) @base.name)
"""


def _stable_id(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _iter_cxx_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in EXTENSIONS:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _node_text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _loc(rel: str, node: Node) -> Location:
    return Location(
        path=rel,
        line=node.start_point[0] + 1,
        column=node.start_point[1],
        end_line=node.end_point[0] + 1,
        end_column=node.end_point[1],
    )


class TreeSitterCxxProvider(GraphProvider):
    """C/C++/CUDA symbol provider backed by tree-sitter-cpp."""

    name = "treesitter-cxx"
    languages = ("c", "cpp", "cuda")

    def __init__(self) -> None:
        self._language = Language(tscpp.language())
        self._parser = Parser(self._language)
        self._symbol_query = Query(self._language, SYMBOL_QUERY)
        self._call_query = Query(self._language, CALL_QUERY)
        self._base_query = Query(self._language, BASE_QUERY)

    def _parse(self, source: bytes) -> Node:
        return self._parser.parse(source).root_node

    def _captures(self, query: Query, root: Node) -> dict[str, list[Node]]:
        cursor = QueryCursor(query)
        caps = cursor.captures(root)
        if isinstance(caps, dict):
            return caps
        out: dict[str, list[Node]] = {}
        for node, name in caps:
            out.setdefault(name, []).append(node)
        return out

    def index(self, root: Path) -> Iterable[Symbol]:
        root = root.resolve()
        for path in _iter_cxx_files(root):
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
                language=self._language_for(path),
            )

            caps = self._captures(self._symbol_query, tree)
            class_nodes = list(caps.get("class.name", [])) + list(caps.get("struct.name", []))
            class_defs = list(caps.get("class.def", [])) + list(caps.get("struct.def", []))

            class_ranges: list[tuple[Symbol, Node]] = []
            for name_node, def_node in zip(class_nodes, class_defs):
                name = _node_text(source, name_node)
                symbol = Symbol(
                    id=_stable_id("class", rel, name_node.start_point[0] + 1, name_node.start_point[1], name),
                    name=name,
                    kind=SymbolKind.CLASS,
                    location=_loc(rel, name_node),
                    qualname=f"{rel}::{name}",
                    language=self._language_for(path),
                    signature=f"class {name}",
                    container_id=module_id,
                )
                class_ranges.append((symbol, def_node))
                yield symbol

            emitted_funcs: set[tuple[str, int, int]] = set()

            def emit_function(name_node: Node, def_node: Node, *, method: bool) -> Symbol | None:
                name = _node_text(source, name_node)
                line = name_node.start_point[0] + 1
                col = name_node.start_point[1]
                key = (name, line, col)
                if key in emitted_funcs:
                    return None
                emitted_funcs.add(key)

                container_id = module_id
                qualname = f"{rel}::{name}"
                kind = SymbolKind.METHOD if method else SymbolKind.FUNCTION
                for class_sym, class_node in class_ranges:
                    if class_node.start_byte <= name_node.start_byte <= class_node.end_byte:
                        container_id = class_sym.id
                        qualname = f"{class_sym.qualname}::{name}"
                        kind = SymbolKind.METHOD
                        break

                # Foo::bar outside class body
                if not method and "::" in _node_text(source, def_node.child_by_field_name("declarator") or def_node):
                    kind = SymbolKind.METHOD

                text_head = _node_text(source, def_node).split("{", 1)[0].strip()
                text_head = re.sub(r"\s+", " ", text_head)[:160]
                return Symbol(
                    id=_stable_id(kind.value, rel, line, col, name),
                    name=name,
                    kind=kind,
                    location=_loc(rel, name_node),
                    qualname=qualname,
                    language=self._language_for(path),
                    signature=text_head,
                    container_id=container_id,
                )

            for name_node, def_node in zip(caps.get("func.name", []), caps.get("func.def", [])):
                sym = emit_function(name_node, def_node, method=False)
                if sym:
                    yield sym

            for name_node, def_node in zip(caps.get("method.name", []), caps.get("method.def", [])):
                sym = emit_function(name_node, def_node, method=True)
                if sym:
                    # Attach to class by qualifier prefix when possible.
                    full = _node_text(source, def_node)
                    m = re.search(r"([A-Za-z_]\w*)\s*::\s*" + re.escape(sym.name), full)
                    if m:
                        cls = m.group(1)
                        for class_sym, _ in class_ranges:
                            if class_sym.name == cls:
                                sym.container_id = class_sym.id
                                sym.qualname = f"{class_sym.qualname}::{sym.name}"
                                break
                        else:
                            # Class may be in a header; keep file-local qualname but mark method.
                            sym.qualname = f"{rel}::{cls}::{sym.name}"
                    yield sym

            for name_node, def_node in zip(
                caps.get("method_field.name", []) + caps.get("field_method.name", []),
                caps.get("method_field.def", []) + caps.get("field_method.def", []),
            ):
                sym = emit_function(name_node, def_node, method=True)
                if sym:
                    yield sym

            # Declarations in headers (no body) — useful for navigation.
            for name_node, def_node in zip(caps.get("decl.name", []), caps.get("decl.def", [])):
                # Skip if we already indexed a definition with same name in this file nearby.
                name = _node_text(source, name_node)
                if any(s_name == name for s_name, _, _ in emitted_funcs):
                    continue
                sym = emit_function(name_node, def_node, method=False)
                if sym:
                    yield sym

    @staticmethod
    def _language_for(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in {".cu", ".cuh"}:
            return "cuda"
        if suffix == ".c":
            return "c"
        return "cpp"

    def structural_relations(self, root: Path, symbols: list[Symbol]) -> Iterable[Relation]:
        root = root.resolve()
        by_file: dict[str, list[Symbol]] = {}
        for symbol in symbols:
            by_file.setdefault(symbol.location.path, []).append(symbol)

        classes_by_name: dict[str, list[Symbol]] = {}
        for symbol in symbols:
            if symbol.kind == SymbolKind.CLASS:
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
            file_classes = {s.name: s for s in file_symbols if s.kind == SymbolKind.CLASS}

            # Walk class nodes and attach bases.
            caps = self._captures(self._symbol_query, tree)
            class_name_nodes = list(caps.get("class.name", [])) + list(caps.get("struct.name", []))
            class_def_nodes = list(caps.get("class.def", [])) + list(caps.get("struct.def", []))
            for name_node, def_node in zip(class_name_nodes, class_def_nodes):
                child_name = _node_text(source, name_node)
                child = file_classes.get(child_name)
                if not child:
                    continue
                base_caps = self._captures(self._base_query, def_node)
                for base_node in base_caps.get("base.name", []):
                    base_name = _node_text(source, base_node)
                    parents = classes_by_name.get(base_name, [])
                    parent = parents[0] if len(parents) == 1 else file_classes.get(base_name)
                    if not parent:
                        continue
                    yield Relation(
                        kind=RelationKind.PARENT_CLASS,
                        from_id=child.id,
                        to_id=parent.id,
                        location=_loc(rel_path, base_node),
                    )
                    yield Relation(
                        kind=RelationKind.CHILD_CLASS,
                        from_id=parent.id,
                        to_id=child.id,
                        location=_loc(rel_path, base_node),
                    )

        # Overrides by method name within known parent/child pairs are approximate.
        methods_by_container: dict[str, dict[str, Symbol]] = {}
        for symbol in symbols:
            if symbol.kind == SymbolKind.METHOD and symbol.container_id:
                methods_by_container.setdefault(symbol.container_id, {})[symbol.name] = symbol

        # Build parent map from relations we just conceptually know: re-scan is heavy;
        # approximate using qualnames already linked via container and class names.
        class_ids = {s.id for s in symbols if s.kind == SymbolKind.CLASS}
        # Use contains only; override detection deferred to expand if needed.
        _ = class_ids
        _ = methods_by_container

    def expand(
        self,
        root: Path,
        symbol: Symbol,
        kind: RelationKind,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        root = root.resolve()
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
        if kind in {RelationKind.CALLED_BY, RelationKind.REFERENCES, RelationKind.REFERENCED_BY}:
            return self._references(root, symbol, symbols_by_id, as_called_by=(kind == RelationKind.CALLED_BY))
        return []

    def _find_def_node(self, source: bytes, symbol: Symbol) -> Node | None:
        tree = self._parse(source)
        caps = self._captures(self._symbol_query, tree)
        buckets = [
            ("func.name", "func.def"),
            ("method.name", "method.def"),
            ("method_field.name", "method_field.def"),
            ("field_method.name", "field_method.def"),
            ("class.name", "class.def"),
            ("struct.name", "struct.def"),
            ("decl.name", "decl.def"),
        ]
        for name_key, def_key in buckets:
            for name_node, def_node in zip(caps.get(name_key, []), caps.get(def_key, [])):
                if _node_text(source, name_node) != symbol.name:
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
        call_caps = self._captures(self._call_query, def_node)
        names: list[tuple[str, Node]] = []
        for key in ("call.name", "call.field", "call.qual"):
            for node in call_caps.get(key, []):
                names.append((_node_text(source, node), node))

        by_name: dict[str, list[Symbol]] = {}
        for s in symbols_by_id.values():
            if s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS}:
                by_name.setdefault(s.name, []).append(s)

        relations: list[Relation] = []
        seen: set[str] = set()
        for call_name, node in names:
            matches = by_name.get(call_name, [])
            if not matches:
                continue
            # Prefer same-file, then unique global match.
            same = [m for m in matches if m.location.path == symbol.location.path]
            target = same[0] if same else (matches[0] if len(matches) == 1 else None)
            if not target or target.id == symbol.id or target.id in seen:
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

    def _references(
        self,
        root: Path,
        symbol: Symbol,
        symbols_by_id: dict[str, Symbol],
        *,
        as_called_by: bool,
    ) -> list[Relation]:
        pattern = re.compile(rf"\b{re.escape(symbol.name)}\b")
        relations: list[Relation] = []
        seen: set[tuple[str, int]] = set()

        scoped = [
            s
            for s in symbols_by_id.values()
            if s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS}
        ]

        for path in _iter_cxx_files(root):
            rel = _rel(root, path)
            text = _read_bytes(path).decode("utf-8", errors="replace")
            for idx, line in enumerate(text.splitlines(), start=1):
                if not pattern.search(line):
                    continue
                if rel == symbol.location.path and idx == symbol.location.line:
                    continue
                if as_called_by and f"{symbol.name}(" not in line.replace(" ", ""):
                    # loose call heuristic
                    if not re.search(rf"\b{re.escape(symbol.name)}\s*\(", line):
                        continue
                enclosing = self._enclosing(scoped, rel, idx)
                if not enclosing or enclosing.id == symbol.id:
                    # Still record reference to self-located usage via meta target.
                    to_id = enclosing.id if enclosing else symbol.id
                else:
                    to_id = enclosing.id
                key = (to_id, idx)
                if key in seen:
                    continue
                seen.add(key)
                relations.append(
                    Relation(
                        kind=RelationKind.CALLED_BY if as_called_by else RelationKind.REFERENCES,
                        from_id=symbol.id,
                        to_id=to_id,
                        location=Location(path=rel, line=idx, column=line.find(symbol.name)),
                    )
                )
        return relations

    @staticmethod
    def _enclosing(scoped: list[Symbol], path: str, line: int) -> Symbol | None:
        candidates = [s for s in scoped if s.location.path == path and s.location.line <= line]
        if not candidates:
            return None
        # Prefer tightest end_line window when available.
        def score(s: Symbol) -> tuple[int, int]:
            end = s.location.end_line or 10**9
            if end < line:
                return (10**9, -s.location.line)
            return (end - s.location.line, -s.location.line)

        candidates.sort(key=score)
        for s in candidates:
            end = s.location.end_line
            if end is None or end >= line:
                return s
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

        text = "\n".join(lines[start - 1 : end])
        return SourceSnippet(
            path=symbol.location.path,
            start_line=start,
            end_line=end,
            text=text,
            highlight_line=symbol.location.line,
        )
