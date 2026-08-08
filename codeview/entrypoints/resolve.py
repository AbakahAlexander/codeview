"""Map entry candidates onto SCIP symbols in the store."""

from __future__ import annotations

from pathlib import Path

from codeview.entrypoints.models import EntryPointCandidate
from codeview.models import (
    Confidence,
    EntryPoint,
    EntryPointKind,
    Symbol,
    SymbolKind,
)
from codeview.store import SymbolStore


def resolve_entry_points(
    store: SymbolStore,
    candidates: list[EntryPointCandidate],
    *,
    limit: int = 40,
) -> list[EntryPoint]:
    """Resolve packaging/bootstrap candidates to SCIP symbols.

    Does not invent hubs or filter trees. Callers/callees come from SCIP when
    the user expands a root.
    """
    resolved: list[EntryPoint] = []
    seen_symbols: set[str] = set()

    def add_entry(ep: EntryPoint) -> None:
        if len(resolved) >= limit:
            return
        if ep.symbol_id and ep.symbol_id in seen_symbols:
            return
        if ep.symbol_id:
            seen_symbols.add(ep.symbol_id)
        resolved.append(ep)

    for cand in candidates:
        if cand.category == EntryPointKind.LIBRARY:
            continue

        primary = _resolve_candidate_symbol(store, cand)
        if primary:
            add_entry(_to_entry(cand, primary))
            continue

        add_entry(
            EntryPoint(
                category=cand.category,
                display_name=cand.display_name,
                source=cand.source,
                confidence=cand.confidence,
                symbol_id=None,
                target=cand.path or (f"{cand.module}:{cand.attr}" if cand.module else None),
                evidence=list(cand.evidence),
                symbol=None,
            )
        )

    # Indexed C/C++/JVM mains (per-file). Always merge — packaging scripts alone
    # must not hide ``src/server.c`` / ``main.c`` in Makefile-based projects.
    for sym in _native_main_symbols(store, limit=limit):
        add_entry(
            EntryPoint(
                category=EntryPointKind.NATIVE_MAIN,
                display_name=sym.name,
                source="indexed main()",
                confidence=Confidence.CONFIRMED,
                symbol_id=sym.id,
                target=f"{sym.location.path}:{sym.location.line}",
                evidence=[sym.location.path],
                symbol=sym,
            )
        )

    with_symbols = [ep for ep in resolved if ep.symbol_id and ep.symbol]
    # Prefer native/src mains ahead of incidental script MODULE entries.
    with_symbols.sort(key=_entry_display_rank)
    return (with_symbols or resolved)[:limit]


def _entry_display_rank(ep: EntryPoint) -> tuple:
    path = (
        ep.symbol.location.path.replace("\\", "/").lstrip("./")
        if ep.symbol
        else ""
    )
    name = Path(path).name.lower()
    if (
        ep.category == EntryPointKind.NATIVE_MAIN
        and path.startswith(("src/", "lib/"))
    ):
        # Primary program entries first (server/main), then other src binaries.
        tip = 0 if name in {"server.c", "main.c", "main.cpp", "main.cc"} else 1
        return (0, tip, path)
    if ep.category in {
        EntryPointKind.CLI,
        EntryPointKind.FRONTEND,
        EntryPointKind.NATIVE_MAIN,
    }:
        return (1, 0, path or ep.display_name)
    return (2, 0, path or ep.display_name)


def symbols_from_entries(entries: list[EntryPoint]) -> list[Symbol]:
    return [ep.symbol for ep in entries if ep.symbol is not None]


def _to_entry(cand: EntryPointCandidate, symbol: Symbol) -> EntryPoint:
    return EntryPoint(
        category=cand.category,
        display_name=cand.display_name or symbol.name,
        source=cand.source,
        confidence=cand.confidence,
        symbol_id=symbol.id,
        target=cand.path
        or (f"{cand.module}:{cand.attr}" if cand.module and cand.attr else symbol.location.path),
        evidence=list(cand.evidence),
        symbol=symbol,
    )


