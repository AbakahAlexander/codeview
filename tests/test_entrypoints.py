"""Entry-point detection across representative fixture repositories."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeview.entrypoints import detect_candidates
from codeview.entrypoints.resolve import resolve_entry_points, symbols_from_entries
from codeview.models import (
    Confidence,
    EntryPointKind,
    Location,
    RelationKind,
    Symbol,
    SymbolKind,
)
from codeview.store import SymbolStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _paths(cands) -> list[str]:
    return [c.path for c in cands if c.path]


def test_python_cli_scripts():
    root = FIXTURES / "python_cli"
    cands = detect_candidates(root)
    assert any(
        c.category == EntryPointKind.CLI
        and c.command_name == "demo"
        and c.module == "pkg.cli"
        and c.attr == "main"
        and c.confidence == Confidence.CONFIRMED
        for c in cands
    )
    assert any(c.path == "pkg/cli.py" for c in cands)
    # Public re-exports are not entry points — reach them via calls/refs.
    assert not any(c.category == EntryPointKind.LIBRARY for c in cands)


def test_python_fastapi_main_guard():
    root = FIXTURES / "python_fastapi"
    cands = detect_candidates(root)
    paths = _paths(cands)
    assert "app/main.py" in paths
    hit = next(c for c in cands if c.path == "app/main.py")
    assert hit.source == "__main__ guard"
    assert hit.attr == "main"
    assert not any(c.category == EntryPointKind.LIBRARY for c in cands)


def test_dunder_main_not_skipped_under_dot_parent(tmp_path: Path):
    """Repos under ``~/.codeview/repos/...`` must still see ``__main__.py``."""
    from codeview.entrypoints.python import find_dunder_main_files

    root = tmp_path / ".codeview" / "repos" / "demo"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__main__.py").write_text("from .cli import main\nmain()\n", encoding="utf-8")
    (pkg / "cli.py").write_text("def main():\n    pass\n", encoding="utf-8")
    found = find_dunder_main_files(root)
    assert "pkg/__main__.py" in found


def test_react_vite_html_not_app_tsx():
    root = FIXTURES / "react_vite"
    cands = detect_candidates(root)
    paths = _paths(cands)
    assert paths[0] == "src/main.tsx"
    assert "src/App.tsx" not in paths
    assert cands[0].category == EntryPointKind.FRONTEND
    assert cands[0].confidence == Confidence.CONFIRMED
    assert cands[0].prefer_imports


def test_next_app_router_page():
    root = FIXTURES / "next_app_router"
    paths = _paths(detect_candidates(root))
    assert "app/page.tsx" in paths
    assert "app/layout.tsx" in paths


def test_node_cli_bin_not_exports():
    root = FIXTURES / "node_cli"
    cands = detect_candidates(root)
    paths = _paths(cands)
    assert "src/cli.ts" in paths
    assert "src/lib.ts" not in paths  # package exports are not entry points
    assert any(c.category == EntryPointKind.CLI and c.display_name == "widget" for c in cands)
    assert not any(c.category == EntryPointKind.LIBRARY for c in cands)


def test_java_gradle_no_library_api_as_entry():
    """Pure JVM libraries have no execution root — don't invent API-class entries."""
    root = FIXTURES / "java_gradle"
    cands = detect_candidates(root)
    assert not any(c.category == EntryPointKind.LIBRARY for c in cands)
    assert not any(c.path and "Schema" in c.path for c in cands)


def test_cpp_cmake_executable():
    root = FIXTURES / "cpp_cmake"
    cands = detect_candidates(root)
    assert any(
        c.path == "src/main.cpp"
        and c.attr == "main"
        and c.category == EntryPointKind.NATIVE_MAIN
        for c in cands
    )


def test_mixed_monorepo_workspace_packages():
    root = FIXTURES / "mixed_monorepo"
    cands = detect_candidates(root)
    paths = _paths(cands)
    assert "packages/web/src/main.tsx" in paths
    assert any(c.command_name == "api" and c.module == "api.app" for c in cands)


def test_resolve_pipeline_maps_execution_root_only(tmp_path: Path):
    """repository → detection → SCIP symbol mapping → entry symbols.

    Imports from the entry file are not promoted to entry points; they belong
    in the call/ref graph when the entry is expanded.
    """
    root = FIXTURES / "react_vite"
    db = tmp_path / "test.sqlite3"
    store = SymbolStore(db)
    store.set_meta("root", str(root))

    main_mod = Symbol(
        id="main-mod",
        name="main.tsx",
        kind=SymbolKind.MODULE,
        location=Location(path="src/main.tsx", line=1, column=0),
        qualname="src/main.tsx",
        language="typescript",
        signature="file",
    )
    app_fn = Symbol(
        id="app-fn",
        name="App",
        kind=SymbolKind.FUNCTION,
        location=Location(path="src/components/App.tsx", line=1, column=0),
        qualname="App",
        language="typescript",
    )
    store.replace_symbols([main_mod, app_fn])
    from codeview.models import Relation

    store.add_relations(
        [
            Relation(
                kind=RelationKind.REFERENCES,
                from_id=main_mod.id,
                to_id=app_fn.id,
                location=Location(path="src/main.tsx", line=1, column=0),
            )
        ]
    )

    entries = resolve_entry_points(store, detect_candidates(root), limit=20)
    symbols = symbols_from_entries(entries)
    names = {s.name for s in symbols}
    assert "main.tsx" in names
    assert "App" not in names
    assert all(e.category != EntryPointKind.LIBRARY for e in entries)
    assert all(e.evidence for e in entries if e.symbol_id)


@pytest.mark.parametrize(
    "fixture,expected_path",
    [
        ("react_vite", "src/main.tsx"),
        ("node_cli", "src/cli.ts"),
        ("cpp_cmake", "src/main.cpp"),
        ("next_app_router", "app/page.tsx"),
    ],
)
def test_fixture_expected_primary_path(fixture: str, expected_path: str):
    cands = detect_candidates(FIXTURES / fixture)
    assert expected_path in _paths(cands)
