"""
Shift routes.

GET  /api/shifts          — list all shifts with event counts
GET  /api/shifts/current  — currently active shift
POST /api/shifts/start    — operator: force-start a new shift now
POST /api/shifts/end      — operator: end the current shift
POST /api/shifts/resume   — operator: reopen the most recently ended shift
"""
from fastapi import APIRouter, HTTPException

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db import database as db
from fleet_pipeline.pipeline.shift_detector import operator_start, operator_end, operator_resume

router = APIRouter(prefix="/api/shifts", tags=["shifts"])


@router.get("")
def list_shifts():
    with db.db_conn(DB_PATH) as conn:
        shifts = db.get_all_shifts(conn)
        for shift in shifts:
            count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE shift_id=? AND commit_status='COMMITTED'",
                (shift["shift_id"],),
            ).fetchone()[0]
            shift["event_count"] = count
    return {"shifts": shifts, "count": len(shifts)}


@router.get("/current")
def current_shift():
    with db.db_conn(DB_PATH) as conn:
        shift = db.get_active_shift(conn)
        if shift:
            shift["event_count"] = conn.execute(
                "SELECT COUNT(*) FROM events WHERE shift_id=? AND commit_status='COMMITTED'",
                (shift["shift_id"],),
            ).fetchone()[0]
            shift["message_count"] = conn.execute(
                "SELECT COUNT(*) FROM raw_messages WHERE timestamp_iso >= ?",
                (shift["started_at"],),
            ).fetchone()[0]
    return {"shift": shift}


@router.post("/start")
async def shift_start():
    """Force-start a new shift immediately."""
    from fleet_pipeline.api.main import ws_manager
    shift = operator_start(DB_PATH)
    await ws_manager.broadcast("shift_changed", {"action": "start", "shift": shift})
    return {"shift": shift}


@router.post("/end")
async def shift_end():
    """End the currently active shift."""
    from fleet_pipeline.api.main import ws_manager
    ended = operator_end(DB_PATH)
    if not ended:
        raise HTTPException(status_code=400, detail="No active shift to end")
    await ws_manager.broadcast("shift_changed", {"action": "end"})
    return {"ended": True}


@router.post("/resume")
async def shift_resume():
    """Reopen the most recently ended shift."""
    from fleet_pipeline.api.main import ws_manager
    shift = operator_resume(DB_PATH)
    if not shift:
        raise HTTPException(status_code=400, detail="No previous shift to resume")
    await ws_manager.broadcast("shift_changed", {"action": "resume", "shift": shift})
    return {"shift": shift}
