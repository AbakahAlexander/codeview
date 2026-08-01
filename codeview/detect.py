from __future__ import annotations

from pathlib import Path

from codeview.fsutil import path_is_skipped

# Extensions used for directory browse / language labels (not heuristic parsers).
SOURCE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".java": "java",
    ".scala": "scala",
    ".sc": "scala",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "c",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".cu": "cuda",
    ".cuh": "cuda",
    ".rs": "rust",
    ".go": "go",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rb": "ruby",
    ".cs": "csharp",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".html": "html",
    ".htm": "html",
    ".md": "text",
}


def language_for_extension(ext: str) -> str:
    return SOURCE_EXTENSIONS.get(ext.lower(), "text")


def detect_providers(root: Path, *, sample_limit: int = 200_000) -> list[str]:
    """Auto mode is SCIP-only — Codeview generates or consumes a precise index."""
    del sample_limit  # detection lives in scip_index; auto always means scip.
    root = root.resolve()
    if not root.is_dir():
        return []
    # Presence of any known source file → scip pipeline.
    for path in root.rglob("*"):
        if path_is_skipped(path) or not path.is_file():
            continue
        if path.suffix.lower() in SOURCE_EXTENSIONS:
            return ["scip"]
        if path.name == "index.scip":
            return ["scip"]
    try:
        from codeview.providers.scip import find_scip_index

        if find_scip_index(root) is not None:
            return ["scip"]
    except Exception:
        pass
    return ["scip"]


def all_browse_extensions(provider_names: list[str] | None = None) -> set[str]:
    del provider_names
    return set(SOURCE_EXTENSIONS.keys())


def provider_name_for_path(rel_or_path: str) -> str | None:
    if Path(rel_or_path).suffix.lower() in SOURCE_EXTENSIONS:
        return "scip"
    return None


def provider_name_for_symbol_language(language: str) -> str | None:
    if language:
        return "scip"
    return None
