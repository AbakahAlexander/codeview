"""Generate a SCIP index on the fly (users never need to know about SCIP)."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from codeview.fsutil import path_is_skipped
from codeview.paths import bin_dir, scip_cache_dir, tools_dir

ProgressCb = Callable[[int, str], None]


@dataclass
class IndexJobState:
    status: str = "idle"  # idle | indexing | ready | error
    percent: int = 0
    message: str = ""
    error: str | None = None
    revision: str | None = None
    has_graph: bool = False
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "percent": self.percent,
            "message": self.message,
            "error": self.error,
            "revision": self.revision,
            "has_graph": self.has_graph,
        }


# Extension → logical language key used to pick an indexer.
_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sc": "scala",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".rb": "ruby",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "c",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".cu": "cuda",
    ".cuh": "cuda",
    ".cs": "csharp",
}


def detect_index_languages(root: Path, *, sample_limit: int = 50_000) -> list[str]:
    """Return language keys present under root, most important first."""
    root = root.resolve()
    found: set[str] = set()
    count = 0

    def consider(path: Path) -> None:
        nonlocal count
        if count >= sample_limit or path_is_skipped(path):
            return
        if not path.is_file():
            return
        lang = _EXT_LANG.get(path.suffix.lower())
        if lang:
            found.add(lang)
            count += 1

    if (root / ".git").exists():
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "ls-files"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            for line in proc.stdout.splitlines():
                consider(root / line)
                if len(found) >= 8 and count >= 200:
                    break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if not found:
        for path in root.rglob("*"):
            consider(path)
            if len(found) >= 8 and count >= 500:
                break

    # Prefer one primary indexer when mixed; order matters.
    priority = [
        "python",
        "typescript",
        "javascript",
        "java",
        "kotlin",
        "scala",
        "go",
        "rust",
        "csharp",
        "ruby",
        "cpp",
        "c",
        "cuda",
    ]
    # Collapse JS into typescript indexer.
    if "javascript" in found:
        found.discard("javascript")
        found.add("typescript")
    if "cuda" in found:
        found.add("cpp")
    return [name for name in priority if name in found]


def generate_scip(
    root: Path,
    *,
    out_path: Path | None = None,
    on_progress: ProgressCb | None = None,
) -> Path:
    """Run the best available indexer; return path to index.scip."""
    root = root.resolve()
    out = out_path or (scip_cache_dir(root) / "index.scip")
    out.parent.mkdir(parents=True, exist_ok=True)

    def progress(pct: int, msg: str) -> None:
        if on_progress:
            on_progress(max(0, min(99, pct)), msg)

    progress(5, "Detecting languages…")
    languages = detect_index_languages(root)
    if not languages:
        raise RuntimeError(
            "No supported languages found in this project. "
            "Supported: Python, JS/TS, Go, Java/Kotlin/Scala, Rust, Ruby, C/C++, C#."
        )

    # Try each language until one indexer succeeds.
    errors: list[str] = []
    for i, lang in enumerate(languages):
        base = 10 + int(70 * i / max(1, len(languages)))
        progress(base, f"Indexing ({lang})…")
        try:
            path = _run_indexer(lang, root, out, progress)
            progress(95, "Index ready")
            return path
        except Exception as exc:
            errors.append(f"{lang}: {exc}")
            continue

    raise RuntimeError(
        "Could not build an index for this project.\n" + "\n".join(errors)
    )


def _run_indexer(lang: str, root: Path, out: Path, progress: ProgressCb) -> Path:
    if lang == "python":
        return _index_python(root, out, progress)
    if lang == "typescript":
        return _index_typescript(root, out, progress)
    if lang == "go":
        return _index_go(root, out, progress)
    if lang in {"java", "kotlin", "scala"}:
        return _index_jvm(root, out, progress)
    if lang == "rust":
        return _index_rust(root, out, progress)
    if lang == "ruby":
        return _index_ruby(root, out, progress)
    if lang in {"c", "cpp", "cuda"}:
        return _index_clang(root, out, progress)
    if lang == "csharp":
        return _index_dotnet(root, out, progress)
    raise RuntimeError(f"No indexer wired for {lang}")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(cmd: list[str], *, cwd: Path, env: dict | None = None, timeout: int = 3600) -> None:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=merged,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(err[:2000])


def _npm() -> str:
    npm = _which("npm")
    if not npm:
        raise RuntimeError("npm is required to fetch language indexers (install Node.js)")
    return npm


def _ensure_npm_package(package: str, binary: str) -> Path:
    """Install an npm package under ~/.codeview/tools and return the binary path."""
    tool_root = tools_dir() / "npm" / package.replace("/", "__").replace("@", "")
    bin_path = tool_root / "node_modules" / ".bin" / binary
    if bin_path.is_file():
        return bin_path
    tool_root.mkdir(parents=True, exist_ok=True)
    _run([_npm(), "install", "--no-fund", "--no-audit", package], cwd=tool_root, timeout=600)
    if not bin_path.is_file():
        # Some packages put the bin only on PATH after install; try npx-style path.
        alt = tool_root / "node_modules" / package / "bin" / binary
        if alt.is_file():
            return alt
        raise RuntimeError(f"Installed {package} but could not find binary {binary}")
    return bin_path


def _move_scip_output(root: Path, out: Path) -> Path:
    candidates = [
        root / "index.scip",
        root / "dump.scip",
    ]
    for src in candidates:
        if src.is_file():
            if src.resolve() != out.resolve():
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(out))
            return out
    if out.is_file():
        return out
    raise RuntimeError("Indexer finished but no index.scip was produced")


def _index_python(root: Path, out: Path, progress: ProgressCb) -> Path:
    progress(15, "Preparing Python indexer…")
    exe = _which("scip-python")
    if not exe:
        bin_path = _ensure_npm_package("@sourcegraph/scip-python", "scip-python")
        exe = str(bin_path)
    progress(30, "Indexing Python…")
    name = root.name or "project"
    _run(
        [exe, "index", ".", "--project-name", name, "--project-version", "_"],
        cwd=root,
        timeout=3600,
    )
    return _move_scip_output(root, out)


def _index_typescript(root: Path, out: Path, progress: ProgressCb) -> Path:
    progress(15, "Preparing JS/TS indexer…")
    exe = _which("scip-typescript")
    if not exe:
        bin_path = _ensure_npm_package("@sourcegraph/scip-typescript", "scip-typescript")
        exe = str(bin_path)
    progress(30, "Indexing JavaScript/TypeScript…")
    _run([exe, "index"], cwd=root, timeout=3600)
    return _move_scip_output(root, out)


def _index_go(root: Path, out: Path, progress: ProgressCb) -> Path:
    exe = _which("scip-go")
    if not exe:
        raise RuntimeError("scip-go not found on PATH")
    progress(30, "Indexing Go…")
    _run([exe, "index", "."], cwd=root, timeout=3600)
    return _move_scip_output(root, out)


def _index_jvm(root: Path, out: Path, progress: ProgressCb) -> Path:
    exe = _which("scip-java")
    if not exe:
        # Common local install location we may populate later.
        candidate = bin_dir() / "scip-java"
        if candidate.is_file():
            exe = str(candidate)
        else:
            raise RuntimeError("scip-java not found on PATH")
    progress(30, "Indexing JVM sources…")
    _run([exe, "index"], cwd=root, timeout=3600)
    return _move_scip_output(root, out)


def _index_rust(root: Path, out: Path, progress: ProgressCb) -> Path:
    exe = _which("rust-analyzer")
    if not exe:
        raise RuntimeError("rust-analyzer not found on PATH")
    progress(30, "Indexing Rust…")
    # rust-analyzer scip export
    with out.open("wb") as fh:
        proc = subprocess.run(
            [exe, "scip", "."],
            cwd=str(root),
            stdout=fh,
            stderr=subprocess.PIPE,
            text=False,
            timeout=3600,
            check=False,
        )
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(err or "rust-analyzer scip failed")
    return out


def _index_ruby(root: Path, out: Path, progress: ProgressCb) -> Path:
    exe = _which("scip-ruby")
    if not exe:
        raise RuntimeError("scip-ruby not found on PATH")
    progress(30, "Indexing Ruby…")
    _run([exe, "index"], cwd=root, timeout=3600)
    return _move_scip_output(root, out)


def _index_clang(root: Path, out: Path, progress: ProgressCb) -> Path:
    exe = _which("scip-clang")
    if not exe:
        raise RuntimeError("scip-clang not found on PATH")
    progress(30, "Indexing C/C++…")
    _run([exe, "index"], cwd=root, timeout=3600)
    return _move_scip_output(root, out)


def _index_dotnet(root: Path, out: Path, progress: ProgressCb) -> Path:
    exe = _which("scip-dotnet")
    if not exe:
        raise RuntimeError("scip-dotnet not found on PATH")
    progress(30, "Indexing .NET…")
    _run([exe, "index"], cwd=root, timeout=3600)
    return _move_scip_output(root, out)


class BackgroundIndexer:
    """Single-flight background index job attached to ExplorerService."""

    def __init__(self) -> None:
        self.state = IndexJobState()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        with self._lock:
            return self.state.to_dict()

    def _set(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(self.state, key, value)
            self.state.updated_at = time.time()

    def start(self, fn) -> bool:
        """Start ``fn`` in a daemon thread if none is running. Returns False if busy."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            # Caller may already have set status/message; keep them if present.
            if self.state.status != "indexing":
                self.state = IndexJobState(status="indexing", percent=0, message="Starting…")
        self._thread = threading.Thread(target=fn, name="codeview-index", daemon=True)
        self._thread.start()
        return True
