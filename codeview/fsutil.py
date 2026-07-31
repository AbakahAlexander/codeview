from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from codeview.models import Location, Symbol, SymbolKind

SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "build",
    "Documentation",
    "tools",
    "samples",
    "scripts",
    ".indexes",
}


def stable_id(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def rel_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def directory_symbol(root: Path, rel: str) -> Symbol:
    name = Path(rel).name or str(root.name)
    return Symbol(
        id=stable_id("directory", rel or "."),
        name=name if rel else root.name,
        kind=SymbolKind.DIRECTORY,
        location=Location(path=rel or ".", line=1, column=0),
        qualname=f"dir:{rel or '.'}",
        language="filesystem",
        signature="directory",
    )


def file_symbol(root: Path, rel: str, language: str = "c") -> Symbol:
    return Symbol(
        id=stable_id("file", rel),
        name=Path(rel).name,
        kind=SymbolKind.MODULE,
        location=Location(path=rel, line=1, column=0),
        qualname=f"file:{rel}",
        language=language,
        signature="file",
    )


def browse(root: Path, rel: str, extensions: set[str]) -> list[Symbol]:
    """List immediate child directories and source files. Fast — no parsing."""
    root = root.resolve()
    base = root if not rel or rel in {".", ""} else (root / rel)
    if not base.is_dir():
        return []

    dirs: list[Symbol] = []
    files: list[Symbol] = []
    try:
        entries = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return []

    for entry in entries:
        if entry.name in SKIP_DIR_NAMES or entry.name.startswith("."):
            continue
        child_rel = rel_path(root, entry)
        if entry.is_dir():
            dirs.append(directory_symbol(root, child_rel))
        elif entry.is_file() and entry.suffix.lower() in extensions:
            from codeview.detect import language_for_extension

            lang = language_for_extension(entry.suffix)
            files.append(file_symbol(root, child_rel, language=lang))
    return dirs + files


def count_source_files(root: Path, extensions: set[str], limit: int = 5_000_000) -> int:
    root = root.resolve()
    # Prefer git ls-files when available — much faster on huge trees.
    if (root / ".git").exists():
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "ls-files"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            count = 0
            for line in proc.stdout.splitlines():
                suffix = Path(line).suffix.lower()
                if suffix in extensions and not any(part in SKIP_DIR_NAMES for part in Path(line).parts):
                    count += 1
                    if count >= limit:
                        break
            return count
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    count = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix.lower() in extensions:
            count += 1
            if count >= limit:
                break
    return count


def _rg_bin() -> str:
    import shutil

    found = shutil.which("rg")
    if found:
        return found
    candidates = [
        Path.home() / ".cargo/bin/rg",
        Path("/usr/share/cursor/resources/app/node_modules/@vscode/ripgrep/bin/rg"),
        Path("/usr/bin/rg"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "rg"


def rg_files(root: Path, query: str, globs: list[str], limit: int = 40) -> list[str]:
    """Return relative file paths that mention query (content search)."""
    if not query.strip():
        return []
    cmd = [
        _rg_bin(),
        "-l",
        "--no-messages",
        "-F",
        query.strip(),
        *sum((["-g", g] for g in globs), []),
        *sum((["-g", f"!{d}/**"] for d in ("Documentation", "tools", "samples", "scripts")), []),
        str(root),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in proc.stdout.splitlines():
        p = Path(line.strip())
        try:
            out.append(p.resolve().relative_to(root.resolve()).as_posix())
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def rg_call_sites(root: Path, name: str, globs: list[str], limit: int = 80) -> list[tuple[str, int]]:
    """Find approximate call sites for name( ... )."""
    pattern = rf"\b{re.escape(name)}\s*\("
    cmd = [
        _rg_bin(),
        "-n",
        "--no-messages",
        "-e",
        pattern,
        *sum((["-g", g] for g in globs), []),
        *sum((["-g", f"!{d}/**"] for d in ("Documentation", "tools", "samples", "scripts")), []),
        str(root),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    hits: list[tuple[str, int]] = []
    for line in proc.stdout.splitlines():
        # path:line:content
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        path_str, line_str = parts[0], parts[1]
        if not line_str.isdigit():
            continue
        p = Path(path_str)
        try:
            rel = p.resolve().relative_to(root.resolve()).as_posix()
        except Exception:
            continue
        hits.append((rel, int(line_str)))
        if len(hits) >= limit:
            break
    return hits
