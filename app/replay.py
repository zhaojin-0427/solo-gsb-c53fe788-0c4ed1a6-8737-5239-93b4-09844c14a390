"""Rebuild materialised state by replaying the append-only journal.

On startup the whole journal is replayed to reconstruct locks / sections /
fault routes ("重启后页面仍显示当前锁闭并可回放裁决"). The same engine can
replay up to an arbitrary event id so the dispatch console can revisit the
ruling at any point in history.

The replay intentionally re-runs the exact same transition functions as the
live path with ``emit=False``: the events already exist, only state is
rebuilt.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from . import database as db
from . import interlocking as ix

REPLAY_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE active_routes (
    id INTEGER PRIMARY KEY, route_id TEXT NOT NULL, state TEXT NOT NULL,
    sections TEXT NOT NULL, switches TEXT NOT NULL, flanks TEXT NOT NULL,
    grant_payload TEXT NOT NULL, opened_at TEXT NOT NULL, fault_reason TEXT);
CREATE TABLE route_sections (
    route_id INTEGER NOT NULL, section_id TEXT NOT NULL, idx INTEGER NOT NULL,
    occupied INTEGER NOT NULL DEFAULT 0, cleared INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (route_id, section_id));
CREATE TABLE section_state (
    section_id TEXT PRIMARY KEY, occupied INTEGER NOT NULL DEFAULT 0);
CREATE TABLE locks (
    route_id INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
    PRIMARY KEY (route_id, kind, ref));
"""


def _reset_runtime(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM locks")
    conn.execute("DELETE FROM route_sections")
    conn.execute("DELETE FROM active_routes")
    conn.execute("DELETE FROM section_state")
    db.set_meta(conn, ix.META_APPLIED_SEQ, 0)


def _replay_grant(mem: sqlite3.Connection, ev_id: int,
                  payload: dict[str, Any]) -> None:
    signal = payload.get("signal")
    if isinstance(signal, dict):
        signal = signal.get("id")
    grant_payload = {
        "route_id": payload["route_id"],
        "sections": payload["sections"],
        "switches": payload["switches"],
        "flanks": payload["flanks"],
        "signal": signal,
        "grant_event_id": ev_id,
    }
    ix._open_route(mem, grant_payload,
                   opened_at=payload.get("opened_at"), pk=ev_id)


def _replay_reset(mem: sqlite3.Connection, payload: dict[str, Any]) -> None:
    pk = payload["route_instance"]
    mem.execute("DELETE FROM locks WHERE route_id=?", (pk,))
    mem.execute("DELETE FROM route_sections WHERE route_id=?", (pk,))
    mem.execute("DELETE FROM active_routes WHERE id=?", (pk,))


def _iter_events(conn: sqlite3.Connection,
                 until: Optional[int] = None):
    sql = ("SELECT id, ts, kind, payload FROM events"
           + (" WHERE id<=?" if until is not None else "")
           + " ORDER BY id")
    params = (until,) if until is not None else ()
    return conn.execute(sql, params)


def rebuild_from_journal(conn: sqlite3.Connection) -> dict[str, Any]:
    """Clear materialised state and replay every event. Called at startup.

    Two-phase for million-event journals:

    1. Scan the journal cheaply (one row at a time, no JSON decode) to find
       the event-id ranges where at least one route is open — GRANT..RESET/
       COMPLETED/FAULT interactions. Detections outside those ranges are
       pure anomalies and mutate nothing.
    2. Decode and apply only events inside those ranges.
    """
    with db.write_lock():
        _reset_runtime(conn)
        count = conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]

        # No json_extract in SQL (it parses JSON per row); cheap membership
        # tests on the raw payload string keep the 1M-row scan tight.
        all_rows = conn.execute(
            "SELECT id, kind, payload FROM events ORDER BY id")
        applied_seq = 0
        open_sections: set[str] = set()
        for ev_id, kind, raw in all_rows:
            if kind == "ROUTE_REQUEST" and '"verdict":"GRANT"' in raw:
                payload = json.loads(raw)
                _replay_grant(conn, ev_id, payload)
                open_sections.update(payload["sections"])
            elif kind == "DETECTION":
                seq = _extract_int(raw, '"seq":')
                if seq is not None:
                    applied_seq = seq
                if open_sections and _raw_mentions(raw, open_sections):
                    payload = json.loads(raw)
                    ix._apply_detection(conn, payload["seq"], {
                        "state": payload["state"],
                        "section": payload["section"],
                        "detector": payload.get("detector"),
                    }, emit=False, existing_event_id=ev_id)
                    open_sections = _open_section_set(conn)
            elif kind == "ROUTE_RESET":
                _replay_reset(conn, json.loads(raw))
                open_sections = _open_section_set(conn)
        db.set_meta(conn, ix.META_APPLIED_SEQ, applied_seq)
    return {"events_replayed": count}


