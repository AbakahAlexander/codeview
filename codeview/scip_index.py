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
    """Return language keys present under root, dominant language first.

    Mixed repos (e.g. Apache Iceberg: thousands of ``.java`` + a couple of
    generated ``.py`` files) must not pick Python just because it appears first
    in a fixed priority list.
    """
    from collections import Counter

    root = root.resolve()
    counts: Counter[str] = Counter()
    scanned = 0

    def consider(path: Path) -> None:
        nonlocal scanned
        if scanned >= sample_limit or path_is_skipped(path):
            return
        if not path.is_file():
            return
        lang = _EXT_LANG.get(path.suffix.lower())
        if not lang:
            return
        counts[lang] += 1
        scanned += 1

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
                if scanned >= sample_limit:
                    break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if not counts:
        for path in root.rglob("*"):
            consider(path)
            if scanned >= sample_limit:
                break

    if not counts:
        return []

    # Collapse JS into typescript indexer.
    if counts.get("javascript"):
        counts["typescript"] += counts.pop("javascript")
    if counts.get("cuda"):
        counts["cpp"] += counts["cuda"]

    # Build-system hints: bump the primary stack when manifests are obvious.
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file() or (
        root / "pom.xml"
    ).is_file():
        for lang in ("java", "kotlin", "scala"):
            if counts.get(lang):
                counts[lang] += 10_000
                break
    if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
        if counts.get("python"):
            counts["python"] += 10_000
    if (root / "go.mod").is_file() and counts.get("go"):
        counts["go"] += 10_000
    if (root / "Cargo.toml").is_file() and counts.get("rust"):
        counts["rust"] += 10_000
    if (root / "package.json").is_file() and counts.get("typescript"):
        counts["typescript"] += 10_000

    # Dominant language first; ignore tiny companions (<2% of the leader)
    # so a couple of generated scripts don't steal the index.
    ordered = [lang for lang, _n in counts.most_common()]
    leader = counts[ordered[0]]
    significant = [lang for lang in ordered if counts[lang] * 50 >= leader or lang == ordered[0]]
    return significant


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

    # Try each language until one indexer succeeds (dominant language first).
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


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict | None = None,
    timeout: int = 3600,
    on_progress: ProgressCb | None = None,
    progress_start: int = 30,
    progress_end: int = 88,
    progress_label: str = "Indexing…",
) -> None:
    """Run a subprocess; optionally stream live percent/message updates."""
    merged = os.environ.copy()
    if env:
        merged.update(env)

    if on_progress is None:
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
        return

    import re
    import threading

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=merged,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    started = time.time()
    last_line = progress_label
    stop = threading.Event()
    pct_re = re.compile(r"(\d{1,3})\s*%")

    def reader() -> None:
        nonlocal last_line
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            # Skip curl/progress-meter noise and blank Gradle chatter.
            low = line.lower()
            if " --:--:--" in line or line.startswith("% Total"):
                continue
            if low.startswith("to honour the jvm settings"):
                continue
            if "configuration on demand is an incubating feature" in low:
                continue
            last_line = line[:160]

    def heartbeat() -> None:
        while not stop.wait(0.35):
            elapsed = time.time() - started
            # Climb from progress_start → progress_end while the tool runs.
            # Faster early movement, then asymptote (unknown-length jobs).
            span = max(1, progress_end - progress_start)
            frac = 1.0 - (1.0 / (1.0 + elapsed / 12.0))
            pct = progress_start + int(span * frac)
            m = pct_re.search(last_line)
            if m:
                reported = min(99, int(m.group(1)))
                pct = progress_start + int(span * (reported / 100.0))
            label = last_line or progress_label
            on_progress(min(progress_end, pct), f"{label} · {int(elapsed)}s")

    t_read = threading.Thread(target=reader, name="codeview-index-out", daemon=True)
    t_beat = threading.Thread(target=heartbeat, name="codeview-index-beat", daemon=True)
    on_progress(progress_start, progress_label)
    t_read.start()
    t_beat.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        stop.set()
        raise RuntimeError(f"Timed out after {timeout}s: {' '.join(cmd[:3])}") from exc
    finally:
        stop.set()
        t_read.join(timeout=2)
        t_beat.join(timeout=2)

    if rc != 0:
        raise RuntimeError((last_line or f"exit {rc}")[:2000])


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
        on_progress=progress,
        progress_start=30,
        progress_end=90,
        progress_label="Indexing Python…",
    )
    return _move_scip_output(root, out)


