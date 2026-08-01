"""JS/TS entry detection from HTML, package.json, and framework conventions."""

from __future__ import annotations

import json
import re
from pathlib import Path

from codeview.entrypoints.models import EntryPointCandidate
from codeview.models import Confidence, EntryPointKind

_JS_SOURCE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_JS_SKIP_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".next",
    "coverage",
    "out",
    ".turbo",
    ".venv",
    "venv",
    "__pycache__",
}

# App bootstraps only — not every component named App.
_JS_BOOTSTRAP_ENTRIES = (
    "src/main.tsx",
    "src/main.ts",
    "src/main.jsx",
    "src/main.js",
    "src/index.tsx",
    "src/index.ts",
    "src/index.jsx",
    "src/index.js",
    "main.tsx",
    "main.ts",
    "main.jsx",
    "main.js",
    "index.tsx",
    "index.ts",
    "index.jsx",
    "index.js",
    "app/page.tsx",
    "app/page.jsx",
    "app/layout.tsx",
    "pages/_app.tsx",
    "pages/_app.jsx",
    "pages/_app.js",
    "pages/index.tsx",
    "pages/index.jsx",
    "pages/index.js",
    "src/pages/_app.tsx",
    "src/pages/_app.jsx",
    "src/pages/index.tsx",
    "src/pages/index.jsx",
    "server.ts",
    "server.js",
    "src/server.ts",
    "src/server.js",
)


