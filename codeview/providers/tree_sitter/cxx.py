from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

from tree_sitter import Language, Node, Parser, Query, QueryCursor
import tree_sitter_cpp as tscpp

from codeview.fsutil import path_is_skipped, rg_call_sites
from codeview.models import Location, Relation, RelationKind, SourceSnippet, Symbol, SymbolKind
from codeview.providers.base import GraphProvider

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
  declarator: (pointer_declarator
    declarator: (function_declarator
      declarator: (identifier) @func_ptr.name))) @func_ptr.def

(function_definition
  declarator: (pointer_declarator
    declarator: (pointer_declarator
      declarator: (function_declarator
        declarator: (identifier) @func_ptr2.name)))) @func_ptr2.def

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

(declaration
  declarator: (pointer_declarator
    declarator: (function_declarator
      declarator: (identifier) @decl_ptr.name))) @decl_ptr.def

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
    """C/C++/CUDA provider. Lazy by default — parses a file only when needed."""

    name = "treesitter-cxx"
    languages = ("c", "cpp", "cuda")
    lazy_index = True

    def __init__(self) -> None:
        self._language = Language(tscpp.language())
        self._parser = Parser(self._language)
        self._symbol_query = Query(self._language, SYMBOL_QUERY)
        self._call_query = Query(self._language, CALL_QUERY)
        self._base_query = Query(self._language, BASE_QUERY)
        self.pending_symbols: list[Symbol] = []

    def source_globs(self) -> list[str]:
        return ["*.c", "*.h", "*.cc", "*.hh", "*.cpp", "*.cxx", "*.hpp", "*.hxx", "*.cu", "*.cuh"]

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

    @staticmethod
    def _language_for(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in {".cu", ".cuh"}:
            return "cuda"
        if suffix == ".c":
            return "c"
        return "cpp"

    def index(self, root: Path) -> Iterable[Symbol]:
        return []

    def structural_relations(self, root: Path, symbols: list[Symbol]) -> Iterable[Relation]:
        return []

    def parse_file(self, root: Path, rel: str) -> tuple[list[Symbol], list[Relation]]:
        """Parse one source file into symbols + containment edges."""
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
                language=self._language_for(path),
                signature="file",
            )
        ]
        relations: list[Relation] = []
        caps = self._captures(self._symbol_query, tree)

        class_ranges: list[tuple[Symbol, Node]] = []
        for name_node, def_node in zip(
            list(caps.get("class.name", [])) + list(caps.get("struct.name", [])),
            list(caps.get("class.def", [])) + list(caps.get("struct.def", [])),
        ):
            name = _node_text(source, name_node)
            sym = Symbol(
                id=_stable_id("class", rel, name_node.start_point[0] + 1, name_node.start_point[1], name),
                name=name,
                kind=SymbolKind.CLASS,
                location=_loc(rel, name_node),
                qualname=f"{rel}::{name}",
                language=self._language_for(path),
                signature=f"class {name}",
                container_id=module_id,
            )
            class_ranges.append((sym, def_node))
            symbols.append(sym)

        emitted: set[tuple[str, int, int]] = set()

        def emit_function(name_node: Node, def_node: Node, *, method: bool) -> Symbol | None:
            name = _node_text(source, name_node)
            line = name_node.start_point[0] + 1
            col = name_node.start_point[1]
            key = (name, line, col)
            if key in emitted:
                return None
            emitted.add(key)
            container_id = module_id
            qualname = f"{rel}::{name}"
            kind = SymbolKind.METHOD if method else SymbolKind.FUNCTION
            for class_sym, class_node in class_ranges:
                if class_node.start_byte <= name_node.start_byte <= class_node.end_byte:
                    container_id = class_sym.id
                    qualname = f"{class_sym.qualname}::{name}"
                    kind = SymbolKind.METHOD
                    break
            text_head = re.sub(r"\s+", " ", _node_text(source, def_node).split("{", 1)[0].strip())[:160]
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

        pairs = (
            list(zip(caps.get("func.name", []), caps.get("func.def", [])))
            + list(zip(caps.get("func_ptr.name", []), caps.get("func_ptr.def", [])))
            + list(zip(caps.get("func_ptr2.name", []), caps.get("func_ptr2.def", [])))
            + list(zip(caps.get("method.name", []), caps.get("method.def", [])))
            + list(zip(caps.get("method_field.name", []), caps.get("method_field.def", [])))
            + list(zip(caps.get("field_method.name", []), caps.get("field_method.def", [])))
            + list(zip(caps.get("decl.name", []), caps.get("decl.def", [])))
            + list(zip(caps.get("decl_ptr.name", []), caps.get("decl_ptr.def", [])))
        )
        for name_node, def_node in pairs:
            is_method = name_node in set(caps.get("method.name", [])) or name_node in set(
                caps.get("method_field.name", [])
            ) or name_node in set(caps.get("field_method.name", []))
            sym = emit_function(name_node, def_node, method=is_method)
            if sym:
                symbols.append(sym)

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
        if kind in {
            RelationKind.PARENT_CLASS,
            RelationKind.CHILD_CLASS,
            RelationKind.CONTAINS,
            RelationKind.CONTAINED_IN,
            RelationKind.OVERRIDES,
            RelationKind.OVERRIDDEN_BY,
            RelationKind.IMPLEMENTS,
            RelationKind.IMPLEMENTED_BY,
            RelationKind.REFERENCES,
            RelationKind.REFERENCED_BY,
        }:
            return []
        if kind == RelationKind.CALLS:
            return self._callees(root, symbol, symbols_by_id)
        if kind == RelationKind.CALLED_BY:
            return self._callers_rg(root, symbol, symbols_by_id)
        return []

    def _find_def_node(self, source: bytes, symbol: Symbol) -> Node | None:
        tree = self._parse(source)
        caps = self._captures(self._symbol_query, tree)
        name_keys = (
            "func.name",
            "func_ptr.name",
            "func_ptr2.name",
            "method.name",
            "method_field.name",
            "field_method.name",
            "class.name",
            "struct.name",
            "decl.name",
            "decl_ptr.name",
        )
        for name_key in name_keys:
            for name_node in caps.get(name_key, []):
                if _node_text(source, name_node) != symbol.name:
                    continue
                if name_node.start_point[0] + 1 != symbol.location.line:
                    continue
                return self._enclosing_def(name_node)
        # Fallback: unique name match in file.
        matches = []
        for name_key in name_keys:
            for name_node in caps.get(name_key, []):
                if _node_text(source, name_node) == symbol.name:
                    matches.append(name_node)
        if len(matches) == 1:
            return self._enclosing_def(matches[0])
        return None

    @staticmethod
    def _enclosing_def(name_node: Node) -> Node | None:
        node: Node | None = name_node
        while node is not None:
            if node.type in {
                "function_definition",
                "class_specifier",
                "struct_specifier",
                "declaration",
                "field_declaration",
                "method_definition",
            }:
                return node
            node = node.parent
        return name_node

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

        # Prefer symbols from this file (lazy mode often has a sparse global map).
        file_syms, _ = self.parse_file(root, symbol.location.path)
        self.pending_symbols.extend(file_syms)
        by_name: dict[str, list[Symbol]] = {}
        for s in list(symbols_by_id.values()) + file_syms:
            if s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS}:
                by_name.setdefault(s.name, []).append(s)

        call_caps = self._captures(self._call_query, def_node)
        relations: list[Relation] = []
        seen: set[str] = set()
        for key in ("call.name", "call.field", "call.qual"):
            for node in call_caps.get(key, []):
                call_name = _node_text(source, node)
                matches = by_name.get(call_name, [])
                ordered = [m for m in matches if m.location.path == symbol.location.path]
                ordered.extend(m for m in matches if m.location.path != symbol.location.path)
                if not ordered:
                    # Helpers often wrap C macros — surface the call even if unresolved.
                    ordered = [
                        Symbol(
                            id=_stable_id("unresolved", symbol.location.path, call_name, node.start_point[0]),
                            name=call_name,
                            kind=SymbolKind.UNKNOWN,
                            location=_loc(symbol.location.path, node),
                            qualname=f"call:{call_name}",
                            language="c",
                            signature="unresolved call / macro",
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

    def _callers_rg(
        self,
        root: Path,
        symbol: Symbol,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        # C/C++ textual call sites for the exact symbol name.
        hits = rg_call_sites(root, symbol.name, self.source_globs(), limit=500)
        # Linux rust/helpers: bindgen renames rust_helper_FOO → bindings::FOO in Rust.
        if symbol.name.startswith("rust_helper_"):
            stripped = symbol.name[len("rust_helper_") :]
            if stripped:
                hits.extend(rg_call_sites(root, stripped, ["*.rs"], limit=500))

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
            if rel.endswith(".rs"):
                caller = self._enclosing_rust(root, rel, line)
                if caller is None or caller.id == symbol.id or caller.id in seen:
                    continue
                self.pending_symbols.append(caller)
                seen.add(caller.id)
                relations.append(
                    Relation(
                        kind=RelationKind.CALLED_BY,
                        from_id=symbol.id,
                        to_id=caller.id,
                        location=Location(path=rel, line=line, column=0),
                    )
                )
                continue
            # Ensure file symbols exist in scoped list when possible.
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

    def _enclosing_rust(self, root: Path, rel: str, line: int) -> Symbol | None:
        """Best-effort nearest `fn name` above a Rust call site (no rust parser yet)."""
        source = _read_bytes(root / rel)
        if not source:
            return None
        text = source.decode("utf-8", errors="replace")
        lines = text.splitlines()
        fn_re = re.compile(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)"
        )
        best: tuple[str, int] | None = None
        for i, row in enumerate(lines[: max(0, line)], start=1):
            m = fn_re.match(row)
            if m:
                best = (m.group(1), i)
        if not best:
            # Fall back to file-level symbol so the call site is still visible.
            return Symbol(
                id=_stable_id("rustfile", rel),
                name=Path(rel).name,
                kind=SymbolKind.MODULE,
                location=Location(path=rel, line=1, column=0),
                qualname=f"file:{rel}",
                language="rust",
                signature="file",
            )
        name, start = best
        return Symbol(
            id=_stable_id("rustfn", rel, name, start),
            name=name,
            kind=SymbolKind.FUNCTION,
            location=Location(path=rel, line=start, column=0, end_line=line),
            qualname=f"{rel}::{name}",
            language="rust",
            signature="fn",
        )

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
        if symbol.kind == SymbolKind.DIRECTORY:
            return SourceSnippet(path=symbol.location.path, start_line=1, end_line=1, text="(directory)", highlight_line=1)
        source = _read_bytes(path)
        lines = source.decode("utf-8", errors="replace").splitlines()
        if not lines:
            return SourceSnippet(path=symbol.location.path, start_line=1, end_line=1, text="", highlight_line=1)
        if symbol.kind == SymbolKind.MODULE and symbol.signature == "file":
            end = min(len(lines), 40)
            return SourceSnippet(
                path=symbol.location.path,
                start_line=1,
                end_line=end,
                text="\n".join(lines[:end]),
                highlight_line=1,
            )
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
