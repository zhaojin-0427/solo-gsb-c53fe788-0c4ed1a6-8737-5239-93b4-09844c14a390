"""Performance: after 1,000,000 detector reports, new report and a route
grant still respond within 1 second.

Run explicitly:  pytest -m perf
Seeding uses bulk inserts (history is already-acked in this scenario); the
measured calls go through the full live transaction path.
"""
import json
import os
import time

import pytest

from app import database as db
from app import interlocking as ix

pytestmark = pytest.mark.perf

N_SEED = 1_000_000


def _long_yard(n_sections: int) -> dict:
    """A switch-chain long route of n_sections sections, all via N legs.

    Each switch gets a short R-leg spur so the topology check (root+N+R) is
    satisfied; the spurs are unused by the perf route.
    """
    endpoints = [{"id": "E0"}, {"id": f"E{n_sections}"}]
    # internal nodes are switches; node i connects: root <- T(i-1), N -> T(i)
    switches = [{"id": f"SW{i}"} for i in range(1, n_sections)]
    nodes = ["E0"] + [f"SW{i}" for i in range(1, n_sections)] + \
            [f"E{n_sections}"]
    sections = []
    for i in range(n_sections):
        sec = {"id": f"T{i}", "a": nodes[i], "b": nodes[i + 1],
               "detector": f"DT{i}"}
        # At SW{i} section T(i-1) is the root leg; at SW{i+1} T(i) is N.
        if sec["a"].startswith("SW"):
            sec["a_leg"] = None if sec["a"] == f"SW{i}" else "N"
        if sec["b"].startswith("SW"):
            sec["b_leg"] = "N"
        sections.append(sec)
    for i in range(1, n_sections):
        endpoints.append({"id": f"SP{i}"})
        sections.append({"id": f"SP{i}DG", "a": f"SW{i}", "b": f"SP{i}",
                         "a_leg": "R"})
    return {
        "id": "perf", "name": "perf",
        "endpoints": endpoints, "signals": [],
        "switches": switches,
        "resources": [{"id": "P"}],
        "sections": sections,
        "routes": [{"id": "LONG", "from": "E0", "to": f"E{n_sections}",
                    "sections": [s["id"] for s in sections
                                 if not s["id"].startswith("SP")],
                    "switches": [{"id": f"SW{i}", "position": "N"}
                                 for i in range(1, n_sections)],
                    "flanks": ["P"], "signal": None}],
    }


def test_latency_after_one_million_reports(tmp_path):
    db.configure(str(tmp_path / "perf.db"))
    from app.station import parse_station
    yard = _long_yard(20)
    station = parse_station(yard)
    conn = db.connect()
    ix.import_station(conn, station, json.dumps(yard))
    conn.commit()

    # Bulk-seed 1M historical reports against an unknown section name;
    # they are accepted reports (journal + detections) representing the
    # accumulated history requirement.
    t0 = time.monotonic()
    conn.execute("BEGIN")
    now = db.now_iso()
    batch = 10000
    for base in range(0, N_SEED, batch):
        rows = []
        for seq in range(base + 1, min(base + batch + 1, N_SEED + 1)):
            content = json.dumps(
                {"state": "OCCUPY", "detector": None,
                 "section": "HIST"}, separators=(",", ":"), sort_keys=True)
            import hashlib
            sha = hashlib.sha256(content.encode()).hexdigest()
            rows.append((seq, content, sha, now))
        conn.executemany(
            "INSERT INTO detections(seq, content, sha256, received_at) "
            "VALUES(?,?,?,?)", rows)
        conn.executemany(
            "INSERT INTO events(ts, kind, payload) VALUES(?,?,?)",
            [(now, "DETECTION",
              json.dumps({"seq": s, "state": "OCCUPY", "section": "HIST",
                          "detector": None, "effect": "ANOMALY"},
                         separators=(",", ":"), sort_keys=True))
             for s, *_ in rows])
    conn.commit()
    db.set_meta(conn, ix.META_APPLIED_SEQ, N_SEED)
    conn.commit()
    seed_s = time.monotonic() - t0
    print(f"\nseeded {N_SEED} reports in {seed_s:.1f}s")

    # 1) in-order detection on the live route must be fast (indexed PK).
    g = ix.request_route(conn, "LONG")
    assert g["verdict"] == "GRANT", g
    t0 = time.monotonic()
    with db.write_lock():
        conn.execute("BEGIN IMMEDIATE")
        r1 = ix.submit_detection(
            conn, {"seq": N_SEED + 1, "state": "OCCUPY",
                   "section": "T0"}, station)
        conn.commit()
    dt_report = time.monotonic() - t0
    assert r1["effect"] == "OCCUPY"
    assert dt_report < 1.0, f"detection latency {dt_report:.3f}s"

    # 2) a conflicting route request must be fast while 1M+ events exist.
    t0 = time.monotonic()
    with db.write_lock():
        conn.execute("BEGIN IMMEDIATE")
        v = ix.request_route(conn, "LONG")  # same route still ACTIVE -> DENY
        conn.commit()
    dt_grant = time.monotonic() - t0
    assert v["verdict"] == "DENY"
    assert dt_grant < 1.0, f"request latency {dt_grant:.3f}s"

    # 3) state read must stay fast too.
    t0 = time.monotonic()
    ix.build_state(conn, station)
    dt_state = time.monotonic() - t0
    assert dt_state < 1.0

    # 4) restart replay over 1M events completes (correctness, no hard SLA).
    from app.replay import rebuild_from_journal
    t0 = time.monotonic()
    rebuild_from_journal(conn)
    dt_rebuild = time.monotonic() - t0
    print(f"replay 1M events in {dt_rebuild:.1f}s; "
          f"report {dt_report*1000:.1f}ms, request {dt_grant*1000:.1f}ms, "
          f"state {dt_state*1000:.1f}ms")
    conn.close()
