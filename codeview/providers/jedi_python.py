from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Iterable

import jedi
from jedi.api.classes import Name

from codeview.models import Location, Relation, RelationKind, SourceSnippet, Symbol, SymbolKind
from codeview.providers.base import GraphProvider

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
    "dist",
    "build",
    ".eggs",
    ".indexes",
}


def _stable_id(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _iter_python_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        yield path


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


class JediPythonProvider(GraphProvider):
    name = "jedi-python"
    languages = ("python",)

    def __init__(self) -> None:
        self._projects: dict[str, jedi.Project] = {}

    def _project(self, root: Path) -> jedi.Project:
        key = str(root.resolve())
        if key not in self._projects:
            self._projects[key] = jedi.Project(path=key)
        return self._projects[key]

    def index(self, root: Path) -> Iterable[Symbol]:
        root = root.resolve()
        project = self._project(root)

        for path in _iter_python_files(root):
            rel = _rel(root, path)
            module_qual = rel[:-3].replace("/", ".")
            if module_qual.endswith(".__init__"):
                module_qual = module_qual[: -len(".__init__")]
            module_id = _stable_id("module", rel)
            yield Symbol(
                id=module_id,
                name=Path(rel).stem,
                kind=SymbolKind.MODULE,
                location=Location(path=rel, line=1, column=0),
                qualname=module_qual or Path(rel).stem,
                language="python",
            )

            source = _read_text(path)
            try:
                tree = ast.parse(source, filename=rel)
            except SyntaxError:
                continue

            script = None
            try:
                script = jedi.Script(code=source, path=str(path), project=project)
            except Exception:
                script = None

            for node in tree.body:
                yield from self._symbols_from_node(
                    node,
                    rel=rel,
                    module_qual=module_qual,
                    module_id=module_id,
                    container_id=module_id,
                    container_qual=module_qual,
                    script=script,
                )

    def _symbols_from_node(
        self,
        node: ast.AST,
        *,
        rel: str,
        module_qual: str,
        module_id: str,
        container_id: str,
        container_qual: str,
        script: jedi.Script | None,
        class_name: str | None = None,
    ) -> Iterable[Symbol]:
        if isinstance(node, ast.ClassDef):
            line = node.lineno
            column = node.col_offset
            symbol_id = _stable_id(SymbolKind.CLASS.value, rel, line, column, node.name)
            qualname = f"{module_qual}.{node.name}" if module_qual else node.name
            signature, docstring = self._enrich(script, node.name, line, column, fallback_doc=ast.get_docstring(node))
            yield Symbol(
                id=symbol_id,
                name=node.name,
                kind=SymbolKind.CLASS,
                location=Location(
                    path=rel,
                    line=line,
                    column=column,
                    end_line=getattr(node, "end_lineno", None),
                ),
                qualname=qualname,
                language="python",
                signature=signature or f"class {node.name}",
                docstring=docstring,
                container_id=module_id,
            )
            for child in node.body:
                yield from self._symbols_from_node(
                    child,
                    rel=rel,
                    module_qual=module_qual,
                    module_id=module_id,
                    container_id=symbol_id,
                    container_qual=qualname,
                    script=script,
                    class_name=node.name,
                )
            return

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            line = node.lineno
            column = node.col_offset
            kind = SymbolKind.METHOD if class_name else SymbolKind.FUNCTION
            symbol_id = _stable_id(kind.value, rel, line, column, node.name)
            qualname = f"{container_qual}.{node.name}" if container_qual else node.name
            signature, docstring = self._enrich(
                script,
                node.name,
                line,
                column,
                fallback_doc=ast.get_docstring(node),
            )
            yield Symbol(
                id=symbol_id,
                name=node.name,
                kind=kind,
                location=Location(
                    path=rel,
                    line=line,
                    column=column,
                    end_line=getattr(node, "end_lineno", None),
                ),
                qualname=qualname,
                language="python",
                signature=signature or f"def {node.name}",
                docstring=docstring,
                container_id=container_id,
            )

    def _enrich(
        self,
        script: jedi.Script | None,
        name: str,
        line: int,
        column: int,
        fallback_doc: str | None = None,
    ) -> tuple[str | None, str | None]:
        if script is None:
            return None, fallback_doc
        try:
            names = script.get_names(all_scopes=True, definitions=True)
        except Exception:
            return None, fallback_doc
        for item in names:
            if item.name != name:
                continue
            if (item.line or 1) != line:
                continue
            try:
                return item.description, (item.docstring(raw=True, fast=True) or fallback_doc)
            except Exception:
                return None, fallback_doc
        return None, fallback_doc

    def structural_relations(self, root: Path, symbols: list[Symbol]) -> Iterable[Relation]:
        root = root.resolve()
        by_file: dict[str, list[Symbol]] = {}
        for symbol in symbols:
            by_file.setdefault(symbol.location.path, []).append(symbol)

        class_by_qual: dict[str, Symbol] = {
            s.qualname: s for s in symbols if s.kind == SymbolKind.CLASS
        }

        for symbol in symbols:
            if symbol.container_id:
                yield Relation(
                    kind=RelationKind.CONTAINED_IN,
                    from_id=symbol.id,
                    to_id=symbol.container_id,
                )
                yield Relation(
                    kind=RelationKind.CONTAINS,
                    from_id=symbol.container_id,
                    to_id=symbol.id,
                )

        for rel_path, file_symbols in by_file.items():
            path = root / rel_path
            if not path.exists():
                continue
            try:
                tree = ast.parse(_read_text(path), filename=rel_path)
            except SyntaxError:
                continue

            classes = {s.name: s for s in file_symbols if s.kind == SymbolKind.CLASS}
            module_qual_prefix = next(
                (s.qualname for s in file_symbols if s.kind == SymbolKind.MODULE),
                "",
            )

            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                child = classes.get(node.name)
                if not child:
                    continue
                for base in node.bases:
                    base_name = self._base_name(base)
                    if not base_name:
                        continue
                    parent = class_by_qual.get(f"{module_qual_prefix}.{base_name}")
                    if parent is None:
                        parent = class_by_qual.get(base_name)
                    if parent is None:
                        # Best-effort match on simple name across the index.
                        matches = [s for s in symbols if s.kind == SymbolKind.CLASS and s.name == base_name]
                        parent = matches[0] if len(matches) == 1 else None
                    if parent is None:
                        continue
                    yield Relation(
                        kind=RelationKind.PARENT_CLASS,
                        from_id=child.id,
                        to_id=parent.id,
                        location=Location(path=rel_path, line=node.lineno, column=node.col_offset),
                    )
                    yield Relation(
                        kind=RelationKind.CHILD_CLASS,
                        from_id=parent.id,
                        to_id=child.id,
                        location=Location(path=rel_path, line=node.lineno, column=node.col_offset),
                    )

            # Method overrides: same method name as in a known parent class.
            methods = [s for s in file_symbols if s.kind == SymbolKind.METHOD]
            parents_by_child: dict[str, list[str]] = {}
            # Filled after we have parent relations from this pass; handled in store lazily too.
            _ = methods
            _ = parents_by_child

        # Override edges from already-known parent relations among provided symbols.
        parents: dict[str, list[str]] = {}
        # Recompute quickly from class hierarchy among symbols list via AST already emitted;
        # callers persist relations; override detection uses parent map built below.
        class_symbols = {s.id: s for s in symbols if s.kind == SymbolKind.CLASS}
        method_by_container: dict[str, dict[str, Symbol]] = {}
        for symbol in symbols:
            if symbol.kind == SymbolKind.METHOD and symbol.container_id:
                method_by_container.setdefault(symbol.container_id, {})[symbol.name] = symbol

        # Build parent map by re-walking emitted logic is awkward here; do a second AST pass.
        for rel_path, file_symbols in by_file.items():
            path = root / rel_path
            if not path.exists():
                continue
            try:
                tree = ast.parse(_read_text(path), filename=rel_path)
            except SyntaxError:
                continue
            classes = {s.name: s for s in file_symbols if s.kind == SymbolKind.CLASS}
            module_qual_prefix = next(
                (s.qualname for s in file_symbols if s.kind == SymbolKind.MODULE),
                "",
            )
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                child = classes.get(node.name)
                if not child:
                    continue
                for base in node.bases:
                    base_name = self._base_name(base)
                    if not base_name:
                        continue
                    parent = class_by_qual.get(f"{module_qual_prefix}.{base_name}")
                    if parent is None:
                        parent = class_by_qual.get(base_name)
                    if parent is None:
                        matches = [s for s in symbols if s.kind == SymbolKind.CLASS and s.name == base_name]
                        parent = matches[0] if len(matches) == 1 else None
                    if parent:
                        parents.setdefault(child.id, []).append(parent.id)

        for child_id, parent_ids in parents.items():
            child_methods = method_by_container.get(child_id, {})
            for parent_id in parent_ids:
                if parent_id not in class_symbols:
                    continue
                parent_methods = method_by_container.get(parent_id, {})
                for name, child_method in child_methods.items():
                    parent_method = parent_methods.get(name)
                    if not parent_method:
                        continue
                    yield Relation(
                        kind=RelationKind.OVERRIDES,
                        from_id=child_method.id,
                        to_id=parent_method.id,
                    )
                    yield Relation(
                        kind=RelationKind.OVERRIDDEN_BY,
                        from_id=parent_method.id,
                        to_id=child_method.id,
                    )

    @staticmethod
    def _base_name(base: ast.expr) -> str | None:
        if isinstance(base, ast.Name):
            return base.id
        if isinstance(base, ast.Attribute):
            return base.attr
        return None

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
            # Structural relations are expected to already live in the store.
            return []

        if kind == RelationKind.REFERENCES:
            return self._references(root, symbol, symbols_by_id, as_called_by=False)
        if kind == RelationKind.CALLED_BY:
            return self._references(root, symbol, symbols_by_id, as_called_by=True)
        if kind == RelationKind.CALLS:
            return self._callees(root, symbol, symbols_by_id)
        if kind == RelationKind.REFERENCED_BY:
            return self._references(root, symbol, symbols_by_id, as_called_by=False)
        return []

    def _script(self, root: Path, rel_path: str) -> jedi.Script | None:
        path = root / rel_path
        if not path.exists():
            return None
        try:
            return jedi.Script(path=str(path), project=self._project(root))
        except Exception:
            return None

    def _find_name(self, script: jedi.Script, symbol: Symbol) -> Name | None:
        try:
            names = script.get_names(all_scopes=True, definitions=True)
        except Exception:
            return None
        for name in names:
            if (
                name.name == symbol.name
                and (name.line or 1) == symbol.location.line
                and (name.column or 0) == symbol.location.column
            ):
                return name
        for name in names:
            if name.name == symbol.name and (name.line or 1) == symbol.location.line:
                return name
        return None

    def _match_symbol(
        self,
        root: Path,
        path: str,
        line: int,
        column: int,
        name: str,
        symbols_by_id: dict[str, Symbol],
    ) -> Symbol | None:
        candidates = [
            s
            for s in symbols_by_id.values()
            if s.name == name and s.location.path == path and s.location.line == line
        ]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            exact = [s for s in candidates if s.location.column == column]
            return exact[0] if exact else candidates[0]

        # Fall back to defining scope via goto.
        script = self._script(root, path)
        if not script:
            return None
        try:
            defs = script.goto(line, column, follow_imports=True)
        except Exception:
            return None
        for definition in defs:
            if not definition.module_path:
                continue
            rel = _rel(root, Path(definition.module_path))
            matches = [
                s
                for s in symbols_by_id.values()
                if s.name == definition.name
                and s.location.path == rel
                and s.location.line == (definition.line or 1)
            ]
            if matches:
                return matches[0]
        return None

    def _enclosing_symbol(
        self,
        path: str,
        line: int,
        symbols_by_id: dict[str, Symbol],
    ) -> Symbol | None:
        scoped = [
            s
            for s in symbols_by_id.values()
            if s.location.path == path
            and s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS}
            and s.location.line <= line
            and (s.location.end_line is None or s.location.end_line >= line)
        ]
        if scoped:
            scoped.sort(key=lambda s: (s.location.end_line or 10**9) - s.location.line)
            return scoped[0]

        # Fall back to nearest earlier function/method/class, then module.
        earlier = [
            s
            for s in symbols_by_id.values()
            if s.location.path == path
            and s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS}
            and s.location.line <= line
        ]
        if earlier:
            earlier.sort(key=lambda s: s.location.line, reverse=True)
            return earlier[0]

        modules = [
            s
            for s in symbols_by_id.values()
            if s.location.path == path and s.kind == SymbolKind.MODULE
        ]
        return modules[0] if modules else None

    def _references(
        self,
        root: Path,
        symbol: Symbol,
        symbols_by_id: dict[str, Symbol],
        *,
        as_called_by: bool,
    ) -> list[Relation]:
        # Jedi get_references() often misses cross-module usages in plain scripts.
        # Scan the project with AST (+ optional Jedi goto confirmation) instead.
        if as_called_by:
            return self._find_callers(root, symbol, symbols_by_id)
        return self._find_usages(root, symbol, symbols_by_id)

    def _same_definition(
        self,
        root: Path,
        rel_path: str,
        line: int,
        column: int,
        symbol: Symbol,
    ) -> bool:
        script = self._script(root, rel_path)
        if not script:
            # Fall back to name-only match when Jedi cannot load the file.
            return True
        try:
            defs = script.goto(line, column, follow_imports=True)
        except Exception:
            return True
        if not defs:
            # Imported names sometimes fail goto; keep name-matched call sites.
            return True
        for definition in defs:
            if not definition.module_path:
                continue
            def_rel = _rel(root, Path(definition.module_path))
            if (
                definition.name == symbol.name
                and def_rel == symbol.location.path
                and (definition.line or 1) == symbol.location.line
            ):
                return True
        return False

    def _find_callers(
        self,
        root: Path,
        symbol: Symbol,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        relations: list[Relation] = []
        seen: set[tuple[str, int]] = set()

        for path in _iter_python_files(root):
            rel_path = _rel(root, path)
            source = _read_text(path)
            try:
                tree = ast.parse(source, filename=rel_path)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                call_name, col = self._call_locator(node)
                if call_name != symbol.name:
                    continue
                line = getattr(node, "lineno", None)
                if line is None:
                    continue
                column = col if col is not None else getattr(node, "col_offset", 0)

                # Skip the definition line itself.
                if rel_path == symbol.location.path and line == symbol.location.line:
                    continue

                if not self._same_definition(root, rel_path, line, column, symbol):
                    continue

                caller = self._enclosing_symbol(rel_path, line, symbols_by_id)
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
                        location=Location(path=rel_path, line=line, column=column),
                    )
                )
        return relations

    def _find_usages(
        self,
        root: Path,
        symbol: Symbol,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        relations: list[Relation] = []
        seen: set[tuple[str, int]] = set()
        pattern_name = symbol.name

        for path in _iter_python_files(root):
            rel_path = _rel(root, path)
            source = _read_text(path)
            try:
                tree = ast.parse(source, filename=rel_path)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == pattern_name:
                    line = node.lineno
                    column = node.col_offset
                elif isinstance(node, ast.Attribute) and node.attr == pattern_name:
                    line = node.lineno
                    column = node.col_offset
                else:
                    continue

                if rel_path == symbol.location.path and line == symbol.location.line:
                    continue
                if not self._same_definition(root, rel_path, line, column, symbol):
                    continue

                target = self._enclosing_symbol(rel_path, line, symbols_by_id)
                to_id = target.id if target else symbol.id
                key = (to_id, line)
                if key in seen:
                    continue
                seen.add(key)
                relations.append(
                    Relation(
                        kind=RelationKind.REFERENCES,
                        from_id=symbol.id,
                        to_id=to_id,
                        location=Location(path=rel_path, line=line, column=column),
                        meta={"reference_name": symbol.name},
                    )
                )
        return relations

    def _is_call_site(self, root: Path, rel_path: str, line: int, column: int, name: str) -> bool:
        path = root / rel_path
        if not path.exists():
            return False
        source_line = _read_text(path).splitlines()
        if line < 1 or line > len(source_line):
            return False
        text = source_line[line - 1]
        # Heuristic: name followed by '(' after the reference column.
        snippet = text[column:]
        stripped = snippet.lstrip()
        if not stripped.startswith(name):
            # column may point at start of name
            idx = text.find(name, max(0, column - len(name)))
            if idx < 0:
                return False
            after = text[idx + len(name) :].lstrip()
            return after.startswith("(")
        after = stripped[len(name) :].lstrip()
        return after.startswith("(")

    def _callees(
        self,
        root: Path,
        symbol: Symbol,
        symbols_by_id: dict[str, Symbol],
    ) -> list[Relation]:
        path = root / symbol.location.path
        if not path.exists():
            return []
        source = _read_text(path)
        try:
            tree = ast.parse(source, filename=symbol.location.path)
        except SyntaxError:
            return []

        target_node = self._find_def_node(tree, symbol)
        if target_node is None:
            return []

        script = self._script(root, symbol.location.path)
        if not script:
            return []

        relations: list[Relation] = []
        seen: set[str] = set()
        for node in ast.walk(target_node):
            if not isinstance(node, ast.Call):
                continue
            call_name, col = self._call_locator(node)
            if not call_name:
                continue
            line = getattr(node, "lineno", None)
            if line is None:
                continue
            column = col if col is not None else getattr(node, "col_offset", 0)
            try:
                defs = script.goto(line, column, follow_imports=True)
            except Exception:
                continue
            for definition in defs:
                if definition.type not in {"function", "class"} and definition.name != call_name:
                    continue
                if not definition.module_path:
                    continue
                rel = _rel(root, Path(definition.module_path))
                matched = self._match_symbol(
                    root,
                    rel,
                    definition.line or 1,
                    definition.column or 0,
                    definition.name,
                    symbols_by_id,
                )
                if not matched or matched.id == symbol.id:
                    continue
                if matched.id in seen:
                    continue
                seen.add(matched.id)
                relations.append(
                    Relation(
                        kind=RelationKind.CALLS,
                        from_id=symbol.id,
                        to_id=matched.id,
                        location=Location(
                            path=symbol.location.path,
                            line=line,
                            column=column,
                        ),
                    )
                )
        return relations

    def _find_def_node(self, tree: ast.AST, symbol: Symbol) -> ast.AST | None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol.name and node.lineno == symbol.location.line:
                    return node
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol.name:
                    return node
        return None

    @staticmethod
    def _call_locator(node: ast.Call) -> tuple[str | None, int | None]:
        func = node.func
        if isinstance(func, ast.Name):
            return func.id, func.col_offset
        if isinstance(func, ast.Attribute):
            return func.attr, func.col_offset
        return None, None

    def source_for(self, root: Path, symbol: Symbol, context_lines: int = 12) -> SourceSnippet:
        root = root.resolve()
        path = root / symbol.location.path
        if not path.exists():
            return SourceSnippet(
                path=symbol.location.path,
                start_line=1,
                end_line=1,
                text="",
                highlight_line=symbol.location.line,
            )
        lines = _read_text(path).splitlines()
        start = max(1, symbol.location.line - context_lines)
        # Try to cover the whole definition via AST end_lineno when available.
        end = min(len(lines), symbol.location.line + context_lines)
        try:
            tree = ast.parse("\n".join(lines), filename=symbol.location.path)
            node = self._find_def_node(tree, symbol)
            if node is not None:
                end_lineno = getattr(node, "end_lineno", None) or symbol.location.line
                end = min(len(lines), max(end, end_lineno))
                start = max(1, min(start, node.lineno))
        except SyntaxError:
            pass
        text = "\n".join(lines[start - 1 : end])
        return SourceSnippet(
            path=symbol.location.path,
            start_line=start,
            end_line=end,
            text=text,
            highlight_line=symbol.location.line,
        )
