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
    """Resolve candidates to ``EntryPoint`` records (with symbols when possible)."""
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
        if cand.category == EntryPointKind.LIBRARY and cand.path and not cand.attr:
            # JVM API tree marker — expand via store query below if nothing else resolves.
            continue

        primary = _resolve_candidate_symbol(store, cand)
        if primary:
            add_entry(_to_entry(cand, primary))
            if cand.prefer_imports and primary.kind == SymbolKind.MODULE:
                for imported in _entry_file_imports(store, primary, limit=5):
                    add_entry(
                        EntryPoint(
                            category=EntryPointKind.FRONTEND,
                            display_name=imported.name,
                            source="import from entry file",
                            confidence=Confidence.LIKELY,
                            symbol_id=imported.id,
                            target=imported.location.path,
                            evidence=[*cand.evidence, f"referenced from {cand.path}"],
                            symbol=imported,
                        )
                    )
            continue

        # Unresolved file still recorded without symbol (tests / diagnostics).
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

    if not any(ep.symbol_id for ep in resolved):
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
        if not any(ep.symbol_id for ep in resolved):
            for sym in _library_api_symbols(store, limit=limit):
                add_entry(
                    EntryPoint(
                        category=EntryPointKind.LIBRARY,
                        display_name=sym.name,
                        source="JVM public API type",
                        confidence=Confidence.LIKELY,
                        symbol_id=sym.id,
                        target=sym.location.path,
                        evidence=[sym.location.path],
                        symbol=sym,
                    )
                )

    # Drop unresolved placeholders from the UI list when we have real symbols.
    with_symbols = [ep for ep in resolved if ep.symbol_id and ep.symbol]
    return (with_symbols or resolved)[:limit]


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
        hits = [
            s
            for s in store.search(cand.attr, limit=30)
            if s.name == cand.attr
            and s.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD}
            and (
                s.location.path == rel
                or s.location.path.endswith("/" + rel)
                or cand.module in s.qualname.replace("`", "")
            )
        ]
        if hits:
            return hits[0]
        loose = [s for s in store.search(cand.attr, limit=20) if s.name == cand.attr]
        prefer = [s for s in loose if rel in s.location.path]
        return (prefer or loose or [None])[0]

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


def _entry_file_imports(
    store: SymbolStore, module: Symbol, *, limit: int = 5
) -> list[Symbol]:
    rows = store._conn.execute(
        """
        SELECT s.*
        FROM relations r
        JOIN symbols s ON s.id = r.to_id
        WHERE r.from_id = ?
          AND r.kind = 'references'
          AND s.kind IN ('function', 'class', 'interface', 'method')
          AND (s.signature IS NULL OR s.signature NOT IN ('external', 'file'))
          AND s.path != ?
        ORDER BY
          CASE
            WHEN s.name IN ('App', 'Main', 'Game', 'Root', 'Index') THEN 0
            WHEN s.path LIKE 'src/components/%' OR s.path LIKE 'src/pages/%' THEN 1
            ELSE 2
          END,
          s.path,
          s.line
        """,
        (module.id, module.location.path),
    ).fetchall()
    out: list[Symbol] = []
    seen: set[str] = set()
    for row in rows:
        sym = store._row_to_symbol(row)
        if sym.id in seen:
            continue
        seen.add(sym.id)
        out.append(sym)
        if len(out) >= limit:
            break
    return out


def _native_main_symbols(store: SymbolStore, *, limit: int = 40) -> list[Symbol]:
    native: list[Symbol] = []
    for name in ("main", "WinMain", "wWinMain"):
        for sym in store.search(name, limit=80):
            if sym.name != name:
                continue
            if sym.kind not in {SymbolKind.FUNCTION, SymbolKind.METHOD}:
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
    native.sort(
        key=lambda s: (
            0 if not s.location.path.replace("\\", "/").startswith("tests/") else 1,
            0 if "/test/" not in s.location.path.replace("\\", "/").lower() else 1,
            0 if not s.location.path.replace("\\", "/").startswith("benchmarks/") else 1,
            s.location.path,
        )
    )
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


def _library_api_symbols(store: SymbolStore, *, limit: int = 40) -> list[Symbol]:
    rows = store._conn.execute(
        """
        SELECT * FROM symbols
        WHERE kind IN ('class', 'interface')
          AND (
            path LIKE '%/api/src/main/java/%'
            OR path LIKE '%/api/src/main/kotlin/%'
            OR path LIKE 'api/src/main/java/%'
            OR path LIKE 'src/main/java/%'
            OR path LIKE 'lib/src/main/java/%'
          )
          AND path NOT LIKE '%/test/%'
          AND path NOT LIKE '%/tests/%'
        ORDER BY
          CASE
            WHEN path LIKE '%/api/src/main/%' OR path LIKE 'api/src/main/%' THEN 0
            ELSE 1
          END,
          length(path),
          name
        LIMIT ?
        """,
        (max(limit * 8, 80),),
    ).fetchall()
    primary: list[Symbol] = []
    seen_paths: set[str] = set()
    fallback: list[Symbol] = []
    for row in rows:
        sym = store._row_to_symbol(row)
        stem = Path(sym.location.path).stem
        if sym.name == stem and sym.location.path not in seen_paths:
            seen_paths.add(sym.location.path)
            primary.append(sym)
        elif sym.location.path not in seen_paths:
            fallback.append(sym)
    out = primary + [
        s for s in fallback if s.location.path not in {p.location.path for p in primary}
    ]
    return out[:limit]
