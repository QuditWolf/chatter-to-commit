"""
Step-through simulation — lets the user pick a historical date and ingest
events one at a time, watching the fleet state update live.

State is held in-process (module-level). One simulation session at a time.

Routes:
  GET  /sim/dates          — list dates that have source data available
  POST /sim/load           — body: {date} — queue up events for that date
  GET  /sim/status         — current position, total, preview of next event
  POST /sim/ingest         — commit the next event in queue, return it
  POST /sim/reset          — clear queue without committing
"""
from uuid import uuid4
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db import database as db
from fleet_pipeline.db.sample_data import SAMPLE_EVENTS, CURRENT_SNAPSHOT, BASE_DATE, NOW_DATE

router = APIRouter(prefix="/sim", tags=["simulation"])

# ── In-process state ──────────────────────────────────────────────────────

_queue:      list = []   # ordered list of event dicts pending ingestion
_pos:        int  = 0    # next index to ingest
_sim_run_id: str  = ""
_sim_date:   str  = ""


def _build_queue(date: str) -> list:
    """Return sorted event dicts for the given date from sample data."""
    if date == BASE_DATE:
        source = SAMPLE_EVENTS
    elif date == NOW_DATE:
        source = CURRENT_SNAPSHOT
    else:
        return []

    events = []
    for row in source:
        truck_id, alias, status, site_id, site_alias, h, m, conf, sender = row
        events.append({
            "truck_id":   truck_id,
            "truck_alias": alias,
            "status":     status,
            "site_id":    site_id,
            "site_alias": site_alias,
            "timestamp":  f"{date}T{h:02d}:{m:02d}:00+05:30",
            "confidence": conf,
            "sender":     sender,
            "hour": h, "minute": m,
        })
    events.sort(key=lambda e: (e["hour"], e["minute"]))
    return events


def _event_preview(ev: dict) -> dict:
    return {
        "truck":     ev["truck_alias"],
        "truck_id":  ev["truck_id"],
        "status":    ev["status"],
        "site":      ev["site_alias"],
        "site_id":   ev["site_id"],
        "time":      ev["timestamp"][11:16],
        "confidence": ev["confidence"],
        "sender":    ev["sender"],
    }


# ── Routes ────────────────────────────────────────────────────────────────

@router.get("/dates")
def available_dates():
    """Return dates that have source events available for step simulation."""
    return {"dates": [BASE_DATE, NOW_DATE]}


class LoadBody(BaseModel):
    date: str


@router.post("/load")
def load_simulation(body: LoadBody):
    global _queue, _pos, _sim_run_id, _sim_date

    events = _build_queue(body.date)
    if not events:
        raise HTTPException(400, f"No source data for date {body.date}")

    _queue      = events
    _pos        = 0
    _sim_date   = body.date
    _sim_run_id = f"step-sim-{body.date}"

    # Clear any prior events from this sim run and ensure run record exists
    with db.db_conn(DB_PATH) as conn:
        conn.execute("DELETE FROM events WHERE simulation_run_id=?", (_sim_run_id,))
        conn.execute(
            "INSERT OR IGNORE INTO simulation_runs (run_id, source_file, notes) VALUES (?,?,?)",
            (_sim_run_id, "step_sim", f"Step simulation for {body.date}"),
        )

    return {
        "loaded":      len(_queue),
        "sim_run_id":  _sim_run_id,
        "date":        body.date,
        "next":        _event_preview(_queue[0]) if _queue else None,
    }


@router.get("/status")
def sim_status():
    remaining = len(_queue) - _pos
    return {
        "loaded":     bool(_queue),
        "date":       _sim_date,
        "pos":        _pos,
        "total":      len(_queue),
        "remaining":  remaining,
        "done":       remaining == 0,
        "next":       _event_preview(_queue[_pos]) if _pos < len(_queue) else None,
        "upcoming":   [_event_preview(_queue[i]) for i in range(_pos, min(_pos+3, len(_queue)))],
    }


@router.post("/ingest")
def ingest_next():
    global _pos

    if not _queue:
        raise HTTPException(400, "No simulation loaded. Call /sim/load first.")
    if _pos >= len(_queue):
        raise HTTPException(400, "All events already ingested.")

    ev = _queue[_pos]
    event_id = str(uuid4())
    msg_id   = str(uuid4())
    conf     = ev["confidence"]

    if conf >= 0.85:
        commit_status = "COMMITTED"
    elif conf >= 0.60:
        commit_status = "FLAGGED"
    else:
        commit_status = "HELD"

    with db.db_conn(DB_PATH) as conn:
        db.insert_raw_message(conn, {
            "msg_id":        msg_id,
            "source_file":   "step_sim",
            "timestamp_iso": ev["timestamp"],
            "sender_name":   ev["sender"],
            "sender_id":     None,
            "raw_text":      f"{ev['truck_alias']} {ev['status']} {ev['site_alias']}",
            "is_edited":     False,
            "is_deleted":    False,
            "media_type":    None,
        })
        db.insert_event(conn, {
            "event_id":            event_id,
            "msg_id":              msg_id,
            "truck_id":            ev["truck_id"],
            "truck_alias":         ev["truck_alias"],
            "status":              ev["status"],
            "site_id":             ev["site_id"],
            "site_alias":          ev["site_alias"],
            "material":            None,
            "timestamp_effective": ev["timestamp"],
            "inferred":            False,
            "confidence":          conf,
            "reasoning":           "step simulation",
            "commit_status":       commit_status,
            "processing_id":       _sim_run_id,
            "simulation_run_id":   _sim_run_id,
        })
        db.log_audit(conn, "INSERT", "events", event_id, new_value={"commit_status": commit_status})

    _pos += 1

    return {
        "ingested":  _event_preview(ev),
        "commit_status": commit_status,
        "pos":       _pos,
        "total":     len(_queue),
        "remaining": len(_queue) - _pos,
        "next":      _event_preview(_queue[_pos]) if _pos < len(_queue) else None,
    }


@router.post("/reset")
def reset_simulation():
    global _queue, _pos, _sim_run_id, _sim_date
    if _sim_run_id:
        with db.db_conn(DB_PATH) as conn:
            conn.execute("DELETE FROM events WHERE simulation_run_id=?", (_sim_run_id,))
    _queue, _pos, _sim_run_id, _sim_date = [], 0, "", ""
    return {"status": "reset"}
