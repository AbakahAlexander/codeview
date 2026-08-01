"""Entry-point detector protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from codeview.entrypoints.models import EntryPointCandidate


class EntryPointDetector(Protocol):
    name: str

    def supports(self, root: Path) -> bool:
        """True when this detector should run for ``root``."""

    def detect(self, root: Path, *, limit: int = 40) -> list[EntryPointCandidate]:
        """Return ranked candidates (best first)."""
