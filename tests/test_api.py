"""End-to-end HTTP behaviour via the FastAPI test client."""


def test_state_empty_before_import(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    assert r.json()["station"] is None


def test_import_rejects_bad_json(client):
    r = client.post("/api/station", content=b"{not json")
    assert r.status_code == 400


def test_import_rejects_disconnected_yard(client, demo_raw):
    demo_raw["routes"][0]["sections"] = ["1DG", "9DG"]  # no shared node
    r = client.post("/api/station", json=demo_raw)
    assert r.status_code == 422
    assert r.json()["error"] == "YARD_INVALID"


def test_full_workflow_over_http(client_with_demo):
    c = client_with_demo

    v = c.post("/api/routes/request", json={"route_id": "X-XN-I"}).json()
    assert v["verdict"] == "GRANT"

    conflict = c.post("/api/routes/request",
                      json={"route_id": "XN-X-I"}).json()
    assert conflict["verdict"] == "DENY"
    assert conflict["conflicts"]

    # drive the train through in sequence
    for i, sec in enumerate(["1DG", "3DG", "5DG"], start=1):
        r = c.post("/api/detections",
                   json={"seq": i * 2 - 1, "state": "OCCUPY",
                         "section": sec}).json()
        assert r["verdict"] == "APPLIED"
        r = c.post("/api/detections",
                   json={"seq": i * 2, "state": "CLEAR",
                         "section": sec}).json()
    assert r["completed"]

    # event journal + replay endpoints
    evs = c.get("/api/events?limit=200").json()
    kinds = [e["kind"] for e in evs["events"]]
    assert "ROUTE_COMPLETED" in kinds

    replay = c.get(f"/api/replay/{evs['events'][0]['id']}").json()
    assert "ruling" in replay


def test_idempotency_and_conflict_over_http(client_with_demo):
    c = client_with_demo
    c.post("/api/routes/request", json={"route_id": "X-XN-I"})
    body = {"seq": 1, "state": "OCCUPY", "section": "1DG"}
    a = c.post("/api/detections", json=body).json()
    b = c.post("/api/detections", json=body).json()
    assert b["verdict"] == "IDEMPOTENT"
    assert a["event_id"] == b["event_id"]

    r = c.post("/api/detections",
               json={"seq": 1, "state": "CLEAR", "section": "1DG"})
    assert r.status_code == 409
    assert r.json()["error"] == "SEQ_CONTENT_CONFLICT"


def test_gap_blocks_grant_over_http(client_with_demo):
    c = client_with_demo
    c.post("/api/routes/request", json={"route_id": "X-XN-I"})
    r = c.post("/api/detections",
               json={"seq": 2, "state": "OCCUPY", "section": "3DG"}).json()
    assert r["verdict"] == "BUFFERED"
    r = c.post("/api/routes/request", json={"route_id": "S-SN-II"})
    assert r.status_code == 409 and r.json()["error"] == "SEQ_GAP"
    # missing seq 1 unlocks the drain
    r = c.post("/api/detections",
               json={"seq": 1, "state": "OCCUPY", "section": "1DG"}).json()
    assert r["drained_seqs"] == [1, 2]


def test_reimport_blocked_while_route_open(client_with_demo):
    c = client_with_demo
    c.post("/api/routes/request", json={"route_id": "X-XN-I"})
    yard = c.get("/api/state").json()["station"]
    r = c.post("/api/station", json=yard)
    assert r.status_code == 409
