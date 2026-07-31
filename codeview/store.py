from __future__ import annotations

import json
import sqlite3
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
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def clear_index(self) -> None:
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
        row = self._conn.execute(
            "SELECT 1 FROM expand_cache WHERE symbol_id = ? AND kind = ?",
            (symbol_id, kind_value),
        ).fetchone()
        return row is not None

    def get_symbol(self, symbol_id: str) -> Symbol | None:
        row = self._conn.execute("SELECT * FROM symbols WHERE id = ?", (symbol_id,)).fetchone()
        return self._row_to_symbol(row) if row else None

    def all_symbols(self) -> list[Symbol]:
        rows = self._conn.execute("SELECT * FROM symbols").fetchall()
        return [self._row_to_symbol(row) for row in rows]

    def symbols_by_id(self) -> dict[str, Symbol]:
        return {s.id: s for s in self.all_symbols()}

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
        """Module-level classes and functions (not methods/modules)."""
        q = (query or "").strip()
        if q:
            like = f"%{q}%"
            rows = self._conn.execute(
                """
                SELECT s.* FROM symbols s
                LEFT JOIN symbols c ON s.container_id = c.id
                WHERE s.kind IN ('class', 'function')
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
                WHERE s.kind IN ('class', 'function')
                  AND (c.id IS NULL OR c.kind = 'module')
                ORDER BY s.path, s.line
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
        symbol_count = self._conn.execute("SELECT COUNT(*) AS c FROM symbols").fetchone()["c"]
        file_count = self._conn.execute("SELECT COUNT(DISTINCT path) AS c FROM symbols").fetchone()["c"]
        return {
            "root": self.get_meta("root"),
            "provider": self.get_meta("provider"),
            "language": self.get_meta("language"),
            "symbol_count": symbol_count,
            "file_count": file_count,
            "indexed_at": self.get_meta("indexed_at"),
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
