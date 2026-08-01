"""CMake / native executable source detection."""

from __future__ import annotations

import re
from pathlib import Path

from codeview.entrypoints.models import EntryPointCandidate
from codeview.models import Confidence, EntryPointKind


class NativeEntryDetector:
    name = "native"

    def supports(self, root: Path) -> bool:
        return (root.resolve() / "CMakeLists.txt").is_file()

    def detect(self, root: Path, *, limit: int = 40) -> list[EntryPointCandidate]:
        root = root.resolve()
        out: list[EntryPointCandidate] = []
        for rel in parse_cmake_executable_sources(root, limit=limit):
            out.append(
                EntryPointCandidate(
                    category=EntryPointKind.NATIVE_MAIN,
                    display_name=Path(rel).name,
                    source="CMakeLists.txt add_executable",
                    confidence=Confidence.CONFIRMED,
                    path=rel,
                    attr="main",
                    evidence=[f"add_executable → {rel}"],
                )
            )
        return out[:limit]


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