def _index_typescript(root: Path, out: Path, progress: ProgressCb) -> Path:
    progress(15, "Preparing JS/TS indexer…")
    exe = _which("scip-typescript")
    if not exe:
        bin_path = _ensure_npm_package("@sourcegraph/scip-typescript", "scip-typescript")
        exe = str(bin_path)
    progress(30, "Indexing JavaScript/TypeScript…")
    _run(
        [exe, "index"],
        cwd=root,
        timeout=3600,
        on_progress=progress,
        progress_start=30,
        progress_end=90,
        progress_label="Indexing JavaScript/TypeScript…",
    )
    return _move_scip_output(root, out)


def _index_go(root: Path, out: Path, progress: ProgressCb) -> Path:
    exe = _which("scip-go")
    if not exe:
        raise RuntimeError("scip-go not found on PATH")
    progress(30, "Indexing Go…")
    _run(
        [exe, "index", "."],
        cwd=root,
        timeout=3600,
        on_progress=progress,
        progress_start=30,
        progress_end=90,
        progress_label="Indexing Go…",
    )
    return _move_scip_output(root, out)


def _index_jvm(root: Path, out: Path, progress: ProgressCb) -> Path:
    progress(15, "Preparing JVM indexer…")
    exe = _ensure_scip_java()
    progress(30, "Indexing JVM sources…")
    cmd = [exe, "index", f"--output={out}"]
    # Large multi-module Gradle repos (Iceberg, Spark connectors, …) often fail
    # configuring optional Spark/Flink matrices or test compilation under the
    # SemanticDB plugin. Prefer main sources with empty version matrices.
    if (root / "gradlew").is_file() or (root / "build.gradle").is_file() or (
        root / "build.gradle.kts"
    ).is_file():
        cmd.extend(
            [
                "--",
                "-DsparkVersions=",
                "-DflinkVersions=",
                "-DkafkaVersions=",
                "-x",
                "compileTestJava",
                "-x",
                "test",
            ]
        )
    try:
        _run(
            cmd,
            cwd=root,
            timeout=7200,
            on_progress=progress,
            progress_start=30,
            progress_end=90,
            progress_label="Indexing JVM sources…",
        )
    except RuntimeError as first:
        # Plain retry without Gradle flags (Maven / simple Gradle).
        if len(cmd) == 3:
            raise
        progress(32, f"Retrying JVM index (simple): {str(first)[:80]}")
        _run(
            [exe, "index", f"--output={out}"],
            cwd=root,
            timeout=7200,
            on_progress=progress,
            progress_start=35,
            progress_end=90,
            progress_label="Indexing JVM sources…",
        )
    if out.is_file():
        return out
    return _move_scip_output(root, out)


SCIP_JAVA_VERSION = "v0.12.3"


def _ensure_scip_java() -> str:
    """Locate scip-java on PATH or download a release binary into ~/.codeview/bin."""
    exe = _which("scip-java")
    if exe:
        return exe
    dest = bin_dir() / "scip-java"
    if dest.is_file() and os.access(dest, os.X_OK):
        return str(dest)

    import urllib.request

    # Needs a JDK to run; fail early with a clear message.
    if not _which("java"):
        raise RuntimeError(
            "scip-java requires a JDK (java) on PATH. Install JDK 17+ and retry."
        )

    url = (
        f"https://github.com/scip-code/scip-java/releases/download/"
        f"{SCIP_JAVA_VERSION}/scip-java-{SCIP_JAVA_VERSION}"
    )
    bin_dir().mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".download")
    try:
        urllib.request.urlretrieve(url, tmp)
        tmp.chmod(0o755)
        tmp.replace(dest)
    except Exception as exc:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download scip-java: {exc}") from exc
    return str(dest)


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


SCIP_CLANG_VERSION = "v0.4.0"


