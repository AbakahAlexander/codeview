from codeview.providers.base import GraphProvider
from codeview.providers.jedi_python import JediPythonProvider
from codeview.providers.treesitter_cxx import TreeSitterCxxProvider
from codeview.providers.treesitter_java import TreeSitterJavaProvider
from codeview.providers.treesitter_scala import TreeSitterScalaProvider

PROVIDERS: dict[str, type[GraphProvider]] = {
    JediPythonProvider.name: JediPythonProvider,
    TreeSitterCxxProvider.name: TreeSitterCxxProvider,
    TreeSitterJavaProvider.name: TreeSitterJavaProvider,
    TreeSitterScalaProvider.name: TreeSitterScalaProvider,
}


def get_provider(name: str) -> GraphProvider:
    try:
        cls = PROVIDERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unknown provider {name!r}. Available: {known}") from exc
    return cls()


def list_providers() -> list[dict[str, object]]:
    return [
        {"name": cls.name, "languages": list(cls.languages)}
        for cls in PROVIDERS.values()
    ]
