"""Locations under ~/.codeview — indexes, tools, caches."""

from __future__ import annotations

import shutil
from pathlib import Path


def codeview_home() -> Path:
    return Path.home() / ".codeview"


def indexes_dir() -> Path:
    return codeview_home() / "indexes"


def repos_dir() -> Path:
    """Shallow clones of public git URLs (ephemeral for ``serve``)."""
    return codeview_home() / "repos"


def bin_dir() -> Path:
    return codeview_home() / "bin"


def tools_dir() -> Path:
    return codeview_home() / "tools"


def scip_cache_dir(root: Path) -> Path:
    """Per-project SCIP artifact cache (invisible to users)."""
    name = str(root.resolve()).strip("/").replace("/", "__")[:180] or "index"
    return codeview_home() / "scip" / name


def is_ephemeral_clone(root: Path) -> bool:
    """True when ``root`` lives under ``~/.codeview/repos/`` (git URL peek)."""
    try:
        root.resolve().relative_to(repos_dir().resolve())
        return True
    except (ValueError, OSError):
        return False


def _rm_tree(path: Path, removed: list[str]) -> None:
    if not path.exists():
        return
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(str(path))
    except OSError:
        pass


def purge_ephemeral_session(*, root: Path, db_path: Path | None = None) -> dict[str, object]:
    """Delete a peek clone and its index/SCIP cache. No-op for local paths."""
    root = root.resolve()
    if not is_ephemeral_clone(root):
        return {"purged": False, "reason": "not an ephemeral clone", "root": str(root)}

    removed: list[str] = []
    _rm_tree(root, removed)
    _rm_tree(scip_cache_dir(root), removed)
    if db_path is not None:
        db = db_path.expanduser().resolve()
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db) + suffix) if suffix else db
            _rm_tree(candidate, removed)
    return {"purged": True, "root": str(root), "removed": removed}


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
