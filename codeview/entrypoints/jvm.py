"""JVM library API surface candidates (path conventions; resolved via SCIP)."""

from __future__ import annotations

from pathlib import Path

from codeview.entrypoints.models import EntryPointCandidate
from codeview.models import Confidence, EntryPointKind

_API_GLOBS = (
    "api/src/main/java",
    "api/src/main/kotlin",
    "src/main/java",
    "lib/src/main/java",
)


class JvmEntryDetector:
    name = "jvm"

    def supports(self, root: Path) -> bool:
        root = root.resolve()
        if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
            return True
        if (root / "pom.xml").is_file() or (root / "gradlew").is_file():
            return True
        return any((root / prefix).is_dir() for prefix in _API_GLOBS)

    def detect(self, root: Path, *, limit: int = 40) -> list[EntryPointCandidate]:
        """Emit directory markers; symbol resolution happens against the SCIP index."""
        root = root.resolve()
        # Prefer api/ over generic src/main when both exist.
        for prefix in _API_GLOBS:
            base = root / prefix
            if not base.is_dir():
                continue
            return [
                EntryPointCandidate(
                    category=EntryPointKind.LIBRARY,
                    display_name=prefix,
                    source="JVM public API tree",
                    confidence=Confidence.LIKELY,
                    path=prefix,
                    evidence=[prefix],
                )
            ]
        return []
