"""SQLite persistence (WAL): requests, scheduled jobs, sources, and segment
index in disk mode. In volatile mode segments are not indexed."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    activated_at REAL,
    finished_at REAL,
    result_path TEXT,
    error TEXT,
    stats TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    request_id TEXT PRIMARY KEY,
    due_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    key TEXT PRIMARY KEY,
    template TEXT NOT NULL,
    selectors TEXT NOT NULL,
    volatile INTEGER NOT NULL DEFAULT 0,
    state TEXT
);
CREATE TABLE IF NOT EXISTS segments (
    source_key TEXT NOT NULL,
    track TEXT NOT NULL,
    seq INTEGER NOT NULL,
    uri TEXT NOT NULL,
    extinf REAL NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    fetched_wall REAL NOT NULL,
    PRIMARY KEY (source_key, track, seq)
);
"""


def _serialize_spec(spec: dict[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, default=str)


class Store:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- requests -----------------------------------------------------------
    def save_request(self, request_id: str, spec: dict[str, Any], state: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO requests (id, spec, state, created_at) VALUES (?, ?, ?, ?)",
                (request_id, _serialize_spec(spec), state, time.time()),
            )

    def update_request(
        self,
        request_id: str,
        *,
        state: str | None = None,
        activated_at: float | None = None,
        finished_at: float | None = None,
        result_path: str | None = None,
        error: str | None = None,
        clear_error: bool = False,
        stats: dict[str, Any] | None = None,
    ) -> None:
        sets: list[str] = []
        args: list[Any] = []
        if state is not None:
            sets.append("state = ?")
            args.append(state)
        if activated_at is not None:
            sets.append("activated_at = ?")
            args.append(activated_at)
        if finished_at is not None:
            sets.append("finished_at = ?")
            args.append(finished_at)
        if result_path is not None:
            sets.append("result_path = ?")
            args.append(result_path)
        if error is not None:
            sets.append("error = ?")
            args.append(error)
        if clear_error:
            sets.append("error = NULL")
        if stats is not None:
            sets.append("stats = ?")
            args.append(_serialize_spec(stats))
        if not sets:
            return
        args.append(request_id)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE requests SET {', '.join(sets)} WHERE id = ?", args)

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if not row:
            return None
        columns = [c[0] for c in self._conn.execute("SELECT * FROM requests LIMIT 0").description]
        return dict(zip(columns, row))

    def list_requests(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM requests ORDER BY created_at DESC").fetchall()
        columns = [c[0] for c in self._conn.execute("SELECT * FROM requests LIMIT 0").description]
        return [dict(zip(columns, row)) for row in rows]

    # -- jobs ---------------------------------------------------------------
    def save_job(self, request_id: str, due_at: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO jobs (request_id, due_at) VALUES (?, ?)", (request_id, due_at))

    def delete_job(self, request_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM jobs WHERE request_id = ?", (request_id,))

    def list_jobs(self) -> list[tuple[str, float]]:
        with self._lock:
            rows = self._conn.execute("SELECT request_id, due_at FROM jobs ORDER BY due_at").fetchall()
        return [(r[0], r[1]) for r in rows]

    # -- sources ------------------------------------------------------------
    def save_source(self, key: str, template: dict[str, Any], selectors: dict[str, Any], volatile: bool, state: str = "active") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sources (key, template, selectors, volatile, state) VALUES (?, ?, ?, ?, ?)",
                (key, _serialize_spec(template), _serialize_spec(selectors), int(volatile), state),
            )

    def update_source_state(self, key: str, state: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE sources SET state = ? WHERE key = ?", (state, key))

    # -- segments (disk mode) -----------------------------------------------
    def save_segment(
        self,
        source_key: str,
        track: str,
        seq: int,
        uri: str,
        extinf: float,
        path: str,
        size: int,
        fetched_wall: float,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO segments (source_key, track, seq, uri, extinf, path, size, fetched_wall)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (source_key, track, seq, uri, extinf, path, size, fetched_wall),
            )

    def get_segment_seqs(self, source_key: str, track: str) -> set[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq FROM segments WHERE source_key = ? AND track = ?", (source_key, track)
            ).fetchall()
        return {r[0] for r in rows}

    def get_segment_row(self, source_key: str, track: str, seq: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM segments WHERE source_key = ? AND track = ? AND seq = ?",
                (source_key, track, seq),
            ).fetchone()
        if not row:
            return None
        columns = [c[0] for c in self._conn.execute("SELECT * FROM segments LIMIT 0").description]
        return dict(zip(columns, row))

    def get_segments(self, source_key: str, track: str, since_seq: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if since_seq is None:
                rows = self._conn.execute(
                    "SELECT * FROM segments WHERE source_key = ? AND track = ? ORDER BY seq",
                    (source_key, track),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM segments WHERE source_key = ? AND track = ? AND seq > ? ORDER BY seq",
                    (source_key, track, since_seq),
                ).fetchall()
        columns = [c[0] for c in self._conn.execute("SELECT * FROM segments LIMIT 0").description]
        return [dict(zip(columns, row)) for row in rows]

    def delete_source_segments(self, source_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM segments WHERE source_key = ?", (source_key,))

    def segment_bytes_by_source(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_key, SUM(size) FROM segments GROUP BY source_key"
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows if r[1]}

    def delete_segment(self, source_key: str, track: str, seq: int) -> dict[str, Any] | None:
        """Deletes the segment row and returns its path (for file removal)."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT path, size FROM segments WHERE source_key = ? AND track = ? AND seq = ?",
                (source_key, track, seq),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM segments WHERE source_key = ? AND track = ? AND seq = ?",
                (source_key, track, seq),
            )
        return {"path": row[0], "size": row[1]}
