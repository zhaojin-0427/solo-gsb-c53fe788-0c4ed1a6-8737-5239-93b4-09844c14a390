"""Shared pytest fixtures: isolated DB + FastAPI test client."""
import json
import os
import pathlib

import pytest

@pytest.fixture()
def conn(tmp_path):
    from app import database as db
    db.configure(str(tmp_path / "test.db"))
    c = db.connect()
    yield c
    c.close()


DEMO_PATH = pathlib.Path(__file__).resolve().parents[1] / "examples" / "demo_yard.json"


@pytest.fixture()
def demo_raw():
    return json.loads(DEMO_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def station(conn, demo_raw):
    from app import interlocking as ix
    from app.station import parse_station
    st = parse_station(demo_raw)
    conn.execute("BEGIN")
    ix.import_station(conn, st, json.dumps(demo_raw, ensure_ascii=False))
    conn.commit()
    return st


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with DB pointed at a temp directory (rebuilds on startup)."""
    db_file = tmp_path / "api.db"
    monkeypatch.setenv("INTERLOCK_DB", str(db_file))
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client_with_demo(client, demo_raw):
    r = client.post("/api/station", json=demo_raw)
    assert r.status_code == 200, r.text
    return client
