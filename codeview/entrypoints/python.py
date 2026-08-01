"""Python packaging and ``__main__`` entry detection."""

from __future__ import annotations

import re
from pathlib import Path

from codeview.entrypoints.models import EntryPointCandidate
from codeview.models import Confidence, EntryPointKind

_SKIP = {".git", "venv", ".venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}
_MAIN_GUARD = re.compile(r"""if\s+__name__\s*==\s*['\"]__main__['\"]""")


class PythonEntryDetector:
    name = "python"

    def supports(self, root: Path) -> bool:
        root = root.resolve()
        if (root / "pyproject.toml").is_file() or (root / "setup.cfg").is_file():
            return True
        return any(root.rglob("*.py"))

    def detect(self, root: Path, *, limit: int = 40) -> list[EntryPointCandidate]:
        root = root.resolve()
        out: list[EntryPointCandidate] = []
        seen: set[str] = set()

        def add(c: EntryPointCandidate) -> None:
            key = f"{c.module}:{c.attr}:{c.path}:{c.command_name}"
            if key in seen or len(out) >= limit:
                return
            seen.add(key)
            out.append(c)

        for cmd, module, attr in parse_project_scripts(root):
            rel = module.replace(".", "/") + ".py"
            add(
                EntryPointCandidate(
                    category=EntryPointKind.CLI,
                    display_name=cmd,
                    source="pyproject.toml [project.scripts]",
                    confidence=Confidence.CONFIRMED,
                    path=rel if (root / rel).is_file() else None,
                    module=module,
                    attr=attr,
                    command_name=cmd,
                    evidence=[f"{cmd} = {module}:{attr}"],
                )
            )

        for rel in find_dunder_main_files(root, limit=limit):
            is_pkg_main = Path(rel).name == "__main__.py"
            add(
                EntryPointCandidate(
                    category=EntryPointKind.MODULE,
                    display_name=rel,
                    source="__main__ guard" if not is_pkg_main else "__main__.py",
                    confidence=Confidence.CONFIRMED,
                    path=rel,
                    attr=None if is_pkg_main else "main",
                    evidence=[rel],
                )
            )

        return out[:limit]


def parse_project_scripts(root: Path) -> list[tuple[str, str, str]]:
    """Return ``(command_name, module, attr)`` from ``pyproject.toml`` scripts."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore

    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception:
            data = {}
        scripts: dict = {}
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
    text = target.strip().strip("'\"")
    if ":" not in text:
        return None
    module, attr = text.split(":", 1)
    module, attr = module.strip(), attr.strip()
    if not module or not attr:
        return None
    name = attr.split(".")[-1]
    return module, name


def _parse_scripts_fallback(text: str) -> list[tuple[str, str, str]]:
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
    root = root.resolve()
    found: list[str] = []
    seen: set[str] = set()

    def add(rel: str) -> None:
        if rel in seen:
            return
        seen.add(rel)
        found.append(rel)

    for path in root.rglob("__main__.py"):
        if any(part.startswith(".") or part in _SKIP for part in path.parts):
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
        if any(part.startswith(".") or part in _SKIP for part in path.parts):
            continue
        if "site-packages" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _MAIN_GUARD.search(text):
            try:
                add(path.relative_to(root).as_posix())
            except ValueError:
                pass
            if len(found) >= limit:
                break
    return found
