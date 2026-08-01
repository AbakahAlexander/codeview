from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class SymbolKind(str, Enum):
    MODULE = "module"
    DIRECTORY = "directory"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    PARAMETER = "parameter"
    PROPERTY = "property"
    INTERFACE = "interface"
    UNKNOWN = "unknown"


class RelationKind(str, Enum):
    CALLS = "calls"
    CALLED_BY = "called_by"
    REFERENCES = "references"
    REFERENCED_BY = "referenced_by"
    PARENT_CLASS = "parent_class"
    CHILD_CLASS = "child_class"
    IMPLEMENTS = "implements"
    IMPLEMENTED_BY = "implemented_by"
    OVERRIDES = "overrides"
    OVERRIDDEN_BY = "overridden_by"
    CONTAINS = "contains"
    CONTAINED_IN = "contained_in"


@dataclass(slots=True)
class Location:
    path: str
    line: int
    column: int
    end_line: int | None = None
    end_column: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Symbol:
    id: str
    name: str
    kind: SymbolKind
    location: Location
    qualname: str
    language: str = "python"
    signature: str | None = None
    docstring: str | None = None
    container_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind.value if isinstance(self.kind, SymbolKind) else self.kind,
            "location": self.location.to_dict(),
            "qualname": self.qualname,
            "language": self.language,
            "signature": self.signature,
            "docstring": self.docstring,
            "container_id": self.container_id,
        }


@dataclass(slots=True)
class Relation:
    kind: RelationKind
    from_id: str
    to_id: str
    location: Location | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value if isinstance(self.kind, RelationKind) else self.kind,
            "from_id": self.from_id,
            "to_id": self.to_id,
            "location": self.location.to_dict() if self.location else None,
            "meta": self.meta,
        }


@dataclass(slots=True)
class SourceSnippet:
    path: str
    start_line: int
    end_line: int
    text: str
    highlight_line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class IndexStats:
    root: str
    provider: str
    language: str
    symbol_count: int
    file_count: int
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EntryPointKind(str, Enum):
    CLI = "cli"
    FRONTEND = "frontend"
    LIBRARY = "library"
    NATIVE_MAIN = "native_main"
    MODULE = "module"
    INFERRED = "inferred"


class Confidence(str, Enum):
    CONFIRMED = "confirmed"
    LIKELY = "likely"
    INFERRED = "inferred"


@dataclass(slots=True)
class EntryPoint:
    """Resolved program root: detection evidence + optional SCIP symbol."""

    category: EntryPointKind
    display_name: str
    source: str
    confidence: Confidence
    symbol_id: str | None = None
    target: str | None = None
    evidence: list[str] = field(default_factory=list)
    symbol: Symbol | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "display_name": self.display_name,
            "source": self.source,
            "confidence": self.confidence.value,
            "symbol_id": self.symbol_id,
            "target": self.target,
            "evidence": list(self.evidence),
            "symbol": self.symbol.to_dict() if self.symbol else None,
        }