class JavaScriptEntryDetector:
    name = "javascript"

    def supports(self, root: Path) -> bool:
        root = root.resolve()
        return (
            (root / "package.json").is_file()
            or (root / "tsconfig.json").is_file()
            or (root / "index.html").is_file()
            or (root / "src" / "main.tsx").is_file()
            or (root / "src" / "index.ts").is_file()
        )

    def detect(self, root: Path, *, limit: int = 40) -> list[EntryPointCandidate]:
        root = root.resolve()
        out: list[EntryPointCandidate] = []
        seen_paths: set[str] = set()

        def add(
            raw: str | None,
            *,
            category: EntryPointKind,
            source: str,
            confidence: Confidence,
            evidence: str,
            prefer_imports: bool = False,
            display: str | None = None,
        ) -> None:
            if not raw or len(out) >= limit:
                return
            resolved = resolve_js_source_path(root, raw)
            if not resolved or resolved in seen_paths:
                return
            seen_paths.add(resolved)
            out.append(
                EntryPointCandidate(
                    category=category,
                    display_name=display or Path(resolved).name,
                    source=source,
                    confidence=confidence,
                    path=resolved,
                    evidence=[evidence],
                    prefer_imports=prefer_imports,
                )
            )

        # 1) HTML module scripts — strongest SPA signal (Vite/React).
        for html_rel in ("index.html", "public/index.html"):
            html = root / html_rel
            if not html.is_file():
                continue
            try:
                text = html.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in re.finditer(
                r"""<script[^>]+src=["']([^"']+)["']""",
                text,
                flags=re.IGNORECASE,
            ):
                add(
                    match.group(1),
                    category=EntryPointKind.FRONTEND,
                    source="index.html module script",
                    confidence=Confidence.CONFIRMED,
                    evidence=f"{html_rel} → {match.group(1)}",
                    prefer_imports=True,
                )

        pkg = root / "package.json"
        data: dict = {}
        if pkg.is_file():
            try:
                loaded = json.loads(pkg.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                data = {}

        # 2) package.json bin — CLI executables.
        bin_field = data.get("bin")
        if isinstance(bin_field, str):
            add(
                bin_field,
                category=EntryPointKind.CLI,
                source="package.json bin",
                confidence=Confidence.CONFIRMED,
                evidence=f'bin = "{bin_field}"',
            )
        elif isinstance(bin_field, dict):
            for name, val in bin_field.items():
                if isinstance(val, str):
                    add(
                        val,
                        category=EntryPointKind.CLI,
                        source="package.json bin",
                        confidence=Confidence.CONFIRMED,
                        evidence=f'bin.{name} = "{val}"',
                        display=str(name),
                    )

        # 3) main/module — only when we lack a stronger HTML/bin root.
        if not out:
            for key in ("main", "module", "browser"):
                val = data.get(key)
                if isinstance(val, str):
                    add(
                        val,
                        category=EntryPointKind.MODULE,
                        source=f"package.json {key}",
                        confidence=Confidence.LIKELY,
                        evidence=f'{key} = "{val}"',
                        prefer_imports=True,
                    )

        # 4) Conventional bootstraps (skip App.tsx — that's usually a component).
        if (root / "package.json").is_file() or (root / "tsconfig.json").is_file():
            has_main = any(
                p.endswith(("/main.tsx", "/main.ts", "/main.jsx", "/main.js"))
                or p in {"main.tsx", "main.ts", "main.jsx", "main.js"}
                for p in seen_paths
            )
            for cand in _JS_BOOTSTRAP_ENTRIES:
                if has_main and Path(cand).stem == "index":
                    continue
                add(
                    cand,
                    category=EntryPointKind.FRONTEND,
                    source="conventional JS/TS entry",
                    confidence=Confidence.LIKELY,
                    evidence=cand,
                    prefer_imports=True,
                )

        # 5) package exports — library surface only when nothing else matched.
        if not out and data.get("exports") is not None:
            for export_path in _package_export_paths(data.get("exports")):
                add(
                    export_path,
                    category=EntryPointKind.LIBRARY,
                    source="package.json exports",
                    confidence=Confidence.LIKELY,
                    evidence=f"exports → {export_path}",
                )

        return out[:limit]


def find_js_ts_entry_files(root: Path, *, limit: int = 40) -> list[str]:
    """Compatibility helper: relative paths only."""
    return [
        c.path
        for c in JavaScriptEntryDetector().detect(root, limit=limit)
        if c.path
    ]


def resolve_js_source_path(root: Path, raw: str) -> str | None:
    """Map a package/HTML path to an existing JS/TS source file under root."""
    text = raw.strip().split("?")[0].split("#")[0].strip()
    if not text or text.startswith("http://") or text.startswith("https://"):
        return None
    text = text.lstrip("/")
    if text.startswith("./"):
        text = text[2:]
    parts = Path(text).parts
    if any(part in _JS_SKIP_DIRS for part in parts):
        return None

    candidates: list[str] = [text]
    path = Path(text)
    if not path.suffix:
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            candidates.append(text + ext)
            candidates.append(f"{text}/index{ext}")
    elif path.suffix.lower() == ".js":
        stem = path.with_suffix("")
        for ext in (".ts", ".tsx", ".jsx", ".mjs"):
            candidates.append(stem.with_suffix(ext).as_posix())
        if path.name == "index.js":
            for ext in (".ts", ".tsx", ".jsx"):
                candidates.append((path.parent / f"index{ext}").as_posix())

    for rel in candidates:
        rel_posix = Path(rel).as_posix()
        full = root / rel_posix
        if not full.is_file():
            continue
        if full.suffix.lower() not in _JS_SOURCE_EXTS:
            continue
        if any(part in _JS_SKIP_DIRS for part in Path(rel_posix).parts):
            continue
        return rel_posix
    return None


def _package_export_paths(exports: object) -> list[str]:
    out: list[str] = []

    def walk(node: object, *, prefer_dot: bool = False) -> None:
        if isinstance(node, str):
            if not node.startswith(".") and "/" not in node and not node.endswith("*"):
                return
            out.append(node)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if prefer_dot and "." in node:
            walk(node["."])
            return
        for key in (".", "import", "require", "default", "node", "browser"):
            if key in node:
                walk(node[key])
        for key, val in node.items():
            if key in {".", "import", "require", "default", "node", "browser", "types"}:
                continue
            if isinstance(key, str) and key.startswith("./") and not key.endswith("*"):
                walk(val)

    walk(exports, prefer_dot=True)
    return out
