"""JVM entry detection — execution roots only (``main``), not public API trees."""

from __future__ import annotations

from pathlib import Path

from codeview.entrypoints.models import EntryPointCandidate
from codeview.models import Confidence, EntryPointKind

_MAIN_MARKERS = (
    "public static void main",
    "fun main(",
    "def main(args: Array[String])",
)


class JvmEntryDetector:
    name = "jvm"

    def supports(self, root: Path) -> bool:
        root = root.resolve()
        if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
            return True
        if (root / "pom.xml").is_file() or (root / "gradlew").is_file():
            return True
        return (root / "src" / "main" / "java").is_dir() or (
            root / "src" / "main" / "kotlin"
        ).is_dir()

    def detect(self, root: Path, *, limit: int = 40) -> list[EntryPointCandidate]:
        """Find source files that declare a program ``main``."""
        root = root.resolve()
        out: list[EntryPointCandidate] = []
        seen: set[str] = set()
        for pattern in ("**/*.java", "**/*.kt", "**/*.kts", "**/*.scala"):
            for path in root.glob(pattern):
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    continue
                if any(
                    part.startswith(".") or part in {"build", "out", "target", "node_modules"}
                    for part in rel.parts
                ):
                    continue
                if "test" in {p.lower() for p in rel.parts}:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if not any(marker in text for marker in _MAIN_MARKERS):
                    continue
                rel_s = rel.as_posix()
                if rel_s in seen:
                    continue
                seen.add(rel_s)
                out.append(
                    EntryPointCandidate(
                        category=EntryPointKind.NATIVE_MAIN,
                        display_name=path.stem,
                        source="JVM main()",
                        confidence=Confidence.CONFIRMED,
                        path=rel_s,
                        attr="main",
                        evidence=[rel_s],
                    )
                )
                if len(out) >= limit:
                    return out
        return out
