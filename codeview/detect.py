from __future__ import annotations

from pathlib import Path

from codeview.fsutil import SKIP_DIR_NAMES

# Extension → provider name. Browse-only extensions have no provider.
PROVIDER_EXTENSIONS: dict[str, str] = {
    ".py": "jedi-python",
    ".java": "treesitter-java",
    ".scala": "treesitter-scala",
    ".sc": "treesitter-scala",
    ".c": "treesitter-cxx",
    ".cc": "treesitter-cxx",
    ".cpp": "treesitter-cxx",
    ".cxx": "treesitter-cxx",
    ".h": "treesitter-cxx",
    ".hh": "treesitter-cxx",
    ".hpp": "treesitter-cxx",
    ".hxx": "treesitter-cxx",
    ".cu": "treesitter-cxx",
    ".cuh": "treesitter-cxx",
}

# Shown in directory browse even without a semantic provider.
BROWSE_ONLY_EXTENSIONS: set[str] = {
    ".sh",
    ".bash",
    ".zsh",
    ".html",
    ".htm",
    ".g4",
    ".md",
    ".rs",
    ".go",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
}

LANG_TO_PROVIDER: dict[str, str] = {
    "python": "jedi-python",
    "java": "treesitter-java",
    "scala": "treesitter-scala",
    "c": "treesitter-cxx",
    "cpp": "treesitter-cxx",
    "cuda": "treesitter-cxx",
}


def language_for_extension(ext: str) -> str:
    e = ext.lower()
    if e == ".py":
        return "python"
    if e == ".java":
        return "java"
    if e in {".scala", ".sc"}:
        return "scala"
    if e in {".cu", ".cuh"}:
        return "cuda"
    if e == ".c":
        return "c"
    if e in {".h", ".hh", ".hpp", ".hxx", ".cc", ".cpp", ".cxx"}:
        return "cpp"
    if e == ".rs":
        return "rust"
    if e in {".sh", ".bash", ".zsh"}:
        return "shell"
    if e in {".html", ".htm"}:
        return "html"
    if e == ".g4":
        return "antlr"
    return "text"


def detect_providers(root: Path, *, sample_limit: int = 200_000) -> list[str]:
    """Return provider names present under root, ordered by importance.

    Prefer an existing SCIP index when present — Codeview should not reinvent indexing.
    """
    root = root.resolve()
    try:
        from codeview.providers.scip import find_scip_index

        if find_scip_index(root) is not None:
            return ["scip"]
    except Exception:
        pass

    found: set[str] = set()
    count = 0

    def consider(path: Path) -> None:
        nonlocal count
        if count >= sample_limit:
            return
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            return
        if not path.is_file():
            return
        provider = PROVIDER_EXTENSIONS.get(path.suffix.lower())
        if provider:
            found.add(provider)
            count += 1

    if (root / ".git").exists():
        import subprocess

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
                if count >= sample_limit and len(found) >= 4:
                    break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if not found:
        for path in root.rglob("*"):
            consider(path)
            if count >= sample_limit and found:
                break

    priority = ["treesitter-java", "treesitter-scala", "jedi-python", "treesitter-cxx"]
    return [name for name in priority if name in found] + sorted(found - set(priority))


def all_browse_extensions(provider_names: list[str] | None = None) -> set[str]:
    from codeview.providers import get_provider

    exts = set(BROWSE_ONLY_EXTENSIONS)
    names = provider_names or list({*PROVIDER_EXTENSIONS.values()})
    for name in names:
        try:
            provider = get_provider(name)
        except ValueError:
            continue
        exts |= provider.source_extensions() or {
            ext for ext, pname in PROVIDER_EXTENSIONS.items() if pname == name
        }
    return exts


def provider_name_for_path(rel_or_path: str) -> str | None:
    suffix = Path(rel_or_path).suffix.lower()
    return PROVIDER_EXTENSIONS.get(suffix)


def provider_name_for_symbol_language(language: str) -> str | None:
    return LANG_TO_PROVIDER.get((language or "").lower())
