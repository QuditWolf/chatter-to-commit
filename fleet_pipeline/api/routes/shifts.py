"""
Shift routes.

GET  /api/shifts          — list all shifts with event counts
GET  /api/shifts/current  — currently active shift
POST /api/shifts/start    — operator: force-start a new shift now
POST /api/shifts/end      — operator: end the current shift
POST /api/shifts/resume   — operator: reopen the most recently ended shift
POST /api/shifts/create   — manually create a shift with custom start/end times
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fleet_pipeline.config import DB_PATH, WA_CONTROL_GROUP_JID
from fleet_pipeline.db import database as db
from fleet_pipeline.pipeline.shift_detector import (
    operator_start,
    operator_end,
    operator_resume,
    _parse_iso,
)


class CreateShiftRequest(BaseModel):
    started_at: str          # ISO format (IST expected from UI)
    ended_at: Optional[str] = None
    site_id: Optional[str] = None


def _notify_shift(shift: dict, action: str) -> None:
    """Fire-and-forget WA notification for shift state change."""
    if not WA_CONTROL_GROUP_JID:
        return
    try:
        from fleet_pipeline.pipeline.wa_notifier import send_shift_notification

        send_shift_notification(shift, action, WA_CONTROL_GROUP_JID, DB_PATH)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "shift notification failed (%s): %s", action, exc
        )

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
    _notify_shift(shift, "start")
    return {"shift": shift}


@router.post("/end")
async def shift_end():
    """End the currently active shift. Posts summary to WA control group."""
    from fleet_pipeline.api.main import ws_manager
    from fleet_pipeline.config import WA_GROUP_JID

    # Capture shift details before ending (needed for notification)
    with db.db_conn(DB_PATH) as conn:
        current = db.get_active_shift(conn)

    ended = operator_end(DB_PATH)
    if not ended:
        raise HTTPException(status_code=400, detail="No active shift to end")
    await ws_manager.broadcast("shift_changed", {"action": "end"})
    # Post shift-ended notification, then summary to control group
    if current:
        _notify_shift(current, "end")
    try:
        from fleet_pipeline.pipeline.wa_notifier import (
            send_summary_to_group,
            _resolve_group_jid,
        )
        _summary_jid = _resolve_group_jid(WA_CONTROL_GROUP_JID or WA_GROUP_JID)
        if _summary_jid:
            send_summary_to_group(_summary_jid, DB_PATH)
    except Exception as _exc:
        import logging
        logging.getLogger(__name__).warning("shift_end summary post failed: %s", _exc)
    return {"ended": True}


@router.post("/resume")
async def shift_resume():
    """Reopen the most recently ended shift."""
    from fleet_pipeline.api.main import ws_manager

    shift = operator_resume(DB_PATH)
    if not shift:
        raise HTTPException(status_code=400, detail="No previous shift to resume")
    await ws_manager.broadcast("shift_changed", {"action": "resume", "shift": shift})
    _notify_shift(shift, "resume")
    return {"shift": shift}


@router.post("/create")
async def shift_create(req: CreateShiftRequest):
    """Manually create a shift with custom start (and optional end) time.
    Intended for retroactively covering time-adjusted events that had no matching shift.
    Rejects overlapping shifts.
    """
    import json
    import sqlite3
    from uuid import uuid4
    from fleet_pipeline.api.main import ws_manager

    ts_start = _parse_iso(req.started_at)
    if ts_start is None:
        raise HTTPException(status_code=400, detail="Invalid started_at timestamp")

    ts_end = _parse_iso(req.ended_at) if req.ended_at else None
    if req.ended_at and ts_end is None:
        raise HTTPException(status_code=400, detail="Invalid ended_at timestamp")
    if ts_end and ts_end <= ts_start:
        raise HTTPException(status_code=400, detail="ended_at must be after started_at")

    with db.db_conn(DB_PATH) as conn:
        # Reject overlapping shifts
        range_end = ts_end.isoformat() if ts_end else ts_start.isoformat() + "Z"
        overlap = conn.execute(
            """SELECT shift_id, shift_name FROM shifts
               WHERE (is_deleted IS NULL OR is_deleted=0)
                 AND started_at < ?
                 AND (ended_at IS NULL OR ended_at > ?)""",
            (range_end, ts_start.isoformat()),
        ).fetchone()
        if overlap:
            raise HTTPException(
                status_code=409,
                detail=f"Overlaps with existing shift {overlap['shift_name']}",
            )

        date_str = ts_start.strftime("%Y-%m-%d")
        day_count = conn.execute(
            "SELECT COUNT(*) FROM shifts WHERE shift_name LIKE ?",
            (f"{date_str}_%",),
        ).fetchone()[0]
        shift_name = f"{date_str}_{day_count + 1:02d}"
        shift_id = str(uuid4())

        default_site_id = req.site_id or None
        default_site_ids_json = json.dumps([req.site_id]) if req.site_id else None

        conn.execute(
            """INSERT INTO shifts
               (shift_id, shift_number, shift_name, started_at, ended_at,
                detection_method, default_site_id, default_site_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                shift_id,
                day_count + 1,
                shift_name,
                ts_start.isoformat(),
                ts_end.isoformat() if ts_end else None,
                "manual",
                default_site_id,
                default_site_ids_json,
            ),
        )
        conn.execute(
            """INSERT OR IGNORE INTO shift_events
               (shift_event_id, shift_id, status, timestamp_iso, commit_status)
               VALUES (?, ?, ?, ?, ?)""",
            (f"shift_start_{shift_id}", shift_id, "SHIFT_START", ts_start.isoformat(), "COMMITTED"),
        )
        if ts_end:
            conn.execute(
                """INSERT OR IGNORE INTO shift_events
                   (shift_event_id, shift_id, status, timestamp_iso, commit_status)
                   VALUES (?, ?, ?, ?, ?)""",
                (f"shift_end_{shift_id}", shift_id, "SHIFT_END", ts_end.isoformat(), "COMMITTED"),
            )

        # Retroactively assign orphaned events whose timestamp_effective falls
        # within this shift's window and currently have no shift assigned.
        ts_end_param = ts_end.isoformat() if ts_end else "9999-12-31T23:59:59"
        reassigned = conn.execute(
            """UPDATE events
               SET shift_id = ?
               WHERE shift_id IS NULL
                 AND commit_status IN ('COMMITTED', 'FLAGGED', 'HELD')
                 AND timestamp_effective >= ?
                 AND timestamp_effective <= ?""",
            (shift_id, ts_start.isoformat(), ts_end_param),
        ).rowcount

    import logging as _log
    _log.getLogger(__name__).info(
        "[SHIFT] Manual shift %s created — retroactively assigned %d orphaned event(s)",
        shift_name, reassigned,
    )

    await ws_manager.broadcast("shift_changed", {"reason": "manual_create", "shift_id": shift_id})
    return {
        "shift_id": shift_id,
        "shift_name": shift_name,
        "started_at": ts_start.isoformat(),
        "ended_at": ts_end.isoformat() if ts_end else None,
        "events_reassigned": reassigned,
    }


