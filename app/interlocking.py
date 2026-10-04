"""Core interlocking logic.

All functions run inside the caller's SQLite transaction and must be called
while holding :func:`app.database.write_lock`.

Event kinds (append-only journal):

* STATION_IMPORT  — a yard definition was imported
* ROUTE_REQUEST   — every dispatch ruling (GRANT with locked resources and
  the signal, or DENY with the conflict evidence)
* DETECTION       — one applied detector report (seq + content + effect)
* SECTION_RELEASE — a section lock was released (successor occupied & self
  cleared)
* ROUTE_COMPLETED — last section cleared: switches/flanks unlocked
* ROUTE_FAULT     — out-of-order occupy/clear locked the route
* ROUTE_RESET     — fault reset after confirming every section idle
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Optional

from . import database as db
from .station import Station

# Event kinds
EVENT_STATION_IMPORT = "STATION_IMPORT"
EVENT_ROUTE_REQUEST = "ROUTE_REQUEST"
EVENT_DETECTION = "DETECTION"
EVENT_SECTION_RELEASE = "SECTION_RELEASE"
EVENT_ROUTE_COMPLETED = "ROUTE_COMPLETED"
EVENT_ROUTE_FAULT = "ROUTE_FAULT"
EVENT_ROUTE_RESET = "ROUTE_RESET"

OCCUPY = "OCCUPY"
CLEAR = "CLEAR"

META_APPLIED_SEQ = "applied_seq"


class InterlockError(Exception):
    """Rule rejection — mapped to HTTP 400/409 in main.py."""

    def __init__(self, code: str, message: str, status: int = 400,
                 extra: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra or {}


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)


def canonical_content(data: dict[str, Any]) -> tuple[str, str]:
    """Canonical detector-report content and its sha256.

    Same sequence + same content is idempotent; same sequence + different
    content is rejected. Only the rule-relevant fields are canonicalised so
    irrelevant formatting differences never matter.
    """
    canon = {
        "state": data["state"],
        "detector": data.get("detector"),
        "section": data.get("section"),
    }
    text = _j(canon)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Station import
# ---------------------------------------------------------------------------

def import_station(conn: sqlite3.Connection, station: Station,
                   spec_json: str) -> dict[str, Any]:
    active = conn.execute("SELECT COUNT(*) AS n FROM active_routes").fetchone()
    if active["n"]:
        raise InterlockError(
            "STATION_BUSY",
            "存在未终结的进路（活动或故障锁闭），不能重新导入站场", 409)
    event_id = db.insert_event(conn, EVENT_STATION_IMPORT, _j({
        "yard_id": station.yard_id,
        "name": station.name,
        "spec": json.loads(spec_json),
    }))
    conn.execute(
        "INSERT INTO station(id, yard_id, spec, imported_at) "
        "VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "yard_id=excluded.yard_id, spec=excluded.spec, "
        "imported_at=excluded.imported_at",
        (station.yard_id, spec_json, db.now_iso()))
    return {"event_id": event_id, "name": station.name,
            "routes": list(station.routes)}


def load_station_row(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM station WHERE id=1").fetchone()


def require_station(conn: sqlite3.Connection) -> Station:
    row = load_station_row(conn)
    if row is None:
        raise InterlockError("NO_STATION", "尚未导入站场", 409)
    from .station import parse_station
    return parse_station(json.loads(row["spec"]))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def gap_info(conn: sqlite3.Connection) -> dict[str, Any]:
    """Sequence status.

    ``applied_seq`` is the highest contiguous sequence applied. Rows with
    seq > applied_seq are buffered (out-of-order); the first missing number
    blocks new grants, releases and resets.
    """
    applied = int(db.get_meta(conn, META_APPLIED_SEQ, 0) or 0)
    buffered = [r["seq"] for r in conn.execute(
        "SELECT seq FROM detections WHERE seq>? ORDER BY seq", (applied,))]
    missing = applied + 1
    return {
        "applied_seq": applied,
        "next_expected_seq": missing,
        "buffered_seqs": buffered,
        "gap": bool(buffered),   # any buffered seq implies a hole
        "gap_missing_seq": missing if buffered else None,
    }


def _ensure_no_gap(conn: sqlite3.Connection, action: str) -> dict[str, Any]:
    gap = gap_info(conn)
    if gap["gap"]:
        raise InterlockError(
            "SEQ_GAP",
            f"检测序号缺 {gap['gap_missing_seq']}，"
            f"禁止{action}（乱序缓冲中: {gap['buffered_seqs']}）",
            409, extra={"gap": gap})
    return gap


def _set_section_occupied(conn: sqlite3.Connection, section_id: str,
                          occupied: bool) -> None:
    conn.execute(
        "INSERT INTO section_state(section_id, occupied) VALUES(?,?) "
        "ON CONFLICT(section_id) DO UPDATE SET occupied=excluded.occupied",
        (section_id, 1 if occupied else 0))


def _open_route(conn: sqlite3.Connection, grant_payload: dict[str, Any],
                opened_at: Optional[str] = None,
                pk: Optional[int] = None) -> int:
    """Materialise a granted route. Used both live and by replay.

    During replay the primary key is forced to the original GRANT event id,
    so references stay identical.
    """
    if pk is None:
        cur = conn.execute(
            "INSERT INTO active_routes(route_id, state, sections, switches, "
            "flanks, grant_payload, opened_at) VALUES(?,?,?,?,?,?,?)",
            (grant_payload["route_id"], "ACTIVE",
             _j(grant_payload["sections"]), _j(grant_payload["switches"]),
             _j(grant_payload["flanks"]), _j(grant_payload),
             opened_at or db.now_iso()))
        pk = cur.lastrowid
    else:
        conn.execute(
            "INSERT INTO active_routes(id, route_id, state, sections, "
            "switches, flanks, grant_payload, opened_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (pk, grant_payload["route_id"], "ACTIVE",
             _j(grant_payload["sections"]), _j(grant_payload["switches"]),
             _j(grant_payload["flanks"]), _j(grant_payload),
             opened_at or db.now_iso()))
    for idx, sid in enumerate(grant_payload["sections"]):
        conn.execute(
            "INSERT INTO route_sections(route_id, section_id, idx) "
            "VALUES(?,?,?)", (pk, sid, idx))
        conn.execute(
            "INSERT INTO locks(route_id, kind, ref) VALUES(?,?,?)",
            (pk, "SECTION", sid))
    for sw in grant_payload["switches"]:
        conn.execute(
            "INSERT INTO locks(route_id, kind, ref) VALUES(?,?,?)",
            (pk, "SWITCH", f"{sw['id']}@{sw['position']}"))
        conn.execute(
            "INSERT INTO locks(route_id, kind, ref) VALUES(?,?,?)",
            (pk, "SWITCH_NAME", sw["id"]))
    for rid in grant_payload["flanks"]:
        conn.execute(
            "INSERT INTO locks(route_id, kind, ref) VALUES(?,?,?)",
            (pk, "FLANK", rid))
    return pk


def _locked_refs(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    return {(r["kind"], r["ref"]) for r in
            conn.execute("SELECT kind, ref FROM locks")}


# ---------------------------------------------------------------------------
# Dispatch: request a route
# ---------------------------------------------------------------------------

def request_route(conn: sqlite3.Connection, route_id: str) -> dict[str, Any]:
    station = require_station(conn)
    route = station.routes.get(route_id)
    if route is None:
        raise InterlockError("ROUTE_NOT_FOUND",
                             f"进路不存在: {route_id}", 404)

    gap = _ensure_no_gap(conn, "新放行")

    # Same route still active / faulted
    dup = conn.execute(
        "SELECT id, state FROM active_routes WHERE route_id=?",
        (route_id,)).fetchall()
    conflicts: list[dict[str, Any]] = []
    for row in dup:
        label = {"ACTIVE": "已开放未出清",
                 "FAULT": "故障锁闭中"}.get(row["state"], row["state"])
        conflicts.append({"type": "ROUTE_BUSY",
                          "ref": f"route:{route_id}",
                          "detail": f"进路{label} (实例#{row['id']})",
                          "held_by": row["id"]})

    wanted: set[tuple[str, str]] = set()
    for sid in route.sections:
        wanted.add(("SECTION", sid))
    for wid, pos in route.switches.items():
        wanted.add(("SWITCH", f"{wid}@{pos}"))
        wanted.add(("SWITCH_NAME", wid))
    for rid in route.flanks:
        wanted.add(("FLANK", rid))

    held = _locked_refs(conn)
    if wanted & held or conflicts:
        owners = conn.execute(
            "SELECT route_id, kind, ref FROM locks").fetchall()
        owner_map: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in owners:
            owner_map.setdefault((row["kind"], row["ref"]), []).append(row)
        type_name = {"SECTION": "共用区段", "SWITCH": "道岔位置",
                     "SWITCH_NAME": "道岔", "FLANK": "侧防资源"}
        for kind, ref in sorted(wanted & held):
            for row in owner_map[(kind, ref)]:
                conflicts.append({
                    "type": kind,
                    "ref": ref,
                    "detail": f"{type_name.get(kind, kind)} {ref} 已被 "
                              f"进路 {row['route_id']}(#{row['route_id']}) 锁闭",
                    "held_by": row["route_id"],
                })
        verdict = {
            "verdict": "DENY",
            "route_id": route_id,
            "signal": {"id": route.signal, "aspect": "HALT",
                       "display": "红灯（停车）"},
            "conflicts": conflicts,
            "reason": "资源未全部空闲，信号保持停车",
        }
        event_id = db.insert_event(
            conn, EVENT_ROUTE_REQUEST, _j(verdict))
        verdict["event_id"] = event_id
        return verdict

    grant_payload = {
        "route_id": route_id,
        "sections": list(route.sections),
        "switches": [{"id": k, "position": v}
                     for k, v in route.switches.items()],
        "flanks": list(route.flanks),
        "signal": route.signal,
        "from": route.fr,
        "to": route.to,
    }
    opened_at = db.now_iso()
    # Grant first as an event so the active-route row id equals the event id.
    verdict = {
        "verdict": "GRANT",
        "route_id": route_id,
        "signal": {"id": route.signal,
                   "aspect": "PROCEED", "display": "绿灯（按进路放行）"},
        "locked": sorted(f"{k}:{r}" for k, r in wanted),
        "sections": list(route.sections),
        "switches": grant_payload["switches"],
        "flanks": list(route.flanks),
        "opened_at": opened_at,
        "release_rule": "道岔与侧防锁至末段出清；区段须后继段占用且本段"
                        "出清后逐段释放",
    }
    event_id = db.insert_event(conn, EVENT_ROUTE_REQUEST, _j(verdict))
    grant_payload["grant_event_id"] = event_id
    # The active-route instance id is exactly its GRANT event id.
    pk = _open_route(conn, grant_payload, opened_at=opened_at, pk=event_id)
    verdict["route_instance"] = pk
    verdict["event_id"] = event_id
    return verdict


# ---------------------------------------------------------------------------
# Detection reports
# ---------------------------------------------------------------------------

def _resolve_section(conn: sqlite3.Connection, station: Station,
                     detector: Optional[str],
                     section: Optional[str]) -> str:
    det_map = station.section_by_detector()
    if detector is not None and section is not None:
        if det_map.get(detector) != section:
            raise InterlockError(
                "DETECTOR_MISMATCH",
                f"检测器 {detector} 不属于区段 {section}", 400)
        return section
    if detector is not None:
        if detector not in det_map:
            raise InterlockError("UNKNOWN_DETECTOR",
                                 f"检测器不存在: {detector}", 400)
        return det_map[detector]
    if section is not None:
        if section not in station.sections:
            raise InterlockError("UNKNOWN_SECTION",
                                 f"区段不存在: {section}", 400)
        return section
    raise InterlockError("NO_TARGET",
                         "上报必须包含 detector 或 section", 400)


def submit_detection(conn: sqlite3.Connection, data: dict[str, Any],
                     station: Station) -> dict[str, Any]:
    seq = data.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise InterlockError("BAD_SEQ", "序号必须是从 1 开始的正整数", 400)
    state = data.get("state")
    if state not in (OCCUPY, CLEAR):
        raise InterlockError("BAD_STATE", "state 必须是 OCCUPY 或 CLEAR", 400)

    section_id = _resolve_section(
        conn, station, data.get("detector"), data.get("section"))
    # Canonical content keeps the caller-provided target fields; a report
    # using detector D1 and the equivalent section-based report are still
    # compared by the resolved section recorded alongside. To make both
    # encodings identical, normalise to the detector when one exists.
    det = data.get("detector")
    if det is None:
        det = station.sections[section_id].detector
    content, sha = canonical_content({
        "state": state, "detector": det,
        "section": section_id})

    # Idempotency / same-seq-different-content.
    existing = conn.execute(
        "SELECT seq, content, sha256 FROM detections WHERE seq=?",
        (seq,)).fetchone()
    if existing is not None:
        if existing["sha256"] != sha:
            raise InterlockError(
                "SEQ_CONTENT_CONFLICT",
                f"序号 {seq} 已存在不同内容，拒绝改写", 409,
                extra={"seq": seq, "existing": existing["content"]})
        applied = int(db.get_meta(conn, META_APPLIED_SEQ, 0) or 0)
        if seq <= applied:
            ev = conn.execute(
                "SELECT id FROM events WHERE kind='DETECTION' "
                "AND json_extract(payload, '$.seq')=?",
                (seq,)).fetchone()
            return {"verdict": "IDEMPOTENT", "seq": seq,
                    "status": "APPLIED",
                    "event_id": ev["id"] if ev else None,
                    "note": "同序同内容，幂等返回原裁决"}
        return {"verdict": "IDEMPOTENT", "seq": seq, "status": "BUFFERED",
                "event_id": None,
                "note": "同序同内容，仍在乱序缓冲中"}

    applied = int(db.get_meta(conn, META_APPLIED_SEQ, 0) or 0)
    if seq <= applied:
        # Cannot normally happen (existing row would be found); keep guard.
        raise InterlockError("SEQ_BELOW_CURSOR",
                             f"序号 {seq} 已越过", 409)

    conn.execute(
        "INSERT INTO detections(seq, content, sha256, received_at) "
        "VALUES(?,?,?,?)",
        (seq, content, sha, db.now_iso()))

    if seq > applied + 1:
        gap = gap_info(conn)
        return {"verdict": "BUFFERED", "seq": seq,
                "note": f"乱序上报已缓冲，等待序号 {applied + 1}",
                "gap": gap}

    # In-order: cascade through every contiguous buffered report.
    return _drain_buffer(conn)


def _drain_buffer(conn: sqlite3.Connection) -> dict[str, Any]:
    """Apply seq, seq+1, ... while buffered rows exist contiguously."""
    last: Optional[dict[str, Any]] = None
    processed: list[int] = []
    while True:
        applied = int(db.get_meta(conn, META_APPLIED_SEQ, 0) or 0)
        row = conn.execute(
            "SELECT seq, content FROM detections WHERE seq=?",
            (applied + 1,)).fetchone()
        if row is None:
            break
        content = json.loads(row["content"])
        effect = _apply_detection(conn, row["seq"], content, emit=True)
        db.set_meta(conn, META_APPLIED_SEQ, row["seq"])
        processed.append(row["seq"])
        last = effect
    assert last is not None
    out = dict(last)
    out["verdict"] = "APPLIED"
    if len(processed) > 1:
        out["note"] = f"连同缓冲一并处理 {len(processed)} 条"
        out["drained_seqs"] = processed
    return out


def _find_route_section(conn: sqlite3.Connection, section_id: str):
    rows = conn.execute(
        "SELECT rs.route_id, rs.idx, rs.occupied, rs.cleared, "
        "ar.route_id AS route_code, ar.state "
        "FROM route_sections rs JOIN active_routes ar ON ar.id=rs.route_id "
        "WHERE rs.section_id=? AND ar.state IN ('ACTIVE','FAULT') "
        "ORDER BY rs.route_id", (section_id,)).fetchall()
    active = [r for r in rows if r["state"] == "ACTIVE"]
    return (active[0] if active else (rows[0] if rows else None),
            rows)


def _fault_route(conn: sqlite3.Connection, route_pk: int, code: str,
                 detail: str, section_id: str, seq: int,
                 emit: bool = True) -> int:
    conn.execute(
        "UPDATE active_routes SET state='FAULT', fault_reason=? WHERE id=?",
        (f"{code}: {detail}", route_pk))
    payload = {
        "route_instance": route_pk,
        "section": section_id, "seq": seq,
        "fault_code": code, "detail": detail,
        "locks_retained": True,
        "reset_rule": "须确认全段空闲后人工故障复位",
    }
    if emit:
        return db.insert_event(conn, EVENT_ROUTE_FAULT, _j(payload))
    return -1  # caller already holds the original fault event during replay


def _apply_detection(conn: sqlite3.Connection, seq: int,
                     content: dict[str, Any], emit: bool = True,
                     existing_event_id: Optional[int] = None
                     ) -> dict[str, Any]:
    """Apply one in-order detector report to materialised state.

    emit=True appends the ruling events (live). emit=False mutates state only
    while replaying a journal that already contains those events.
    """
    section_id = content["section"]
    state = content["state"]
    rrow, all_rows = _find_route_section(conn, section_id)

    def ev(payload: dict[str, Any]) -> int:
        if emit:
            return db.insert_event(conn, EVENT_DETECTION, _j(payload))
        return existing_event_id if existing_event_id is not None else -1

    base = {"seq": seq, "state": state, "section": section_id,
            "detector": content.get("detector")}

    # No active route owns this section — anomaly, nothing to lock.
    if rrow is None:
        _set_section_occupied(conn, section_id, state == OCCUPY)
        event_id = ev({**base, "effect": "ANOMALY",
                       "detail": "没有活动进路使用该区段"})
        return {**base, "effect": "ANOMALY",
                "detail": "没有活动进路使用该区段", "event_id": event_id}

    pk = rrow["route_id"]
    route = conn.execute(
        "SELECT route_id FROM active_routes WHERE id=?", (pk,)).fetchone()
    route_code = route["route_id"]

    # Faulted route: keep reflecting section occupancy, but no transition.
    if rrow["state"] == "FAULT":
        _set_section_occupied(conn, section_id, state == OCCUPY)
        event_id = ev({**base, "route_instance": pk, "route_id": route_code,
                       "effect": "FAULT_LOCKED_NO_ACTION",
                       "detail": "进路已故障锁闭，事件仅记录"})
        return {**base, "effect": "FAULT_LOCKED",
                "route_instance": pk, "route_id": route_code,
                "event_id": event_id}

    idx = rrow["idx"]

    # -- OCCUPY ------------------------------------------------------------
    if state == OCCUPY:
        if rrow["occupied"]:
            detail = f"区段 {section_id} 已占用，重复占用"
            fault_id = _fault_route(
                conn, pk, "OUT_OF_ORDER_OCCUPY",
                detail, section_id, seq, emit=emit)
            _set_section_occupied(conn, section_id, True)
            event_id = ev({**base, "route_instance": pk,
                           "route_id": route_code,
                           "effect": "FAULT_LOCK", "fault_event_id": fault_id,
                           "fault_code": "OUT_OF_ORDER_OCCUPY",
                           "detail": detail})
            return {**base, "effect": "FAULT_LOCK",
                    "route_instance": pk, "route_id": route_code,
                    "fault_event_id": fault_id, "event_id": event_id,
                    "detail": detail}

        # The train must occupy the first not-yet-cleared section in
        # order. A predecessor may be cleared (occupied=0, released or
        # still waiting for this very successor).
        nxt_row = conn.execute(
            "SELECT idx, section_id FROM route_sections "
            "WHERE route_id=? AND cleared=0 ORDER BY idx LIMIT 1",
            (pk,)).fetchone()
        if nxt_row["idx"] != idx:
            detail = (f"期望第 {nxt_row['idx'] + 1} 段占用"
                      f"（{nxt_row['section_id']}），"
                      f"却收到第 {idx + 1} 段 {section_id} 占用")
            fault_id = _fault_route(
                conn, pk, "OUT_OF_ORDER_OCCUPY", detail, section_id, seq,
                emit=emit)
            _set_section_occupied(conn, section_id, True)
            event_id = ev({**base, "route_instance": pk,
                           "route_id": route_code,
                           "effect": "FAULT_LOCK", "fault_event_id": fault_id,
                           "fault_code": "OUT_OF_ORDER_OCCUPY",
                           "detail": detail})
            return {**base, "effect": "FAULT_LOCK",
                    "route_instance": pk, "route_id": route_code,
                    "fault_event_id": fault_id, "event_id": event_id,
                    "detail": detail}

        conn.execute(
            "UPDATE route_sections SET occupied=1 "
            "WHERE route_id=? AND section_id=?", (pk, section_id))
        _set_section_occupied(conn, section_id, True)
        event_id = ev({**base, "route_instance": pk, "route_id": route_code,
                       "index": idx, "effect": "OCCUPY"})
        # Successor occupation may release already-cleared predecessor(s).
        released = _release_cascade(conn, pk, route_code, emit)
        out = {**base, "effect": "OCCUPY", "route_instance": pk,
               "route_id": route_code, "index": idx, "event_id": event_id,
               "released": released}
        if _maybe_completed(conn, pk):
            out["completed"] = True
        return out

    # -- CLEAR -------------------------------------------------------------
    if not rrow["occupied"]:
        detail = f"区段 {section_id} 未占用即上报出清（越序出清）"
        fault_id = _fault_route(
            conn, pk, "OUT_OF_ORDER_CLEAR", detail, section_id, seq,
            emit=emit)
        event_id = ev({**base, "route_instance": pk, "route_id": route_code,
                       "effect": "FAULT_LOCK", "fault_event_id": fault_id,
                       "fault_code": "OUT_OF_ORDER_CLEAR", "detail": detail})
        return {**base, "effect": "FAULT_LOCK",
                "route_instance": pk, "route_id": route_code,
                "fault_event_id": fault_id, "event_id": event_id,
                "detail": detail}

    prev = conn.execute(
        "SELECT idx, occupied, cleared FROM route_sections "
        "WHERE route_id=? AND idx=?", (pk, idx - 1)).fetchone()
    if prev is not None and prev["occupied"] and not prev["cleared"]:
        detail = (f"后继段 {section_id} 出清前，前段 "
                  f"{_section_at(conn, pk, idx - 1)} 尚未出清（越序出清）")
        fault_id = _fault_route(
            conn, pk, "OUT_OF_ORDER_CLEAR", detail, section_id, seq,
            emit=emit)
        _set_section_occupied(conn, section_id, False)
        event_id = ev({**base, "route_instance": pk, "route_id": route_code,
                       "effect": "FAULT_LOCK", "fault_event_id": fault_id,
                       "fault_code": "OUT_OF_ORDER_CLEAR", "detail": detail})
        return {**base, "effect": "FAULT_LOCK",
                "route_instance": pk, "route_id": route_code,
                "fault_event_id": fault_id, "event_id": event_id,
                "detail": detail}

    conn.execute(
        "UPDATE route_sections SET occupied=0, cleared=1 "
        "WHERE route_id=? AND section_id=?", (pk, section_id))
    _set_section_occupied(conn, section_id, False)
    event_id = ev({**base, "route_instance": pk, "route_id": route_code,
                   "index": idx, "effect": "CLEAR"})
    released = _release_cascade(conn, pk, route_code, emit)
    out = {**base, "effect": "CLEAR", "route_instance": pk,
           "route_id": route_code, "index": idx, "event_id": event_id,
           "released": released}
    if _maybe_completed(conn, pk):
        out["completed"] = True
    return out


def _section_at(conn: sqlite3.Connection, pk: int, idx: int) -> str:
    row = conn.execute(
        "SELECT section_id FROM route_sections WHERE route_id=? AND idx=?",
        (pk, idx)).fetchone()
    return row["section_id"] if row else "?"


def _release_cascade(conn: sqlite3.Connection, pk: int, route_code: str,
                     emit: bool) -> list[dict[str, Any]]:
    """Release every cleared section whose successor is occupied.

    A section releases exactly when the successor section is confirmed
    occupied (so the train has left it) — or, for the last section, when it
    is itself cleared. Released section locks are deleted; the cascade walks
    backwards because releasing one section can unblock its predecessor.
    When the last section lock is gone the switches/flanks are unlocked too
    and the route is COMPLETED.
    """
    released: list[dict[str, Any]] = []
    while True:
        cand = conn.execute(
            "SELECT cur.idx AS idx, cur.section_id AS sid, "
            "       cur.cleared AS cleared, nxt.occupied AS succ_occ, "
            "       l.kind AS has_lock "
            "FROM route_sections cur "
            "LEFT JOIN route_sections nxt "
            "  ON nxt.route_id=cur.route_id AND nxt.idx=cur.idx+1 "
            "LEFT JOIN locks l ON l.route_id=cur.route_id "
            "  AND l.kind='SECTION' AND l.ref=cur.section_id "
            "WHERE cur.route_id=? AND cur.cleared=1 AND l.kind IS NOT NULL "
            "  AND (nxt.idx IS NULL OR nxt.occupied=1) "
            "ORDER BY cur.idx LIMIT 1", (pk,)).fetchone()
        if cand is None:
            break
        conn.execute(
            "DELETE FROM locks WHERE route_id=? AND kind='SECTION' AND ref=?",
            (pk, cand["sid"]))
        if emit:
            rel_id = db.insert_event(conn, EVENT_SECTION_RELEASE, _j({
                "route_instance": pk, "route_id": route_code,
                "section": cand["sid"], "index": cand["idx"]}))
        else:
            rel_id = -1
        released.append({"section": cand["sid"], "index": cand["idx"],
                         "event_id": rel_id})

    # Last section cleared -> all section locks gone -> drop the switches
    # and flanks; the route is fully released at末段出清.
    remaining_sec = conn.execute(
        "SELECT COUNT(*) AS n FROM locks WHERE route_id=? AND kind='SECTION'",
        (pk,)).fetchone()["n"]
    if remaining_sec == 0:
        last = conn.execute(
            "SELECT MAX(idx) AS m FROM route_sections WHERE route_id=? "
            "AND cleared=1", (pk,)).fetchone()
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM route_sections WHERE route_id=?",
            (pk,)).fetchone()["n"]
        if last["m"] is not None and last["m"] == total - 1:
            conn.execute(
                "DELETE FROM locks WHERE route_id=? AND kind!='SECTION'",
                (pk,))
            conn.execute(
                "UPDATE active_routes SET state='COMPLETED' WHERE id=?",
                (pk,))
            if emit:
                last_sec = _section_at(conn, pk, last["m"])
                db.insert_event(conn, EVENT_ROUTE_COMPLETED, _j({
                    "route_instance": pk, "route_id": route_code,
                    "last_section": last_sec}))
    return released


def _maybe_completed(conn: sqlite3.Connection, pk: int) -> bool:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM locks WHERE route_id=?",
        (pk,)).fetchone()["n"] == 0 and conn.execute(
        "SELECT state FROM active_routes WHERE id=?",
        (pk,)).fetchone()["state"] == "COMPLETED"


# ---------------------------------------------------------------------------
# Fault reset
# ---------------------------------------------------------------------------

def reset_route(conn: sqlite3.Connection, identifier: str) -> dict[str, Any]:
    _ensure_no_gap(conn, "故障复位")
    row = None
    if identifier.isdigit():
        row = conn.execute(
            "SELECT * FROM active_routes WHERE id=?",
            (int(identifier),)).fetchone()
    if row is None:
        rows = conn.execute(
            "SELECT * FROM active_routes WHERE route_id=? AND state='FAULT' "
            "ORDER BY id DESC", (identifier,)).fetchall()
        if not rows:
            raise InterlockError(
                "ROUTE_NOT_FOUND",
                f"没有故障锁闭的进路: {identifier}", 404)
        row = rows[0]
    if row["state"] != "FAULT":
        raise InterlockError(
            "NOT_FAULTED",
            f"进路 #{row['id']}({row['route_id']}) 状态为 {row['state']}，"
            f"无需复位", 409,
            extra={"route_instance": row["id"], "state": row["state"]})

    sections = json.loads(row["sections"])
    busy = []
    for sid in sections:
        ss = conn.execute(
            "SELECT occupied FROM section_state WHERE section_id=?",
            (sid,)).fetchone()
        if ss is not None and ss["occupied"]:
            busy.append(sid)
    if busy:
        raise InterlockError(
            "SECTIONS_NOT_IDLE",
            f"复位须确认全段空闲，仍占用: {busy}", 409,
            extra={"route_instance": row["id"], "busy": busy})

    conn.execute("DELETE FROM locks WHERE route_id=?", (row["id"],))
    conn.execute("DELETE FROM route_sections WHERE route_id=?", (row["id"],))
    conn.execute("DELETE FROM active_routes WHERE id=?", (row["id"],))
    event_id = db.insert_event(conn, EVENT_ROUTE_RESET, _j({
        "route_instance": row["id"], "route_id": row["route_id"],
        "fault_reason": row["fault_reason"],
        "confirmed_idle": sections}))
    return {"verdict": "RESET", "route_instance": row["id"],
            "route_id": row["route_id"], "event_id": event_id}


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------

def build_state(conn: sqlite3.Connection,
                station: Optional[Station] = None) -> dict[str, Any]:
    if station is None:
        station = require_station(conn)
    gap = gap_info(conn)
    actives = []
    for ar in conn.execute(
            "SELECT * FROM active_routes ORDER BY id").fetchall():
        rss = conn.execute(
            "SELECT section_id, idx, occupied, cleared FROM route_sections "
            "WHERE route_id=? ORDER BY idx", (ar["id"],)).fetchall()
        locks = conn.execute(
            "SELECT kind, ref FROM locks WHERE route_id=?",
            (ar["id"],)).fetchall()
        sections_view = []
        for rs in rss:
            ss = conn.execute(
                "SELECT occupied FROM section_state WHERE section_id=?",
                (rs["section_id"],)).fetchone()
            sections_view.append({
                "id": rs["section_id"], "index": rs["idx"],
                "route_occupied": bool(rs["occupied"]),
                "cleared": bool(rs["cleared"]),
                "global_occupied": bool(ss and ss["occupied"]),
                "detector": station.detector_of(rs["section_id"]),
            })
        actives.append({
            "instance": ar["id"],
            "route_id": ar["route_id"],
            "state": ar["state"],
            "fault_reason": ar["fault_reason"],
            "opened_at": ar["opened_at"],
            "sections": sections_view,
            "switches": json.loads(ar["switches"]),
            "flanks": json.loads(ar["flanks"]),
            "locks": [{"kind": l["kind"], "ref": l["ref"]} for l in locks],
        })

    occupied_sections = [r["section_id"] for r in conn.execute(
        "SELECT section_id FROM section_state WHERE occupied=1")]
    return {
        "station": station.to_view(),
        "sequence": gap,
        "blocked": gap["gap"],
        "active_routes": actives,
        "occupied_sections": occupied_sections,
        "locks": [{"route_instance": r["id"], "route_id": r["route_id"],
                   "kind": r["kind"], "ref": r["ref"]}
                  for r in conn.execute(
                      "SELECT l.kind AS kind, l.ref AS ref, ar.id AS id, "
                      "ar.route_id AS route_id FROM locks l "
                      "JOIN active_routes ar ON ar.id=l.route_id "
                      "ORDER BY l.kind, l.ref")],
    }
