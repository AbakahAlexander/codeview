from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from codeview.models import Location, Relation, RelationKind, Symbol, SymbolKind


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    column_index INTEGER NOT NULL,
    end_line INTEGER,
    end_column INTEGER,
    qualname TEXT NOT NULL,
    language TEXT NOT NULL,
    signature TEXT,
    docstring TEXT,
    container_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qualname ON symbols(qualname);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    path TEXT,
    line INTEGER,
    column_index INTEGER,
    meta_json TEXT NOT NULL DEFAULT '{}',
    lazy INTEGER NOT NULL DEFAULT 0,
    UNIQUE(kind, from_id, to_id, path, line, column_index)
);

CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_id, kind);
CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_id, kind);

CREATE TABLE IF NOT EXISTS saved_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS expand_cache (
    symbol_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(symbol_id, kind)
);
"""


class SymbolStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._symbols_by_id: dict[str, Symbol] | None = None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _invalidate_cache(self) -> None:
        self._symbols_by_id = None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def clear_index(self) -> None:
        with self._lock:
            self._invalidate_cache()
            self._conn.executescript(
                """
                DELETE FROM symbols;
                DELETE FROM relations;
                DELETE FROM expand_cache;
                """
            )
            self._conn.commit()

    def replace_symbols(self, symbols: Iterable[Symbol]) -> int:
        rows = [
            (
                s.id,
                s.name,
                s.kind.value if isinstance(s.kind, SymbolKind) else s.kind,
                s.location.path,
                s.location.line,
                s.location.column,
                s.location.end_line,
                s.location.end_column,
                s.qualname,
                s.language,
                s.signature,
                s.docstring,
                s.container_id,
            )
            for s in symbols
        ]
        with self._lock:
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO symbols(
                    id, name, kind, path, line, column_index, end_line, end_column,
                    qualname, language, signature, docstring, container_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._conn.commit()
            self._invalidate_cache()
            return len(rows)

    def add_relations(self, relations: Iterable[Relation], *, lazy: bool = False) -> int:
        rows = []
        for rel in relations:
            rows.append(
                (
                    rel.kind.value if isinstance(rel.kind, RelationKind) else rel.kind,
                    rel.from_id,
                    rel.to_id,
                    rel.location.path if rel.location else None,
                    rel.location.line if rel.location else None,
                    rel.location.column if rel.location else None,
                    json.dumps(rel.meta or {}),
                    1 if lazy else 0,
                )
            )
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO relations(
                    kind, from_id, to_id, path, line, column_index, meta_json, lazy
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def mark_expanded(self, symbol_id: str, kind: RelationKind | str) -> None:
        kind_value = kind.value if isinstance(kind, RelationKind) else kind
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO expand_cache(symbol_id, kind) VALUES(?, ?)
                ON CONFLICT(symbol_id, kind) DO UPDATE SET computed_at=CURRENT_TIMESTAMP
                """,
                (symbol_id, kind_value),
            )
            self._conn.commit()

    def is_expanded(self, symbol_id: str, kind: RelationKind | str) -> bool:
        kind_value = kind.value if isinstance(kind, RelationKind) else kind
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM expand_cache WHERE symbol_id = ? AND kind = ?",
                (symbol_id, kind_value),
            ).fetchone()
            return row is not None

    def get_symbol(self, symbol_id: str) -> Symbol | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM symbols WHERE id = ?", (symbol_id,)).fetchone()
            return self._row_to_symbol(row) if row else None

    def all_symbols(self) -> list[Symbol]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM symbols").fetchall()
            return [self._row_to_symbol(row) for row in rows]

    def symbols_by_id(self) -> dict[str, Symbol]:
        with self._lock:
            if self._symbols_by_id is None:
                rows = self._conn.execute("SELECT * FROM symbols").fetchall()
                self._symbols_by_id = {self._row_to_symbol(row).id: self._row_to_symbol(row) for row in rows}
            return self._symbols_by_id

    def symbols_in_path(self, path: str) -> list[Symbol]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM symbols WHERE path = ? ORDER BY line, column_index",
                (path,),
            ).fetchall()
            return [self._row_to_symbol(row) for row in rows]

    def search(self, query: str, limit: int = 50) -> list[Symbol]:
        q = query.strip()
        if not q:
            return []
        like = f"%{q}%"
        rows = self._conn.execute(
            """
            SELECT * FROM symbols
            WHERE name LIKE ? OR qualname LIKE ?
            ORDER BY
                CASE
                    WHEN name = ? THEN 0
                    WHEN name LIKE ? THEN 1
                    WHEN qualname LIKE ? THEN 2
                    ELSE 3
                END,
                length(qualname),
                name
            LIMIT ?
            """,
            (like, like, q, f"{q}%", f"%{q}%", limit),
        ).fetchall()
        return [self._row_to_symbol(row) for row in rows]

    def top_level(self, query: str | None = None, limit: int = 500) -> list[Symbol]:
        """Module-level classes/interfaces/functions (not methods/modules)."""
        q = (query or "").strip()
        if q:
            like = f"%{q}%"
            rows = self._conn.execute(
                """
                SELECT s.* FROM symbols s
                LEFT JOIN symbols c ON s.container_id = c.id
                WHERE s.kind IN ('class', 'function', 'interface')
                  AND (c.id IS NULL OR c.kind = 'module')
                  AND (s.name LIKE ? OR s.qualname LIKE ? OR s.path LIKE ?)
                ORDER BY s.path, s.line
                LIMIT ?
                """,
                (like, like, like, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT s.* FROM symbols s
                LEFT JOIN symbols c ON s.container_id = c.id
                WHERE s.kind IN ('class', 'function', 'interface')
                  AND (c.id IS NULL OR c.kind = 'module')
                ORDER BY s.path, s.line
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_symbol(row) for row in rows]

    def file_modules(self, limit: int = 500) -> list[Symbol]:
        """Source files from the index (SCIP documents), for a file-first tree."""
        rows = self._conn.execute(
            """
            SELECT * FROM symbols
            WHERE kind = 'module' AND signature = 'file'
            ORDER BY path
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._row_to_symbol(row) for row in rows]

    def relations_for(self, symbol_id: str, kind: RelationKind | str | None = None) -> list[dict[str, Any]]:
        if kind is None:
            rows = self._conn.execute(
                "SELECT * FROM relations WHERE from_id = ? ORDER BY kind, line",
                (symbol_id,),
            ).fetchall()
        else:
            kind_value = kind.value if isinstance(kind, RelationKind) else kind
            rows = self._conn.execute(
                "SELECT * FROM relations WHERE from_id = ? AND kind = ? ORDER BY line",
                (symbol_id, kind_value),
            ).fetchall()
        return [self._row_to_relation_dict(row) for row in rows]

    def relations_enriched(
        self,
        symbol_id: str,
        kind: RelationKind | str,
        *,
        limit: int = 80,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return relations joined with target symbols, plus total count before limit."""
        kind_value = kind.value if isinstance(kind, RelationKind) else kind
        with self._lock:
            total_row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM relations WHERE from_id = ? AND kind = ?",
                (symbol_id, kind_value),
            ).fetchone()
            total = int(total_row["c"]) if total_row else 0
            rows = self._conn.execute(
                """
                SELECT
                  r.kind AS rel_kind,
                  r.from_id,
                  r.to_id,
                  r.path AS rel_path,
                  r.line AS rel_line,
                  r.column_index AS rel_column,
                  r.meta_json,
                  r.lazy,
                  s.id AS s_id,
                  s.name AS s_name,
                  s.kind AS s_kind,
                  s.path AS s_path,
                  s.line AS s_line,
                  s.column_index AS s_column,
                  s.end_line AS s_end_line,
                  s.end_column AS s_end_column,
                  s.qualname AS s_qualname,
                  s.language AS s_language,
                  s.signature AS s_signature,
                  s.docstring AS s_docstring,
                  s.container_id AS s_container_id
                FROM relations r
                LEFT JOIN symbols s ON s.id = r.to_id
                WHERE r.from_id = ? AND r.kind = ?
                ORDER BY r.line
                LIMIT ?
                """,
                (symbol_id, kind_value, limit),
            ).fetchall()
            enriched: list[dict[str, Any]] = []
            for row in rows:
                rel: dict[str, Any] = {
                    "kind": row["rel_kind"],
                    "from_id": row["from_id"],
                    "to_id": row["to_id"],
                    "location": (
                        {
                            "path": row["rel_path"],
                            "line": row["rel_line"],
                            "column": row["rel_column"],
                        }
                        if row["rel_path"] is not None
                        else None
                    ),
                    "meta": json.loads(row["meta_json"] or "{}"),
                    "lazy": bool(row["lazy"]),
                    "to_symbol": None,
                }
                if row["s_id"]:
                    rel["to_symbol"] = Symbol(
                        id=row["s_id"],
                        name=row["s_name"],
                        kind=SymbolKind(row["s_kind"]),
                        location=Location(
                            path=row["s_path"],
                            line=row["s_line"],
                            column=row["s_column"],
                            end_line=row["s_end_line"],
                            end_column=row["s_end_column"],
                        ),
                        qualname=row["s_qualname"],
                        language=row["s_language"],
                        signature=row["s_signature"],
                        docstring=row["s_docstring"],
                        container_id=row["s_container_id"],
                    ).to_dict()
                enriched.append(rel)
            return enriched, total

    def save_path(self, name: str, steps: list[dict[str, Any]]) -> int:
        cur = self._conn.execute(
            "INSERT INTO saved_paths(name, steps_json) VALUES(?, ?)",
            (name, json.dumps(steps)),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_saved_paths(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, name, steps_json, created_at FROM saved_paths ORDER BY id DESC"
        ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "steps": json.loads(row["steps_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def delete_saved_path(self, path_id: int) -> None:
        self._conn.execute("DELETE FROM saved_paths WHERE id = ?", (path_id,))
        self._conn.commit()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            symbol_count = self._conn.execute("SELECT COUNT(*) AS c FROM symbols").fetchone()["c"]
            file_count = self._conn.execute("SELECT COUNT(DISTINCT path) AS c FROM symbols").fetchone()["c"]
            meta_files = self.get_meta("file_count")
            return {
                "root": self.get_meta("root"),
                "provider": self.get_meta("provider"),
                "language": self.get_meta("language"),
                "symbol_count": symbol_count,
                "file_count": int(meta_files) if meta_files else file_count,
                "indexed_at": self.get_meta("indexed_at"),
                "index_mode": self.get_meta("index_mode") or "eager",
            }

    @staticmethod
    def _row_to_symbol(row: sqlite3.Row) -> Symbol:
        return Symbol(
            id=row["id"],
            name=row["name"],
            kind=SymbolKind(row["kind"]),
            location=Location(
                path=row["path"],
                line=row["line"],
                column=row["column_index"],
                end_line=row["end_line"],
                end_column=row["end_column"],
            ),
            qualname=row["qualname"],
            language=row["language"],
            signature=row["signature"],
            docstring=row["docstring"],
            container_id=row["container_id"],
        )

    @staticmethod
    def _row_to_relation_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "kind": row["kind"],
            "from_id": row["from_id"],
            "to_id": row["to_id"],
            "location": (
                {
                    "path": row["path"],
                    "line": row["line"],
                    "column": row["column_index"],
                }
                if row["path"] is not None
                else None
            ),
            "meta": json.loads(row["meta_json"] or "{}"),
            "lazy": bool(row["lazy"]),
        }
