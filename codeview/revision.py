"""Detect whether a project changed since the last index."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def project_revision(root: Path) -> str:
    """Stable revision token for ``root``.

    Prefer git HEAD. If not a git repo, hash a sample of source mtimes/sizes.
    """
    root = root.resolve()
    git = _git_head(root)
    if git:
        return f"git:{git}"
    return f"fs:{_filesystem_fingerprint(root)}"


def _git_head(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    head = (proc.stdout or "").strip()
    return head or None


def _filesystem_fingerprint(root: Path, *, limit: int = 5_000) -> str:
    """Cheap content-change signal when git is unavailable."""
    digest = hashlib.sha1()
    count = 0
    try:
        paths = sorted(p for p in root.rglob("*") if p.is_file())
    except OSError:
        return "unknown"
    for path in paths:
        rel = str(path.relative_to(root))
        if any(part.startswith(".") for part in path.parts):
            continue
        if any(
            skip in path.parts
            for skip in (
                "node_modules",
                "__pycache__",
                ".venv",
                "venv",
                "dist",
                "build",
                "target",
            )
        ):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        digest.update(rel.encode())
        digest.update(str(int(st.st_mtime_ns)).encode())
        digest.update(str(st.st_size).encode())
        count += 1
        if count >= limit:
            break
    digest.update(str(count).encode())
    return digest.hexdigest()[:20]
