"""Entry-point detection: package/build evidence → candidates → SCIP symbols."""

from __future__ import annotations

from pathlib import Path

from codeview.entrypoints.javascript import JavaScriptEntryDetector, find_js_ts_entry_files
from codeview.entrypoints.jvm import JvmEntryDetector
from codeview.entrypoints.models import EntryPointCandidate
from codeview.entrypoints.native import NativeEntryDetector, parse_cmake_executable_sources
from codeview.entrypoints.python import (
    PythonEntryDetector,
    find_dunder_main_files,
    parse_project_scripts,
)
from codeview.entrypoints.ranking import rank_candidates
from codeview.entrypoints.resolve import resolve_entry_points, symbols_from_entries
from codeview.models import EntryPoint
from codeview.store import SymbolStore

DETECTORS = (
    PythonEntryDetector(),
    JavaScriptEntryDetector(),
    NativeEntryDetector(),
    JvmEntryDetector(),
)

_WORKSPACE_DIRS = ("packages", "apps", "services")


def detect_candidates(root: Path, *, limit: int = 40) -> list[EntryPointCandidate]:
    """Run all supporting detectors and return ranked candidates."""
    root = root.resolve()
    found: list[EntryPointCandidate] = []
    for sub, prefix in _scan_roots(root):
        for detector in DETECTORS:
            if not detector.supports(sub):
                continue
            for cand in detector.detect(sub, limit=limit):
                found.append(_with_prefix(cand, prefix))
    return rank_candidates(found, limit=limit)


def detect_and_resolve(
    store: SymbolStore, *, limit: int = 40
) -> list[EntryPoint]:
    """Full pipeline: repository evidence → SCIP symbol mapping."""
    root = Path(store.get_meta("root") or ".")
    candidates = detect_candidates(root, limit=limit)
    return resolve_entry_points(store, candidates, limit=limit)


def _scan_roots(root: Path) -> list[tuple[Path, str]]:
    """Root plus one-level workspace packages (packages/*, apps/*, services/*)."""
    out: list[tuple[Path, str]] = [(root, "")]
    for name in _WORKSPACE_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                out.append((child, f"{name}/{child.name}"))
    return out


def _with_prefix(cand: EntryPointCandidate, prefix: str) -> EntryPointCandidate:
    if not prefix:
        return cand
    path = f"{prefix}/{cand.path}" if cand.path else None
    evidence = [f"{prefix}: {e}" for e in cand.evidence] or [prefix]
    return EntryPointCandidate(
        category=cand.category,
        display_name=cand.display_name,
        source=cand.source,
        confidence=cand.confidence,
        path=path,
        module=cand.module,
        attr=cand.attr,
        command_name=cand.command_name,
        evidence=evidence,
        prefer_imports=cand.prefer_imports,
    )


__all__ = [
    "DETECTORS",
    "EntryPointCandidate",
    "detect_and_resolve",
    "detect_candidates",
    "find_dunder_main_files",
    "find_js_ts_entry_files",
    "parse_cmake_executable_sources",
    "parse_project_scripts",
    "rank_candidates",
    "resolve_entry_points",
    "symbols_from_entries",
]
