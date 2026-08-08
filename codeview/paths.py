"""Locations under ~/.codeview — indexes, tools, caches."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
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
    except (ValueError, OSError):
        return False
    return True


def _clear_readonly(path: str) -> None:
    """Clear the Windows read-only bit (and any missing write bit) so unlink can succeed."""
    try:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _rmtree_error(func, path: str, exc: BaseException) -> None:
    """``shutil.rmtree`` callback: clear read-only and retry; re-raise if still blocked."""
    winerror = getattr(exc, "winerror", None)
    err_no = getattr(exc, "errno", None)
    access_denied = (
        isinstance(exc, PermissionError)
        or winerror == 5
        or err_no in {errno.EACCES, errno.EPERM}
    )
    if access_denied:
        _clear_readonly(path)
        try:
            func(path)
            return
        except OSError as retry_exc:
            raise RuntimeError(
                f"Could not delete {path}: {retry_exc}. "
                "Close any running codeview serve (and other programs using "
                "~/.codeview), then retry doctor --purge."
            ) from retry_exc
    raise exc


def rmtree_force(path: Path) -> None:
    """Delete a file or directory tree, clearing read-only bits (Windows git packs)."""
    if not path.exists():
        return
    if path.is_file() or path.is_symlink():
        try:
            path.unlink()
        except OSError:
            _clear_readonly(str(path))
            path.unlink()
        return

    def onexc(func, p, exc):
        _rmtree_error(func, p, exc)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=onexc)
    else:

        def onerror(func, p, exc_info):
            _rmtree_error(func, p, exc_info[1])

        shutil.rmtree(path, onerror=onerror)


def _rm_tree(path: Path, removed: list[str]) -> None:
    if not path.exists():
        return
    try:
        rmtree_force(path)
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
        rmtree_force(home)
    return {
        "purged": True,
        "path": str(home),
        "existed": existed,
        "bytes_removed_approx": size_hint,
    }
