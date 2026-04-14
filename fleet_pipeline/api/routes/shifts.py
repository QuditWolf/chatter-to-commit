"""
Shift routes.

GET  /api/shifts          — list all shifts with event counts
GET  /api/shifts/current  — currently active shift
POST /api/shifts/start    — operator: force-start a new shift now
POST /api/shifts/end      — operator: end the current shift
POST /api/shifts/resume   — operator: reopen the most recently ended shift
"""

from fastapi import APIRouter, HTTPException

from fleet_pipeline.config import DB_PATH, WA_CONTROL_GROUP_JID
from fleet_pipeline.db import database as db
from fleet_pipeline.pipeline.shift_detector import (
    operator_start,
    operator_end,
    operator_resume,
)


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
