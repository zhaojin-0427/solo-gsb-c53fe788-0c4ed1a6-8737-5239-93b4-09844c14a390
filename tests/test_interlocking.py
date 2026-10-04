"""Interlocking rules: mutual exclusion, staged release, faults, sequence."""
import json
import pytest

from app import database as db
from app import interlocking as ix
from app.interlocking import InterlockError


def grant(conn, rid):
    return ix.request_route(conn, rid)


def detect(conn, station, seq, state, section):
    return ix.submit_detection(conn, {
        "seq": seq, "state": state, "section": section}, station)


# -- dispatch rulings --------------------------------------------------------

def test_grant_locks_sections_switches_flanks(conn, station):
    v = grant(conn, "X-XN-I")
    assert v["verdict"] == "GRANT"
    assert v["signal"]["aspect"] == "PROCEED"
    locked = set(v["locked"])
    assert {"SECTION:1DG", "SECTION:3DG", "SECTION:5DG",
            "SWITCH:3#@N", "SWITCH:5#@N", "SWITCH_NAME:3#",
            "SWITCH_NAME:5#", "FLANK:P1"} <= locked


def test_shared_section_is_mutually_exclusive(conn, station):
    assert grant(conn, "X-XN-I")["verdict"] == "GRANT"
    v = grant(conn, "XN-X-I")          # shares 1DG/3DG/5DG
    assert v["verdict"] == "DENY"
    assert v["signal"]["aspect"] == "HALT"
    refs = {c["ref"] for c in v["conflicts"]}
    assert "1DG" in refs and "3#@N" in refs


def test_shared_flank_is_mutually_exclusive(conn, station):
    # West crossover X-S-W requires both flank protections P1 and P2, so it
    # conflicts with the II-track route even where sections are disjoint.
    assert grant(conn, "X-S-W")["verdict"] == "GRANT"
    v = grant(conn, "S-SN-II")
    assert v["verdict"] == "DENY"
    assert any(c["ref"] == "P2" for c in v["conflicts"])


def test_switch_name_any_position_exclusive(conn, station):
    assert grant(conn, "X-S-W")["verdict"] == "GRANT"   # 3#@R, 7#@R
    v = grant(conn, "X-XN-I")                            # needs 3#@N
    assert v["verdict"] == "DENY"
    assert any("3#" in c["ref"] for c in v["conflicts"])


def test_disjoint_routes_can_coexist(conn, station):
    # The two main tracks are fully disjoint (sections, switches and flank
    # resources): I and II may be granted at the same time.
    assert grant(conn, "X-XN-I")["verdict"] == "GRANT"
    v = grant(conn, "S-SN-II")
    assert v["verdict"] == "GRANT", v


# -- sequential occupation & staged release ---------------------------------

def test_full_journey_releases_in_order_and_unlocks_last(conn, station):
    v = grant(conn, "X-XN-I")
    inst = v["route_instance"]
    sections = ["1DG", "3DG", "5DG"]
    seq = 1
    for k, sid in enumerate(sections):
        r = detect(conn, station, seq, "OCCUPY", sid); seq += 1
        assert r["effect"] == "OCCUPY"
        # occupying section k releases the predecessor once it is clear
        if k > 0:
            assert {x["section"] for x in r["released"]} == {sections[k - 1]}
        r = detect(conn, station, seq, "CLEAR", sid); seq += 1
        # last section clear releases it and unlocks switches/flanks
        if k < len(sections) - 1:
            assert r["released"] == []
        else:
            assert r["completed"] is True
            assert {x["section"] for x in r["released"]} == {sid}
    row = conn.execute(
        "SELECT state FROM active_routes WHERE id=?", (inst,)).fetchone()
    assert row["state"] == "COMPLETED"
    assert conn.execute(
        "SELECT COUNT(*) n FROM locks WHERE route_id=?", (inst,)).fetchone()["n"] == 0
    # after completion, an overlapping route can be granted
    assert grant(conn, "XN-X-I")["verdict"] == "GRANT"


