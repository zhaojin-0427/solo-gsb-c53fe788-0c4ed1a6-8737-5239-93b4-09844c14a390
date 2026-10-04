"""FastAPI entrypoint for the route interlocking console."""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database as db
from . import interlocking as ix
from . import replay
from .station import YardError, parse_station

def _db_path() -> str:
    # Read lazily so tests can point INTERLOCK_DB at a temp file and so the
    # same process can run multiple isolated app instances.
    return os.environ.get("INTERLOCK_DB", "/data/interlock.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(_db_path())
    conn = db.connect()
    try:
        # Materialised state always derives from the journal on boot.
        replay.rebuild_from_journal(conn)
        conn.commit()
    finally:
        conn.close()
    yield


app = FastAPI(title="站场进路联锁推演台", version="1.0.0",
              lifespan=lifespan)


class RouteRequestIn(BaseModel):
    route_id: str = Field(..., description="进路 id")


class DetectionIn(BaseModel):
    seq: int = Field(..., ge=1)
    state: str
    detector: Optional[str] = None
    section: Optional[str] = None


class ResetIn(BaseModel):
    route: str = Field(..., description="进路 id 或开放实例号")


def _error(exc: ix.InterlockError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.code, "detail": str(exc), **exc.extra})


def _station_or_error(conn) -> Any:
    return ix.require_station(conn)


# -- API --------------------------------------------------------------------

@app.get("/api/state")
def get_state():
    conn = db.connect()
    try:
        try:
            station = _station_or_error(conn)
        except ix.InterlockError:
            return {"station": None,
                    "sequence": ix.gap_info(conn),
                    "active_routes": [], "occupied_sections": [],
                    "locks": [], "blocked": False}
        with db.write_lock():
            state = ix.build_state(conn, station)
        return state
    finally:
        conn.close()


@app.post("/api/station")
async def import_station_api(request: Request):
    raw_body = await request.body()
    try:
        raw = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return JSONResponse(status_code=400,
                            content={"error": "BAD_JSON", "detail": str(exc)})
    try:
        station = parse_station(raw)
    except YardError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": "YARD_INVALID", "detail": str(exc)})
    conn = db.connect()
    try:
        with db.write_lock():
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = ix.import_station(conn, station, raw_body.decode())
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"ok": True, "event_id": result["event_id"],
                "name": result["name"],
                "routes": list(station.routes)}
    except ix.InterlockError as exc:
        return _error(exc)
    finally:
        conn.close()


@app.post("/api/routes/request")
def request_route_api(body: RouteRequestIn):
    conn = db.connect()
    try:
        with db.write_lock():
            conn.execute("BEGIN IMMEDIATE")
            try:
                verdict = ix.request_route(conn, body.route_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return verdict
    except ix.InterlockError as exc:
        return _error(exc)
    finally:
        conn.close()


@app.post("/api/detections")
def detection_api(body: DetectionIn):
    conn = db.connect()
    try:
        with db.write_lock():
            conn.execute("BEGIN IMMEDIATE")
            try:
                station = ix.require_station(conn)
                result = ix.submit_detection(
                    conn, body.model_dump(), station)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return result
    except ix.InterlockError as exc:
        return _error(exc)
    finally:
        conn.close()


@app.post("/api/routes/reset")
def reset_api(body: ResetIn):
    conn = db.connect()
    try:
        with db.write_lock():
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = ix.reset_route(conn, body.route)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return result
    except ix.InterlockError as exc:
        return _error(exc)
    finally:
        conn.close()


@app.get("/api/events")
def list_events(limit: int = 100, before: Optional[int] = None,
                kind: Optional[str] = None):
    limit = max(1, min(limit, 500))
    where, params = [], []
    if before is not None:
        where.append("id < ?")
        params.append(before)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    conn = db.connect()
    try:
        rows = conn.execute(
            f"SELECT id, ts, kind, payload FROM events {clause} "
            f"ORDER BY id DESC LIMIT ?", params + [limit]).fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return {"events": [
                    {"id": r["id"], "ts": r["ts"], "kind": r["kind"],
                     "payload": json.loads(r["payload"])}
                    for r in reversed(rows)],
                "total": total["n"]}
    finally:
        conn.close()


@app.get("/api/replay/{event_id}")
def replay_api(event_id: int):
    conn = db.connect()
    try:
        with db.write_lock():
            return replay.replay_until(conn, event_id)
    except ix.InterlockError as exc:
        return _error(exc)
    finally:
        conn.close()


@app.post("/api/admin/rebuild")
def admin_rebuild():
    conn = db.connect()
    try:
        with db.write_lock():
            conn.execute("BEGIN IMMEDIATE")
            try:
                info = replay.rebuild_from_journal(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"ok": True, **info}
    finally:
        conn.close()


# -- Static frontend --------------------------------------------------------

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