def _open_section_set(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT rs.section_id FROM route_sections rs "
        "JOIN active_routes ar ON ar.id=rs.route_id "
        "WHERE ar.state IN ('ACTIVE','FAULT')")}


def _extract_int(raw: str, marker: str) -> Optional[int]:
    i = raw.find(marker)
    if i < 0:
        return None
    j = i + len(marker)
    k = j
    while k < len(raw) and raw[k].isdigit():
        k += 1
    return int(raw[j:k]) if k > j else None


def _raw_mentions(raw: str, section_ids: set[str]) -> bool:
    for sid in section_ids:
        if f'"section":"{sid}"' in raw:
            return True
    return False


def replay_until(conn: sqlite3.Connection, event_id: int,
                 station_view: Optional[dict[str, Any]] = None
                 ) -> dict[str, Any]:
    """Reconstruct the state visible immediately after ``event_id``.

    Runs in a private in-memory database so the live state is untouched.
    """
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    try:
        mem.executescript(REPLAY_SCHEMA)
        target = conn.execute(
            "SELECT MIN(id) AS lo, MAX(id) AS hi, COUNT(*) AS n FROM events "
            "WHERE id<=?", (event_id,)).fetchone()
        if target["n"] == 0:
            raise ix.InterlockError(
                "EVENT_NOT_FOUND", f"不存在的事件序号: {event_id}", 404)
        applied_seq = 0
        effects: list[dict[str, Any]] = []
        for row in _iter_events(conn, until=event_id):
            payload = json.loads(row["payload"])
            kind = row["kind"]
            if kind == "ROUTE_REQUEST" and payload.get("verdict") == "GRANT":
                _replay_grant(mem, row["id"], payload)
            elif kind == "DETECTION":
                applied_seq = payload["seq"]
                ix._apply_detection(mem, applied_seq, {
                    "state": payload["state"],
                    "section": payload["section"],
                    "detector": payload.get("detector"),
                }, emit=False, existing_event_id=row["id"])
            elif kind == "ROUTE_RESET":
                _replay_reset(mem, payload)
            effects.append({"id": row["id"], "ts": row["ts"],
                            "kind": kind, "payload": payload})

        # Build a compact state view from the ephemeral DB.
        actives = []
        for ar in mem.execute(
                "SELECT * FROM active_routes ORDER BY id").fetchall():
            rss = mem.execute(
                "SELECT section_id, idx, occupied, cleared "
                "FROM route_sections WHERE route_id=? ORDER BY idx",
                (ar["id"],)).fetchall()
            locks = mem.execute(
                "SELECT kind, ref FROM locks WHERE route_id=?",
                (ar["id"],)).fetchall()
            actives.append({
                "instance": ar["id"], "route_id": ar["route_id"],
                "state": ar["state"], "fault_reason": ar["fault_reason"],
                "sections": [{"id": r["section_id"], "index": r["idx"],
                              "route_occupied": bool(r["occupied"]),
                              "cleared": bool(r["cleared"])}
                             for r in rss],
                "switches": json.loads(ar["switches"]),
                "flanks": json.loads(ar["flanks"]),
                "locks": [{"kind": l["kind"], "ref": l["ref"]}
                          for l in locks],
            })
        buffered = [r["seq"] for r in conn.execute(
            "SELECT seq FROM detections WHERE seq>? ORDER BY seq",
            (applied_seq,))]
        return {
            "event_id": event_id,
            "applied_seq": applied_seq,
            "buffered_seqs": buffered,
            "blocked": bool(buffered),
            "active_routes": actives,
            "occupied_sections": [r["section_id"] for r in mem.execute(
                "SELECT section_id FROM section_state WHERE occupied=1")],
            "ruling": effects[-1] if effects else None,
            "events": effects,
        }
    finally:
        mem.close()