def _ensure_scip_clang() -> str:
    """Locate scip-clang on PATH or download a release binary into ~/.codeview/bin."""
    exe = _which("scip-clang")
    if exe:
        return exe
    dest = bin_dir() / "scip-clang"
    if dest.is_file() and os.access(dest, os.X_OK):
        return str(dest)

    import platform
    import urllib.request

    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"} and system == "linux":
        asset = "scip-clang-x86_64-linux"
    elif machine in {"aarch64", "arm64"} and system == "darwin":
        asset = "scip-clang-arm64-darwin"
    elif machine in {"x86_64", "amd64"} and system == "darwin":
        asset = "scip-clang-x86_64-darwin"
    else:
        raise RuntimeError(
            "scip-clang is not available for this platform; install it manually from "
            "https://github.com/sourcegraph/scip-clang/releases"
        )

    url = (
        f"https://github.com/sourcegraph/scip-clang/releases/download/"
        f"{SCIP_CLANG_VERSION}/{asset}"
    )
    bin_dir().mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".download")
    try:
        urllib.request.urlretrieve(url, tmp)
        tmp.chmod(0o755)
        tmp.replace(dest)
    except Exception as exc:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download scip-clang: {exc}") from exc
    return str(dest)


def _find_compdb(root: Path) -> Path | None:
    for rel in (
        "compile_commands.json",
        "build/compile_commands.json",
        "cmake-build-debug/compile_commands.json",
        "cmake-build-release/compile_commands.json",
        "out/compile_commands.json",
    ):
        path = root / rel
        if path.is_file():
            return path
    return None


def _try_cmake_compdb(root: Path, progress: ProgressCb) -> Path | None:
    """Configure with CMAKE_EXPORT_COMPILE_COMMANDS when CMakeLists.txt exists."""
    if not (root / "CMakeLists.txt").is_file():
        return None
    cmake = _which("cmake")
    if not cmake:
        return None
    build = root / ".codeview-build"
    progress(22, "Generating compile_commands.json (CMake)…")
    build.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [cmake, "-B", str(build), "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", str(root)],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    candidate = build / "compile_commands.json"
    if candidate.is_file():
        return candidate
    # CMake may fail (e.g. missing CUDA) — caller can synthesize a fallback.
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    if err:
        progress(24, f"CMake configure skipped: {err[-1][:120]}")
    return None


def _synthesize_compdb(root: Path) -> Path:
    """Best-effort compile_commands.json from source files (no full build required)."""
    import json

    compiler = _which("clang++") or _which("g++") or _which("c++")
    if not compiler:
        raise RuntimeError(
            "Need clang++ or g++ on PATH to index C/C++ without a compile_commands.json"
        )
    cxx_ext = {".c", ".cc", ".cpp", ".cxx", ".cu"}
    entries: list[dict[str, str]] = []
    include_flags: list[str] = []
    for inc in ("include", "src", "lib", "third_party"):
        if (root / inc).is_dir():
            include_flags.extend(["-I", str(root / inc)])

    skip_parts = {".codeview-build", "CMakeFiles", "build", ".git"}
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in cxx_ext or not path.is_file():
            continue
        if path_is_skipped(path):
            continue
        if any(part in skip_parts for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        # Skip CUDA unless clang is present (scip-clang needs it for .cu).
        if path.suffix.lower() == ".cu" and not _which("clang"):
            continue
        cmd = [compiler, "-std=c++17", "-c", rel, *include_flags]
        entries.append(
            {
                "directory": str(root),
                "file": rel,
                "arguments": cmd,
            }
        )
    if not entries:
        raise RuntimeError("No C/C++ source files found to index")
    out = root / ".codeview-compile_commands.json"
    out.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return out


def _index_clang(root: Path, out: Path, progress: ProgressCb) -> Path:
    progress(15, "Preparing C/C++ indexer…")
    exe = _ensure_scip_clang()
    progress(20, "Resolving compilation database…")
    compdb = _find_compdb(root)
    if compdb is None:
        compdb = _try_cmake_compdb(root, progress)
    if compdb is None:
        progress(25, "Synthesizing compile_commands.json…")
        compdb = _synthesize_compdb(root)
    progress(30, "Indexing C/C++…")
    # scip-clang must be invoked from the project root with an explicit compdb.
    _run(
        [
            exe,
            f"--compdb-path={compdb}",
            f"--index-output-path={out}",
        ],
        cwd=root,
        timeout=3600,
        on_progress=progress,
        progress_start=30,
        progress_end=90,
        progress_label="Indexing C/C++…",
    )
    if out.is_file():
        return out
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