@router.post("/{shift_id}/reassign-orphans")
async def shift_reassign_orphans(shift_id: str):
    """Retroactively assign orphaned events (shift_id IS NULL) that fall within
    this shift's time window. Useful for shifts that were created after events
    were already processed.
    """
    import logging as _log
    from fleet_pipeline.api.main import ws_manager

    with db.db_conn(DB_PATH) as conn:
        shift = conn.execute(
            "SELECT * FROM shifts WHERE shift_id=? AND (is_deleted IS NULL OR is_deleted=0)",
            (shift_id,),
        ).fetchone()
        if not shift:
            raise HTTPException(status_code=404, detail="Shift not found")

        shift = dict(shift)
        ts_end_param = shift["ended_at"] if shift["ended_at"] else "9999-12-31T23:59:59"
        reassigned = conn.execute(
            """UPDATE events
               SET shift_id = ?
               WHERE shift_id IS NULL
                 AND commit_status IN ('COMMITTED', 'FLAGGED', 'HELD')
                 AND timestamp_effective >= ?
                 AND timestamp_effective <= ?""",
            (shift_id, shift["started_at"], ts_end_param),
        ).rowcount

    _log.getLogger(__name__).info(
        "[SHIFT] Reassigned %d orphaned event(s) into shift %s",
        reassigned, shift["shift_name"],
    )
    await ws_manager.broadcast("fleet_state_updated", {"source": "shift_reassign"})
    return {"shift_id": shift_id, "shift_name": shift["shift_name"], "events_reassigned": reassigned}
