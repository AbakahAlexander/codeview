"""Detect program entry points from packaging metadata and ``__main__`` hooks."""

from __future__ import annotations

import re
from pathlib import Path


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
