"""Detect program entry points from packaging metadata and ``__main__`` hooks."""

from __future__ import annotations

import json
import re
from pathlib import Path

_JS_SOURCE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_JS_SKIP_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".next",
    "coverage",
    "out",
    ".turbo",
    ".venv",
    "venv",
    "__pycache__",
}

# Common SPA / Next / Node roots when package.json does not name a source file.
_JS_CONVENTIONAL_ENTRIES = (
    "src/main.tsx",
    "src/main.ts",
    "src/main.jsx",
    "src/main.js",
    "src/index.tsx",
    "src/index.ts",
    "src/index.jsx",
    "src/index.js",
    "src/App.tsx",
    "src/App.jsx",
    "src/App.ts",
    "src/App.js",
    "main.tsx",
    "main.ts",
    "main.jsx",
    "main.js",
    "index.tsx",
    "index.ts",
    "index.jsx",
    "index.js",
    "app/page.tsx",
    "app/page.jsx",
    "app/layout.tsx",
    "pages/_app.tsx",
    "pages/_app.jsx",
    "pages/_app.js",
    "pages/index.tsx",
    "pages/index.jsx",
    "pages/index.js",
    "src/pages/_app.tsx",
    "src/pages/_app.jsx",
    "src/pages/index.tsx",
    "src/pages/index.jsx",
    "server.ts",
    "server.js",
    "src/server.ts",
    "src/server.js",
)