def test_section_not_released_until_successor_occupied(conn, station):
    grant(conn, "X-XN-I")
    detect(conn, station, 1, "OCCUPY", "1DG")
    r = detect(conn, station, 2, "CLEAR", "1DG")
    # successor 3DG not yet occupied: 1DG clears but stays locked
    assert r["released"] == []
    detect(conn, station, 3, "OCCUPY", "3DG")   # now 1DG releases
    state = ix.build_state(conn, station)
    locks = {l["ref"] for l in state["locks"] if l["kind"] == "SECTION"}
    assert "1DG" not in locks and "3DG" in locks


def test_switches_flanks_held_until_last_section_clear(conn, station):
    grant(conn, "X-XN-I")
    detect(conn, station, 1, "OCCUPY", "1DG")
    detect(conn, station, 2, "CLEAR", "1DG")
    detect(conn, station, 3, "OCCUPY", "3DG")
    detect(conn, station, 4, "CLEAR", "3DG")
    # only last section left; switch/flank locks still present
    v = ix.request_route(conn, "XN-X-I")
    assert v["verdict"] == "DENY"   # 5DG + switches 3#/5# still held


# -- out-of-order behaviour --------------------------------------------------

def test_out_of_order_occupy_fault_locks(conn, station):
    grant(conn, "X-XN-I")
    detect(conn, station, 1, "OCCUPY", "1DG")
    r = detect(conn, station, 2, "OCCUPY", "5DG")  # jumped over 3DG
    assert r["effect"] == "FAULT_LOCK"
    state = ix.build_state(conn, station)
    ar = state["active_routes"][0]
    assert ar["state"] == "FAULT"
    assert any(l["ref"] == "P1" for l in ar["locks"])  # locks retained


def test_out_of_order_clear_fault_locks(conn, station):
    grant(conn, "X-XN-I")
    detect(conn, station, 1, "OCCUPY", "1DG")
    detect(conn, station, 2, "CLEAR", "1DG")
    # clear 3DG without it ever being occupied (越序出清)
    r = detect(conn, station, 3, "CLEAR", "3DG")
    assert r["effect"] == "FAULT_LOCK"


def test_clear_without_occupy_fault_locks(conn, station):
    grant(conn, "X-XN-I")
    r = detect(conn, station, 1, "CLEAR", "1DG")
    assert r["effect"] == "FAULT_LOCK"


def test_fault_reset_requires_all_sections_idle(conn, station):
    grant(conn, "X-XN-I")
    detect(conn, station, 1, "OCCUPY", "1DG")
    detect(conn, station, 2, "OCCUPY", "5DG")      # -> FAULT, 5DG occupied
    with pytest.raises(InterlockError) as ei:
        ix.reset_route(conn, "X-XN-I")
    assert ei.value.code == "SECTIONS_NOT_IDLE"
    # once every section is idle (faulted route ignores further transitions),
    # reset succeeds
    detect(conn, station, 3, "CLEAR", "5DG")
    detect(conn, station, 4, "CLEAR", "1DG")
    r = ix.reset_route(conn, "X-XN-I")
    assert r["verdict"] == "RESET"
    assert ix.build_state(conn, station)["active_routes"] == []


# -- sequence buffer / idempotency / gaps ------------------------------------

def test_out_of_order_buffered_then_drained(conn, station):
    grant(conn, "X-XN-I")
    r = detect(conn, station, 2, "OCCUPY", "3DG")
    assert r["verdict"] == "BUFFERED"
    g = ix.gap_info(conn)
    assert g["gap"] and g["gap_missing_seq"] == 1

    # grant blocked while gap exists
    with pytest.raises(InterlockError) as ei:
        ix.request_route(conn, "S-SN-II")
    assert ei.value.code == "SEQ_GAP"

    r1 = detect(conn, station, 1, "OCCUPY", "1DG")
    # wait: 1DG occupy is fine; seq1 arrives and cascades to seq2 (3DG)
    assert r1["verdict"] == "APPLIED"
    assert r1["drained_seqs"] == [1, 2]
    assert not ix.gap_info(conn)["gap"]


