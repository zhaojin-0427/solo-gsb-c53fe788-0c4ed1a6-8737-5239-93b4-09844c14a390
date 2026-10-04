"""SQLite persistence layer.

The database holds two kinds of data:

1. An append-only ``events`` journal — the source of truth for every ruling.
   Triggers forbid UPDATE/DELETE on both ``events`` and ``detections``
   ("已发放行和事件记录不可追溯改写").
2. Materialised runtime state (active routes, section states, resource locks)
   which is rebuilt by replaying the journal on every startup, so a restart
   still shows the current locking and can replay rulings.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

_DB_PATH: Optional[str] = None
_write_lock = threading.RLock()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA temp_store=MEMORY;
PRAGMA cache_size=-65536;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Imported yard definition (at most one row, id always 1) ----------------
CREATE TABLE IF NOT EXISTS station (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    yard_id    TEXT,
    spec       TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

-- Append-only ruling journal ---------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,           -- see EVENT_KIND_* in interlocking
    payload TEXT NOT NULL            -- JSON
);

-- Every accepted detector report (append-only). Application state is
-- derived from the applied-sequence cursor: seq <= applied_seq is APPLIED,
-- otherwise BUFFERED — the row itself is never mutated.
CREATE TABLE IF NOT EXISTS detections (
    seq         INTEGER PRIMARY KEY,          -- 全站连续序号, from 1
    content     TEXT NOT NULL,                 -- canonical JSON
    sha256      TEXT NOT NULL,
    received_at TEXT NOT NULL
);

-- Materialised interlocking state ----------------------------------------
CREATE TABLE IF NOT EXISTS active_routes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- == open event id
    route_id      TEXT NOT NULL,
    state         TEXT NOT NULL,                     -- ACTIVE | COMPLETED | FAULT
    sections      TEXT NOT NULL,                     -- JSON ordered section ids
    switches      TEXT NOT NULL,                     -- JSON [{id,position}]
    flanks        TEXT NOT NULL,                     -- JSON [resource id]
    grant_payload TEXT NOT NULL,                     -- JSON snapshot for replay
    opened_at     TEXT NOT NULL,
    fault_reason  TEXT
);
CREATE TABLE IF NOT EXISTS route_sections (
    route_id   INTEGER NOT NULL,
    section_id TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    occupied   INTEGER NOT NULL DEFAULT 0,
    cleared    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (route_id, section_id),
    FOREIGN KEY(route_id) REFERENCES active_routes(id)
);
CREATE TABLE IF NOT EXISTS section_state (
    section_id TEXT PRIMARY KEY,
    occupied   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS locks (
    route_id  INTEGER NOT NULL,
    kind      TEXT NOT NULL,               -- SECTION | SWITCH | FLANK
    ref       TEXT NOT NULL,               -- resource key
    PRIMARY KEY (route_id, kind, ref),
    FOREIGN KEY(route_id) REFERENCES active_routes(id)
);

-- Indexes for the hot paths ----------------------------------------------
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
-- Reverse lookup of an applied detection's ruling event: payload->'seq'.
CREATE INDEX IF NOT EXISTS idx_events_det_seq
    ON events(json_extract(payload, '$.seq')) WHERE kind='DETECTION';
CREATE INDEX IF NOT EXISTS idx_rs_section ON route_sections(section_id);
CREATE INDEX IF NOT EXISTS idx_locks_ref  ON locks(kind, ref);
CREATE INDEX IF NOT EXISTS idx_detect_recv ON detections(received_at);

-- Append-only enforcement -------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_events_no_update
BEFORE UPDATE ON events BEGIN
    SELECT RAISE(ABORT, 'events journal is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_events_no_delete
BEFORE DELETE ON events BEGIN
    SELECT RAISE(ABORT, 'events journal is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_detections_no_update
BEFORE UPDATE ON detections BEGIN
    SELECT RAISE(ABORT, 'detections are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_detections_no_delete
BEFORE DELETE ON detections BEGIN
    SELECT RAISE(ABORT, 'detections are append-only');
END;
"""


def configure(db_path: str) -> None:
    global _DB_PATH
    _DB_PATH = db_path
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def connect() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("database not configured; call database.configure() first")
    conn = sqlite3.connect(_DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def write_lock():
    """Re-entrant global lock serialising state transitions.

    SQLite WAL allows one writer anyway; the lock also makes the
    read-check-write sequences atomic from the application's point of view
    (FastAPI is multithreaded).
    """
    return _write_lock


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# -- small helpers ----------------------------------------------------------

def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def insert_event(conn: sqlite3.Connection, kind: str, payload_json: str) -> int:
    cur = conn.execute(
        "INSERT INTO events(ts, kind, payload) VALUES(?,?,?)",
        (now_iso(), kind, payload_json),
    )
    return cur.lastrowid