def parse_project_scripts(root: Path) -> list[tuple[str, str, str]]:
    """Return ``(command_name, module, attr)`` from ``pyproject.toml`` scripts."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    # Prefer stdlib tomllib when available (3.11+); fall back to a tiny parser.
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore

    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception:
            data = {}
        scripts = {}
        project = data.get("project") or {}
        if isinstance(project, dict):
            for key in ("scripts", "gui-scripts"):
                block = project.get(key) or {}
                if isinstance(block, dict):
                    scripts.update(block)
        out: list[tuple[str, str, str]] = []
        for name, target in scripts.items():
            parsed = _split_entrypoint(str(target))
            if parsed:
                out.append((str(name), parsed[0], parsed[1]))
        return out

    return _parse_scripts_fallback(text)


def _split_entrypoint(target: str) -> tuple[str, str] | None:
    # module:attr  or module:attr.nested
    text = target.strip().strip("'\"")
    if ":" not in text:
        return None
    module, attr = text.split(":", 1)
    module, attr = module.strip(), attr.strip()
    if not module or not attr:
        return None
    # Use the final attribute as the symbol name (cli:main → main).
    name = attr.split(".")[-1]
    return module, name


def _parse_scripts_fallback(text: str) -> list[tuple[str, str, str]]:
    """Minimal ``[project.scripts]`` reader when tomllib is unavailable."""
    out: list[tuple[str, str, str]] = []
    in_scripts = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_scripts = line in {"[project.scripts]", "[project.gui-scripts]"}
            continue
        if not in_scripts or not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        name = left.strip().strip("'\"")
        parsed = _split_entrypoint(right)
        if name and parsed:
            out.append((name, parsed[0], parsed[1]))
    return out


def find_dunder_main_files(root: Path, *, limit: int = 200) -> list[str]:
    """Relative paths of ``__main__.py`` and modules with a top-level main guard."""
    root = root.resolve()
    found: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(r"""if\s+__name__\s*==\s*['\"]__main__['\"]""")

    def add(rel: str) -> None:
        if rel in seen:
            return
        seen.add(rel)
        found.append(rel)

    for path in root.rglob("__main__.py"):
        if any(
            part.startswith(".") or part in {"venv", ".venv", "node_modules", "__pycache__"}
            for part in path.parts
        ):
            continue
        try:
            add(path.relative_to(root).as_posix())
        except ValueError:
            continue
        if len(found) >= limit:
            return found

    for path in root.rglob("*.py"):
        if path.name == "__main__.py":
            continue
        if path.name == "entrypoints.py":
            continue
        if any(
            part.startswith(".") or part in {"venv", ".venv", "node_modules", "__pycache__"}
            for part in path.parts
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(text):
            try:
                add(path.relative_to(root).as_posix())
            except ValueError:
                pass
            if len(found) >= limit:
                break
    return found


def parse_cmake_executable_sources(root: Path, *, limit: int = 80) -> list[str]:
    """Relative source paths named in ``add_executable(...)`` blocks."""
    path = root / "CMakeLists.txt"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(
        r"add_executable\s*\((.*?)\)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        body = match.group(1)
        tokens = re.findall(
            r"[A-Za-z0-9_./+\-]+\.(?:cu|cuh|cpp|cxx|cc|mm|m|c)(?![A-Za-z])",
            body,
        )
        for tok in tokens:
            rel = tok.lstrip("./")
            if rel in seen:
                continue
            if not (root / rel).is_file():
                continue
            seen.add(rel)
            out.append(rel)
            if len(out) >= limit:
                return out
    return out


def find_js_ts_entry_files(root: Path, *, limit: int = 40) -> list[str]:
    """Relative JS/TS entry files from HTML, ``package.json``, and conventions."""
    root = root.resolve()
    if not (root / "package.json").is_file() and not (root / "tsconfig.json").is_file():
        # Still allow HTML-only / loose TS trees that use conventional names.
        if not any((root / name).is_file() for name in ("index.html", "src/main.tsx", "src/index.ts")):
            return []

    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        if not raw or len(found) >= limit:
            return
        resolved = _resolve_js_source_path(root, raw)
        if not resolved or resolved in seen:
            return
        seen.add(resolved)
        found.append(resolved)

    # Vite / SPA: <script type="module" src="/src/main.tsx">
    for html_rel in ("index.html", "public/index.html"):
        html = root / html_rel
        if not html.is_file():
            continue
        try:
            text = html.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(
            r"""<script[^>]+src=["']([^"']+)["']""",
            text,
            flags=re.IGNORECASE,
        ):
            add(match.group(1))

    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            for key in ("main", "module", "browser"):
                val = data.get(key)
                if isinstance(val, str):
                    add(val)
            bin_field = data.get("bin")
            if isinstance(bin_field, str):
                add(bin_field)
            elif isinstance(bin_field, dict):
                for val in bin_field.values():
                    if isinstance(val, str):
                        add(val)
            for export_path in _package_export_paths(data.get("exports")):
                add(export_path)

    if (root / "package.json").is_file() or (root / "tsconfig.json").is_file():
        for cand in _JS_CONVENTIONAL_ENTRIES:
            add(cand)

    return found[:limit]


def _package_export_paths(exports: object) -> list[str]:
    """Flatten package.json ``exports`` values that look like file paths."""
    out: list[str] = []

    def walk(node: object, *, prefer_dot: bool = False) -> None:
        if isinstance(node, str):
            if not node.startswith(".") and "/" not in node and not node.endswith("*"):
                return
            out.append(node)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        # Prefer "." then import/require/default condition keys.
        if prefer_dot and "." in node:
            walk(node["."])
            return
        for key in (".", "import", "require", "default", "node", "browser"):
            if key in node:
                walk(node[key])
        for key, val in node.items():
            if key in {".", "import", "require", "default", "node", "browser", "types"}:
                continue
            if isinstance(key, str) and key.startswith("./") and not key.endswith("*"):
                walk(val)

    walk(exports, prefer_dot=True)
    return out


def _resolve_js_source_path(root: Path, raw: str) -> str | None:
    """Map a package/HTML path to an existing JS/TS source file under root."""
    text = raw.strip().split("?")[0].split("#")[0].strip()
    if not text or text.startswith("http://") or text.startswith("https://"):
        return None
    text = text.lstrip("/")
    if text.startswith("./"):
        text = text[2:]
    parts = Path(text).parts
    if any(part in _JS_SKIP_DIRS for part in parts):
        return None

    candidates: list[str] = [text]
    path = Path(text)
    if not path.suffix:
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            candidates.append(text + ext)
            candidates.append(f"{text}/index{ext}")
    elif path.suffix.lower() == ".js":
        # Prefer TypeScript sources when main points at emitted JS.
        stem = path.with_suffix("")
        for ext in (".ts", ".tsx", ".jsx", ".mjs"):
            candidates.append(stem.with_suffix(ext).as_posix())
        if path.name == "index.js":
            for ext in (".ts", ".tsx", ".jsx"):
                candidates.append((path.parent / f"index{ext}").as_posix())

    for rel in candidates:
        rel_posix = Path(rel).as_posix()
        full = root / rel_posix
        if not full.is_file():
            continue
        if full.suffix.lower() not in _JS_SOURCE_EXTS:
            continue
        if any(part in _JS_SKIP_DIRS for part in Path(rel_posix).parts):
            continue
        return rel_posix
    return None