def test_same_seq_same_content_idempotent(conn, station):
    grant(conn, "X-XN-I")
    a = detect(conn, station, 1, "OCCUPY", "1DG")
    b = detect(conn, station, 1, "OCCUPY", "1DG")
    assert b["verdict"] == "IDEMPOTENT"
    assert a["event_id"] == b["event_id"]
    # same fact via detector id is identical content
    c = ix.submit_detection(conn, {"seq": 1, "state": "OCCUPY",
                                   "detector": "D1"}, station)
    assert c["verdict"] == "IDEMPOTENT"
    n = conn.execute("SELECT COUNT(*) n FROM detections").fetchone()["n"]
    assert n == 1


def test_same_seq_different_content_rejected(conn, station):
    grant(conn, "X-XN-I")
    detect(conn, station, 1, "OCCUPY", "1DG")
    with pytest.raises(InterlockError) as ei:
        detect(conn, station, 1, "CLEAR", "1DG")
    assert ei.value.code == "SEQ_CONTENT_CONFLICT"
    with pytest.raises(InterlockError):
        ix.submit_detection(conn, {"seq": 1, "state": "OCCUPY",
                                   "section": "3DG"}, station)


def test_gap_blocks_unlock_and_reset(conn, station):
    grant(conn, "X-XN-I")
    detect(conn, station, 2, "OCCUPY", "3DG")   # gap
    with pytest.raises(InterlockError):
        ix.reset_route(conn, "X-XN-I")


def test_unknown_detector_rejected(conn, station):
    with pytest.raises(InterlockError) as ei:
        ix.submit_detection(conn, {"seq": 1, "state": "OCCUPY",
                                   "detector": "ZZZ"}, station)
    assert ei.value.code == "UNKNOWN_DETECTOR"


# -- append-only journal -----------------------------------------------------

def test_events_are_append_only(conn, station):
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE events SET kind='X' WHERE id=1")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM events WHERE id=1")
    # detections need an existing row for the per-row trigger to fire
    ix.submit_detection(conn, {"seq": 1, "state": "OCCUPY",
                               "section": "1DG"}, station)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM detections WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE detections SET content='x' WHERE seq=1")


def sqlite_error():
    import sqlite3
    return sqlite3.IntegrityError


# -- restart: journal replay reconstructs everything -------------------------

def test_restart_rebuilds_locks_and_faults(conn, station, tmp_path):
    grant(conn, "X-XN-I")
    detect(conn, station, 1, "OCCUPY", "1DG")
    detect(conn, station, 2, "OCCUPY", "5DG")  # fault
    detect(conn, station, 3, "CLEAR", "5DG")
    conn.close()

    # reopen same file and rebuild as the lifespan handler does
    conn2 = db.connect()
    from app.replay import rebuild_from_journal
    info = rebuild_from_journal(conn2)
    assert info["events_replayed"] >= 4
    state = ix.build_state(conn2, station)
    ar = [a for a in state["active_routes"]
          if a["route_id"] == "X-XN-I"][0]
    assert ar["state"] == "FAULT"
    # 1DG remains occupied; locks retained
    assert "1DG" in state["occupied_sections"]
    assert any(l["ref"] == "P1" for l in state["locks"])
    # and the rulings are still replayable point-in-time
    from app.replay import replay_until
    grant_ev = conn2.execute(
        "SELECT MIN(id) AS m FROM events WHERE kind='ROUTE_REQUEST'").fetchone()
    r = replay_until(conn2, grant_ev["m"])
    assert r["ruling"]["payload"]["verdict"] == "GRANT"
    conn2.close()


def test_replay_point_in_time_matches(conn, station):
    from app.replay import replay_until
    v = grant(conn, "X-XN-I")
    detect(conn, station, 1, "OCCUPY", "1DG")
    detect(conn, station, 2, "CLEAR", "1DG")
    detect(conn, station, 3, "OCCUPY", "3DG")
    # replay to event #2 = first OCCUPY
    r = replay_until(conn, v["event_id"] + 1)
    assert r["active_routes"][0]["sections"][0]["route_occupied"] is True
    assert r["applied_seq"] == 1
