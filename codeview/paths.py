"""Locations under ~/.codeview — indexes, tools, caches."""

from __future__ import annotations

import shutil
from pathlib import Path


def codeview_home() -> Path:
    return Path.home() / ".codeview"


def indexes_dir() -> Path:
    return codeview_home() / "indexes"


def bin_dir() -> Path:
    return codeview_home() / "bin"


def tools_dir() -> Path:
    return codeview_home() / "tools"


def scip_cache_dir(root: Path) -> Path:
    """Per-project SCIP artifact cache (invisible to users)."""
    name = str(root.resolve()).strip("/").replace("/", "__")[:180] or "index"
    return codeview_home() / "scip" / name


def purge_codeview_data() -> dict[str, object]:
    """Delete ~/.codeview (indexes, binaries, SCIP cache, tools)."""
    home = codeview_home()
    existed = home.exists()
    size_hint = 0
    if existed:
        for path in home.rglob("*"):
            if path.is_file():
                try:
                    size_hint += path.stat().st_size
                except OSError:
                    pass
        shutil.rmtree(home)
    return {
        "purged": True,
        "path": str(home),
        "existed": existed,
        "bytes_removed_approx": size_hint,
    }