def _resolve_candidate_symbol(
    store: SymbolStore, cand: EntryPointCandidate
) -> Symbol | None:
    if cand.module and cand.attr:
        rel = cand.module.replace(".", "/") + ".py"
        kinds = {
            SymbolKind.FUNCTION,
            SymbolKind.METHOD,
            SymbolKind.CLASS,
            SymbolKind.INTERFACE,
        }
        hits = [
            s
            for s in store.search(cand.attr, limit=40)
            if s.name == cand.attr
            and s.kind in kinds
            and s.signature != "external"
            and (
                s.location.path == rel
                or s.location.path.endswith("/" + rel)
                or cand.module in s.qualname.replace("`", "")
            )
        ]
        if hits:
            hits.sort(
                key=lambda s: (
                    0 if s.location.path.endswith(rel) or s.location.path == rel else 1,
                    0 if s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD} else 1,
                    0 if s.kind in {SymbolKind.CLASS, SymbolKind.INTERFACE} else 1,
                    s.location.path,
                )
            )
            return hits[0]
        return None

    if not cand.path:
        return None

    rel = cand.path
    if cand.attr == "main":
        mains = [
            s
            for s in store.symbols_in_path(rel)
            if s.name == "main" and s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD}
        ]
        if mains:
            return mains[0]

    return _symbol_for_entry_file(store, rel)


def _symbol_for_entry_file(store: SymbolStore, rel: str) -> Symbol | None:
    """Prefer a precise export/symbol inside the file; fall back to the file module."""
    syms = store.symbols_in_path(rel)
    if not syms:
        return None
    stem = Path(rel).stem
    preferred_names = {stem, "main", "Main", "App", "default", "run", "start", "page"}
    ranked: list[tuple[int, Symbol]] = []
    for sym in syms:
        if sym.kind not in {
            SymbolKind.FUNCTION,
            SymbolKind.METHOD,
            SymbolKind.CLASS,
            SymbolKind.INTERFACE,
        }:
            continue
        if sym.signature == "external":
            continue
        score = 0
        if sym.name == stem:
            score += 4
        if sym.name in preferred_names:
            score += 2
        ranked.append((score, sym))
    ranked.sort(key=lambda item: (-item[0], item[1].location.line, item[1].name))
    if ranked and ranked[0][0] > 0:
        return ranked[0][1]

    modules = [
        s for s in syms if s.kind == SymbolKind.MODULE and s.signature == "file"
    ]
    if modules:
        return modules[0]
    for sym in syms:
        if sym.kind in {
            SymbolKind.FUNCTION,
            SymbolKind.METHOD,
            SymbolKind.CLASS,
            SymbolKind.INTERFACE,
        }:
            return sym
    return syms[0]


def _native_main_symbols(store: SymbolStore, *, limit: int = 40) -> list[Symbol]:
    native: list[Symbol] = []
    for name in ("main", "WinMain", "wWinMain"):
        for sym in store.search(name, limit=200):
            if sym.name != name:
                continue
            if sym.kind not in {SymbolKind.FUNCTION, SymbolKind.METHOD}:
                continue
            if sym.signature in {"external", "unresolved"}:
                continue
            ext = Path(sym.location.path).suffix.lower()
            if ext in {
                ".c",
                ".cc",
                ".cpp",
                ".cxx",
                ".cu",
                ".h",
                ".hpp",
                ".cuh",
                ".java",
                ".kt",
                ".kts",
                ".scala",
            }:
                native.append(sym)

    def path_rank(path: str) -> tuple[int, int, str]:
        p = path.replace("\\", "/").lstrip("./")
        if p.startswith("src/") or p.startswith("lib/"):
            rank = 0
        elif p.startswith("deps/") or "/deps/" in p or p.startswith("third_party/"):
            rank = 3
        elif "/test/" in p or p.startswith("tests/") or "/examples/" in p:
            rank = 2
        else:
            rank = 1
        # Prefer the primary binary entry (server.c / main.c) within a rank.
        name = Path(p).name.lower()
        tip = 0 if name in {"server.c", "main.c", "main.cpp", "main.cc"} else 1
        return (rank, tip, p)

    native.sort(key=lambda s: path_rank(s.location.path))
    seen: set[str] = set()
    out: list[Symbol] = []
    for sym in native:
        if sym.id in seen:
            continue
        seen.add(sym.id)
        out.append(sym)
        if len(out) >= limit:
            break
    return out
