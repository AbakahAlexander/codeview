"""Pre-resolution entry candidates produced by detectors."""

from __future__ import annotations

from dataclasses import dataclass, field

from codeview.models import Confidence, EntryPointKind


@dataclass(slots=True)
class EntryPointCandidate:
    """A detected entry resource before SCIP symbol mapping."""

    category: EntryPointKind
    display_name: str
    source: str
    confidence: Confidence
    path: str | None = None
    module: str | None = None
    attr: str | None = None
    command_name: str | None = None
    evidence: list[str] = field(default_factory=list)
    prefer_imports: bool = False
