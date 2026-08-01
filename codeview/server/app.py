from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from codeview.providers import list_providers
from codeview.service import ExplorerService, default_db_path


_FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
STATIC_DIR = _FRONTEND / "static" if (_FRONTEND / "static").is_dir() else Path(__file__).parent / "static"
TEMPLATE_DIR = _FRONTEND / "templates" if (_FRONTEND / "templates").is_dir() else Path(__file__).parent / "templates"


class IndexRequest(BaseModel):
    path: str
    provider: str = "auto"
    db: str | None = None


class ExpandRequest(BaseModel):
    symbol_id: str
    kind: str


class SavePathRequest(BaseModel):
    name: str
    steps: list[dict] = Field(default_factory=list)


def create_app(service: ExplorerService | None = None) -> FastAPI:
    service = service or ExplorerService()
    app = FastAPI(title="Codeview", version="0.1.0")
    app.state.service = service

    @app.get("/", response_class=HTMLResponse)
    def home() -> HTMLResponse:
        html = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/api/health")
    def health() -> dict:
        store = service.store
        return {
            "ok": True,
            "has_index": store is not None,
            "stats": store.stats() if store else None,
            "providers": list_providers(),
        }

    @app.get("/api/providers")
    def providers() -> list[dict]:
        return list_providers()

    @app.post("/api/index")
    def index_repo(body: IndexRequest) -> dict:
        root = Path(body.path).expanduser()
        if not root.exists():
            raise HTTPException(status_code=400, detail=f"Path does not exist: {root}")
        db = Path(body.db).expanduser() if body.db else default_db_path(root)
        try:
            status = service.prepare_serve(root, db_path=db)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": status, "db": str(db)}

    @app.post("/api/open")
    def open_db(body: dict) -> dict:
        db = Path(str(body.get("db", ""))).expanduser()
        if not db.exists():
            raise HTTPException(status_code=404, detail=f"Database not found: {db}")
        store = service.open(db)
        return {"stats": store.stats(), "db": str(db)}

    @app.get("/api/stats")
    def stats() -> dict:
        store = service.store
        if not store:
            return {"has_index": False, **service.index_status()}
        data = store.stats()
        if store.get_meta("file_count") and not data.get("file_count"):
            data["file_count"] = int(store.get_meta("file_count") or 0)
        data["index_mode"] = store.get_meta("index_mode") or "eager"
        data["index_status"] = service.index_status()
        return {"has_index": True, **data, "db": str(service.db_path)}

    @app.get("/api/index-status")
    def index_status() -> dict:
        return service.index_status()

    @app.get("/api/search")
    def search(q: str, limit: int = 40) -> dict:
        store = service.store
        if not store:
            raise HTTPException(status_code=400, detail="No index loaded")
        if store.get_meta("index_mode") == "lazy":
            hits = service.search_lazy(q, limit=limit)
        else:
            hits = store.search(q, limit=limit)
        return {"query": q, "results": [h.to_dict() for h in hits]}

    @app.get("/api/tree")
    def tree(q: str = "", path: str = "", limit: int = 2000) -> dict:
        store = service.store
        if not store:
            raise HTTPException(status_code=400, detail="No index loaded")

        mode = store.get_meta("index_mode") or "eager"
        if mode == "lazy":
            if q.strip():
                hits = service.search_lazy(q.strip(), limit=min(limit, 80))
                return {"results": [s.to_dict() for s in hits], "mode": "search", "truncated": False}
            children = service.browse(path)
            # Persist so expand/source can resolve ids.
            if children:
                service.upsert_ephemeral(children)
            return {
                "results": [s.to_dict() for s in children],
                "mode": "browse",
                "path": path or ".",
                "truncated": False,
            }

        if mode == "hybrid" and path.strip():
            children = service.browse(path)
            if children:
                service.upsert_ephemeral(children)
            return {
                "results": [s.to_dict() for s in children],
                "mode": "browse",
                "path": path or ".",
                "truncated": False,
            }

        effective = limit if q.strip() else min(limit, 400)
        if q.strip():
            # Substring search over indexed symbol names (SQLite LIKE — not Elasticsearch).
            hits = [
                s
                for s in store.search(q.strip(), limit=effective)
                if s.kind.value in {"class", "function", "method", "interface"}
            ]
            return {"results": [s.to_dict() for s in hits], "mode": "search", "truncated": len(hits) >= effective}
        symbols = store.top_level(query=None, limit=effective)
        return {"results": [s.to_dict() for s in symbols], "mode": "symbols", "truncated": len(symbols) >= effective}

    @app.get("/api/symbol/{symbol_id}")
    def get_symbol(symbol_id: str) -> dict:
        store = service.store
        if not store:
            raise HTTPException(status_code=400, detail="No index loaded")
        symbol = store.get_symbol(symbol_id)
        if not symbol:
            raise HTTPException(status_code=404, detail="Symbol not found")
        structural = store.relations_for(symbol_id)
        return {
            "symbol": symbol.to_dict(),
            "relations": structural,
            "expanded": {
                kind: store.is_expanded(symbol_id, kind)
                for kind in (
                    "calls",
                    "called_by",
                    "references",
                    "parent_class",
                    "child_class",
                    "overrides",
                    "overridden_by",
                    "contains",
                    "contained_in",
                    "implements",
                    "implemented_by",
                )
            },
        }

    @app.post("/api/expand")
    def expand(body: ExpandRequest) -> dict:
        if not service.store:
            raise HTTPException(status_code=400, detail="No index loaded")
        try:
            relations, total = service.expand(body.symbol_id, body.kind, limit=80)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "symbol_id": body.symbol_id,
            "kind": body.kind,
            "relations": relations,
            "total": total,
            "truncated": total > len(relations),
        }

    @app.get("/api/source/{symbol_id}")
    def source(symbol_id: str) -> dict:
        if not service.store:
            raise HTTPException(status_code=400, detail="No index loaded")
        try:
            snippet = service.source(symbol_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return snippet.to_dict()

    @app.get("/api/saved-paths")
    def saved_paths() -> dict:
        store = service.store
        if not store:
            return {"paths": []}
        return {"paths": store.list_saved_paths()}

    @app.post("/api/saved-paths")
    def save_path(body: SavePathRequest) -> dict:
        store = service.store
        if not store:
            raise HTTPException(status_code=400, detail="No index loaded")
        path_id = store.save_path(body.name, body.steps)
        return {"id": path_id}

    @app.delete("/api/saved-paths/{path_id}")
    def delete_path(path_id: int) -> dict:
        store = service.store
        if not store:
            raise HTTPException(status_code=400, detail="No index loaded")
        store.delete_saved_path(path_id)
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app
