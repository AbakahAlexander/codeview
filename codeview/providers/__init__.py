from pathlib import Path

from codeview.providers.base import GraphProvider
from codeview.providers.scip import ScipProvider

PROVIDERS: dict[str, type[GraphProvider]] = {
    ScipProvider.name: ScipProvider,
}


def get_provider(name: str, **kwargs) -> GraphProvider:
    try:
        cls = PROVIDERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unknown provider {name!r}. Available: {known}") from exc
    if name == "scip" and kwargs.get("scip_path"):
        return ScipProvider(index_path=Path(kwargs["scip_path"]))
    return cls()


def list_providers() -> list[dict[str, object]]:
    return [
        {
            "name": cls.name,
            "languages": list(cls.languages),
            "owns_indexing": getattr(cls, "owns_indexing", True),
        }
        for cls in PROVIDERS.values()
    ]
