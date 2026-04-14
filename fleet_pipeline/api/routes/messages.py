"""
Message-to-commit map and corrections.

GET  /api/messages                    — paginated message-commit map
GET  /api/messages/:wa_message_id     — single message + commit + corrections
POST /api/commits/:commit_id/correct  — submit correction
GET  /api/commits/:commit_id/corrections — list corrections for a commit
"""

import re

from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db import database as db
from fleet_pipeline.utils import now_ist_iso

router = APIRouter(tags=["messages"])


class CorrectionRequest(BaseModel):
    field: str  # truck_id | status | site_id | shift_id
    corrected_value: str
    note: Optional[str] = None
    corrected_by: Optional[str] = "operator"


class MapEventRequest(BaseModel):
    truck_id: str
    status: str
    site_id: str
    mapped_by: Optional[str] = "operator"


class ManualCommitRequest(BaseModel):
    truck_id: str
    status: str
    site_id: str
    msg_id: Optional[str] = None  # link to existing raw message
    timestamp_effective: Optional[str] = None  # ISO8601; defaults to now
    note: Optional[str] = None
    created_by: Optional[str] = "operator"


class EditCommitRequest(BaseModel):
    truck_id: Optional[str] = None
    status: Optional[str] = None
    site_id: Optional[str] = None
    timestamp_effective: Optional[str] = None
    note: Optional[str] = None
    edited_by: Optional[str] = "operator"


@router.get("/api/commits-log")
def list_commits(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    truck_id: str = Query(""),
    site_id: str = Query(""),
    status: str = Query(""),  # ENTER | LS | LO | LEFT | US | UO
    commit_status: str = Query(""),  # COMMITTED | FLAGGED | HELD
    search: str = Query(""),
    shift_id: str = Query(""),
):
    """
    Ordered commit log (start → end).
    Returns events with their source message and a human-readable commit_source label.
    """
    where = ["e.commit_status IN ('COMMITTED','FLAGGED','HELD')"]
    params: list = []

    if truck_id:
        where.append("e.truck_id = ?")
        params.append(truck_id)
    if site_id:
        where.append("e.site_id = ?")
        params.append(site_id)
    if status:
        where.append("e.status = ?")
        params.append(status.upper())
    if commit_status:
        where.append("e.commit_status = ?")
        params.append(commit_status.upper())
    if shift_id:
        where.append("e.shift_id = ?")
        params.append(shift_id)
    if search:
        like = f"%{search}%"
        where.append(
            "(r.raw_text LIKE ? OR e.truck_id LIKE ? OR e.site_id LIKE ? OR e.reasoning LIKE ?)"
        )
        params.extend([like, like, like, like])

    where_sql = "WHERE " + " AND ".join(where)

    with db.db_conn(DB_PATH) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM events e LEFT JOIN raw_messages r ON r.msg_id=e.msg_id {where_sql}",
            params,
        ).fetchone()[0]

        rows = conn.execute(
            f"""SELECT e.event_id, e.msg_id, e.truck_id, e.truck_alias,
                       e.status, e.site_id, e.site_alias, e.confidence,
                       e.commit_status, e.commit_path, e.inferred, e.reasoning,
                       e.corrected, e.corrected_at, e.shift_id,
                       e.timestamp_effective, e.timestamp_approximate, e.created_at,
                       r.raw_text, r.sender_name, r.timestamp_iso,
                       r.quoted_wa_message_id,
                       qr.raw_text as quoted_raw_text,
                       t.display_name as truck_name,
                       s.display_name as site_name
                FROM events e
                LEFT JOIN raw_messages r  ON r.msg_id  = e.msg_id
                LEFT JOIN raw_messages qr ON qr.msg_id = r.quoted_wa_message_id
                LEFT JOIN trucks t ON t.truck_id = e.truck_id
                LEFT JOIN sites  s ON s.site_id  = e.site_id
                {where_sql}
                ORDER BY e.timestamp_effective DESC, e.created_at DESC
                LIMIT ? OFFSET ?""",
            params + [limit, (page - 1) * limit],
        ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        # Derive human-readable commit source label
        cp = d.get("commit_path") or ""
        corrected = bool(d.get("corrected"))
        inferred = bool(d.get("inferred"))
        has_msg = bool(d.get("raw_text") or d.get("msg_id"))
        if cp == "manual" and not has_msg:
            source = "Manual entry"
        elif cp == "manual" and corrected:
            source = "Human confirmed"
        elif cp == "manual":
            source = "Manual map"
        elif corrected:
            source = "LLM parsed · corrected"
        elif inferred and cp == "green":
            source = "LLM inferred · auto"
        elif cp == "green":
            source = "LLM auto"
        elif cp == "amber":
            source = "LLM flagged"
        elif cp == "red":
            source = "LLM held"
        else:
            source = cp or "–"
        d["commit_source"] = source
        items.append(d)

    return {
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
        "items": items,
    }


@router.get("/api/messages")
def list_messages(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: str = Query("all"),  # all | committed | held | flagged | corrected
    search: str = Query(""),
    hide_noise: bool = Query(False),
    shift_id: str = Query(""),
):
    """Paginated message-commit map for the operator panel."""
    with db.db_conn(DB_PATH) as conn:
        result = db.get_messages_page(
            conn,
            page=page,
            limit=limit,
            status_filter=status,
            search=search,
            hide_noise=hide_noise,
            shift_id=shift_id,
        )
    return result


@router.get("/api/messages/{wa_message_id}")
def get_message(wa_message_id: str):
    """Single message with its commit and correction history."""
    with db.db_conn(DB_PATH) as conn:
        row = conn.execute(
            "SELECT * FROM wa_messages WHERE wa_message_id=?", (wa_message_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Message not found")
        msg = dict(row)
        event = conn.execute(
            "SELECT * FROM events WHERE wa_message_id=?", (wa_message_id,)
        ).fetchone()
        msg["event"] = dict(event) if event else None
        if event:
            msg["corrections"] = db.get_corrections_for_event(conn, event["event_id"])
    return msg


@router.post("/api/commits/{commit_id}/correct")
def correct_commit(commit_id: str, req: CorrectionRequest):
    """
    Submit a correction to a committed or flagged event.
    Never overwrites the original — appends to corrections table.
    """
    valid_fields = {"truck_id", "status", "site_id", "shift_id"}
    if req.field not in valid_fields:
        raise HTTPException(
            status_code=400, detail=f"field must be one of {valid_fields}"
        )

    with db.db_conn(DB_PATH) as conn:
        event = conn.execute(
            "SELECT * FROM events WHERE event_id=?", (commit_id,)
        ).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Commit not found")

        event_dict = dict(event)
        if event_dict["commit_status"] not in ("COMMITTED", "FLAGGED", "HELD"):
            raise HTTPException(
                status_code=400,
                detail=f"Can only correct COMMITTED, FLAGGED, or HELD events, got {event_dict['commit_status']}",
            )

        original_value = event_dict.get(req.field)
        correction_id = str(uuid4())
        now = now_ist_iso()

        db.insert_correction(
            conn,
            {
                "correction_id": correction_id,
                "original_event_id": commit_id,
                "corrected_by": req.corrected_by or "operator",
                "corrected_at": now,
                "field_changed": req.field,
                "original_value": str(original_value)
                if original_value is not None
                else None,
                "corrected_value": req.corrected_value,
                "note": req.note,
            },
        )

        db.log_audit(
            conn,
            "CORRECTION",
            "events",
            commit_id,
            old_value={req.field: original_value},
            new_value={req.field: req.corrected_value},
            triggered_by=req.corrected_by or "operator",
        )

    return {
        "correction_id": correction_id,
        "commit_id": commit_id,
        "field": req.field,
        "original_value": original_value,
        "corrected_value": req.corrected_value,
    }


@router.post("/api/events/{event_id}/force-commit")
async def force_commit_event(event_id: str):
    """Force-commit a FLAGGED or low-confidence HELD event to COMMITTED status."""
    from fleet_pipeline.api.main import ws_manager
    from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

    now = now_ist_iso()

    with db.db_conn(DB_PATH) as conn:
        event = conn.execute(
            "SELECT * FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        ev = dict(event)
        if ev["commit_status"] not in ("FLAGGED", "HELD"):
            raise HTTPException(
                status_code=400,
                detail=f"Can only force-commit FLAGGED or HELD events, got {ev['commit_status']}",
            )

        conn.execute(
            "UPDATE events SET commit_status='COMMITTED', commit_path='manual', corrected=1, corrected_at=? WHERE event_id=?",
            (now, event_id),
        )
        db.log_audit(
            conn,
            "FORCE_COMMIT",
            "events",
            event_id,
            old_value={"commit_status": ev["commit_status"]},
            new_value={"commit_status": "COMMITTED"},
            triggered_by="operator",
        )

    invalidate_kpi_cache()
    await ws_manager.broadcast("fleet_state_updated", {"source": "force_commit"})
    return {"event_id": event_id, "committed": True}


VALID_STATUSES = {"ENTER", "LS", "LO", "LEFT", "US", "UO"}


@router.post("/api/events/{event_id}/map")
async def map_event(event_id: str, req: MapEventRequest):
    """
    Manually map a HELD/unmapped event to a truck, status, site.
    Used when LLM was offline and the message needs a human to classify it.
    """
    from fleet_pipeline.api.main import ws_manager
    from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

    if req.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400, detail=f"status must be one of {VALID_STATUSES}"
        )

    now = now_ist_iso()

    with db.db_conn(DB_PATH) as conn:
        event = conn.execute(
            "SELECT * FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        if dict(event)["commit_status"] != "HELD":
            raise HTTPException(
                status_code=400, detail="Only HELD events can be manually mapped"
            )

        conn.execute(
            """UPDATE events SET
               truck_id=?, status=?, site_id=?,
               commit_status='COMMITTED', commit_path='manual',
               corrected=1, corrected_at=?
               WHERE event_id=?""",
            (req.truck_id, req.status, req.site_id, now, event_id),
        )

        # Dismiss any open HITL questions for this event
        conn.execute(
            "UPDATE hitl_queue SET status='ANSWERED', answer='manual_map' WHERE context LIKE ? AND status='OPEN'",
            (f"%{event_id}%",),
        )

        db.insert_correction(
            conn,
            {
                "correction_id": str(uuid4()),
                "original_event_id": event_id,
                "corrected_by": req.mapped_by or "operator",
                "corrected_at": now,
                "field_changed": "manual_map",
                "original_value": None,
                "corrected_value": f"{req.truck_id} {req.status} {req.site_id}",
                "note": "Manually mapped by operator",
            },
        )

    invalidate_kpi_cache()
    await ws_manager.broadcast("fleet_state_updated", {"source": "manual_map"})
    return {
        "event_id": event_id,
        "mapped": True,
        "truck_id": req.truck_id,
        "status": req.status,
        "site_id": req.site_id,
    }


@router.get("/api/commits/{commit_id}/corrections")
def get_commit_corrections(commit_id: str):
    """List all corrections for a given commit."""
    with db.db_conn(DB_PATH) as conn:
        corrections = db.get_corrections_for_event(conn, commit_id)
    return {"commit_id": commit_id, "corrections": corrections}


@router.post("/api/commits", status_code=201)
async def create_manual_commit(req: ManualCommitRequest):
    """
    Create an arbitrary manual commit (no source message).
    Appears in the commits log with commit_source='Manual entry'.
    """
    from fleet_pipeline.api.main import ws_manager
    from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

    if req.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400, detail=f"status must be one of {VALID_STATUSES}"
        )

    now = now_ist_iso()
    event_id = str(uuid4())

    with db.db_conn(DB_PATH) as conn:
        # Verify truck and site exist
        truck = conn.execute(
            "SELECT truck_id FROM trucks WHERE truck_id=?", (req.truck_id,)
        ).fetchone()
        if not truck:
            raise HTTPException(
                status_code=400, detail=f"Unknown truck_id: {req.truck_id}"
            )
        site = conn.execute(
            "SELECT site_id FROM sites WHERE site_id=?", (req.site_id,)
        ).fetchone()
        if not site:
            raise HTTPException(
                status_code=400, detail=f"Unknown site_id: {req.site_id}"
            )

        # If msg_id provided, verify it exists and optionally borrow its timestamp
        linked_msg_id = None
        if req.msg_id:
            msg_row = conn.execute(
                "SELECT msg_id, timestamp_iso FROM raw_messages WHERE msg_id=?",
                (req.msg_id,),
            ).fetchone()
            if not msg_row:
                raise HTTPException(
                    status_code=404, detail=f"Message not found: {req.msg_id}"
                )
            linked_msg_id = msg_row["msg_id"]

        ts = req.timestamp_effective or now

        # Get shift for this timestamp
        shift_row = conn.execute(
            "SELECT shift_id FROM shifts WHERE started_at <= ? ORDER BY started_at DESC LIMIT 1",
            (ts,),
        ).fetchone()
        shift_id = shift_row[0] if shift_row else None

        # Derive short alias: use first alias from trucks.aliases JSON, else strip leading T
        truck_row = conn.execute(
            "SELECT display_name, aliases FROM trucks WHERE truck_id=?", (req.truck_id,)
        ).fetchone()
        if truck_row and truck_row["aliases"]:
            import json as _json

            try:
                aliases = _json.loads(truck_row["aliases"])
                truck_alias = aliases[0] if aliases else req.truck_id
            except Exception:
                truck_alias = req.truck_id
        else:
            truck_alias = (
                req.truck_id[1:]
                if re.match(r"^T[A-Z0-9]{1,2}$", req.truck_id)
                else req.truck_id
            )

        site_row = conn.execute(
            "SELECT display_name FROM sites WHERE site_id=?", (req.site_id,)
        ).fetchone()
        site_alias = site_row["display_name"] if site_row else req.site_id

        db.insert_event(
            conn,
            {
                "event_id": event_id,
                "msg_id": linked_msg_id,
                "truck_id": req.truck_id,
                "truck_alias": truck_alias,
                "status": req.status,
                "site_id": req.site_id,
                "site_alias": site_alias,
                "material": None,
                "timestamp_effective": ts,
                "inferred": False,
                "confidence": 1.0,
                "reasoning": req.note or "Manually entered by operator",
                "commit_status": "COMMITTED",
                "commit_path": "manual",
                "shift_id": shift_id,
            },
        )
        conn.execute(
            "UPDATE events SET corrected=1, corrected_at=? WHERE event_id=?",
            (now, event_id),
        )
        db.log_audit(
            conn,
            "MANUAL_ENTRY",
            "events",
            event_id,
            new_value={
                "truck_id": req.truck_id,
                "status": req.status,
                "site_id": req.site_id,
            },
            triggered_by=req.created_by or "operator",
        )

    invalidate_kpi_cache()
    await ws_manager.broadcast("fleet_state_updated", {"source": "manual_entry"})
    return {"event_id": event_id, "created": True}


@router.patch("/api/commits/{commit_id}")
async def edit_commit(commit_id: str, req: EditCommitRequest):
    """
    Edit an existing commit's truck, status, site, or timestamp.
    Original values are preserved in the corrections table.
    """
    from fleet_pipeline.api.main import ws_manager
    from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

    if req.status and req.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400, detail=f"status must be one of {VALID_STATUSES}"
        )

    now = now_ist_iso()

    with db.db_conn(DB_PATH) as conn:
        event = conn.execute(
            "SELECT * FROM events WHERE event_id=?", (commit_id,)
        ).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Commit not found")
        ev = dict(event)

        # Build update
        updates = {}
        if req.truck_id is not None:
            updates["truck_id"] = req.truck_id
        if req.status is not None:
            updates["status"] = req.status
        if req.site_id is not None:
            updates["site_id"] = req.site_id
        if req.timestamp_effective is not None:
            updates["timestamp_effective"] = req.timestamp_effective

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        set_sql = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE events SET {set_sql}, corrected=1, corrected_at=? WHERE event_id=?",
            list(updates.values()) + [now, commit_id],
        )

        # Log each changed field as a correction
        for field, new_val in updates.items():
            db.insert_correction(
                conn,
                {
                    "correction_id": str(uuid4()),
                    "original_event_id": commit_id,
                    "corrected_by": req.edited_by or "operator",
                    "corrected_at": now,
                    "field_changed": field,
                    "original_value": str(ev.get(field))
                    if ev.get(field) is not None
                    else None,
                    "corrected_value": str(new_val),
                    "note": req.note,
                },
            )

        db.log_audit(
            conn,
            "EDIT_COMMIT",
            "events",
            commit_id,
            old_value={k: ev.get(k) for k in updates},
            new_value=updates,
            triggered_by=req.edited_by or "operator",
        )

    invalidate_kpi_cache()
    await ws_manager.broadcast("fleet_state_updated", {"source": "edit_commit"})
    return {"event_id": commit_id, "updated": True, "fields": list(updates.keys())}


@router.delete("/api/commits/{commit_id}")
async def delete_commit(commit_id: str):
    """Mark a commit as DELETED (soft delete — never physically removed)."""
    from fleet_pipeline.api.main import ws_manager
    from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

    with db.db_conn(DB_PATH) as conn:
        event = conn.execute(
            "SELECT * FROM events WHERE event_id=?", (commit_id,)
        ).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Commit not found")
        ev = dict(event)
        if ev["commit_status"] == "DELETED":
            raise HTTPException(status_code=400, detail="Already deleted")

        conn.execute(
            "UPDATE events SET commit_status='DELETED' WHERE event_id=?", (commit_id,)
        )
        db.log_audit(
            conn,
            "DELETE_COMMIT",
            "events",
            commit_id,
            old_value={"commit_status": ev["commit_status"]},
            new_value={"commit_status": "DELETED"},
            triggered_by="operator",
        )

    invalidate_kpi_cache()
    await ws_manager.broadcast("fleet_state_updated", {"source": "delete_commit"})
    return {"event_id": commit_id, "deleted": True}


# ── Truck merge ──────────────────────────────────────────────────────────────


class MergeRequest(BaseModel):
    src_id: str
    dst_id: str


def _resolve_truck_id(conn, alias_or_id: str) -> Optional[str]:
    """Resolve a truck alias or ID to a truck_id."""
    row = conn.execute(
        "SELECT truck_id FROM trucks WHERE truck_id=? COLLATE NOCASE AND is_active=1",
        (alias_or_id,),
    ).fetchone()
    if row:
        return row[0]
    for r in conn.execute("SELECT truck_id, aliases FROM trucks WHERE is_active=1"):
        try:
            import json

            aliases = json.loads(r["aliases"] or "[]")
        except Exception:
            aliases = []
        if alias_or_id.lower() in [a.lower() for a in aliases]:
            return r[0]
    return None


@router.post("/api/trucks/merge")
def merge_trucks(req: MergeRequest):
    """
    Merge src truck into dst truck.
    Copies all src aliases to dst, reassigns all src events to dst,
    then deactivates the src truck.
    """
    src_raw = req.src_id.strip()
    dst_raw = req.dst_id.strip()

    if not src_raw or not dst_raw:
        raise HTTPException(status_code=400, detail="src_id and dst_id are required")
    if src_raw.lower() == dst_raw.lower():
        raise HTTPException(status_code=400, detail="src and dst cannot be the same")

    with db.db_conn(DB_PATH) as conn:
        src_id = _resolve_truck_id(conn, src_raw)
        dst_id = _resolve_truck_id(conn, dst_raw)

        if not src_id:
            raise HTTPException(
                status_code=404, detail=f"Source trolley '{src_raw}' not found"
            )
        if not dst_id:
            raise HTTPException(
                status_code=404, detail=f"Destination trolley '{dst_raw}' not found"
            )

        result = db.merge_trucks(conn, src_id, dst_id)

    # Invalidate caches and broadcast
    try:
        from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

        invalidate_kpi_cache()
    except Exception:
        pass
    try:
        from fleet_pipeline.api.main import ws_manager
        import asyncio

        asyncio.create_task(
            ws_manager.broadcast("fleet_state_updated", {"source": "merge_trucks"})
        )
    except Exception:
        pass

    return {
        "merged": True,
        "src_id": src_id,
        "dst_id": dst_id,
        "aliases_added": result.get("aliases_added", []),
        "events_reassigned": result.get("events_reassigned", 0),
    }


# ── Shift events ───────────────────────────────────────────────


@router.get("/api/shift-events")
def list_shift_events(
    shift_id: str = Query(""),
):
    """List shift events (SHIFT_START/SHIFT_END) for the UI."""
    with db.db_conn(DB_PATH) as conn:
        if shift_id:
            rows = conn.execute(
                """SELECT se.*, s.shift_name
                   FROM shift_events se
                   JOIN shifts s ON s.shift_id = se.shift_id
                   WHERE se.shift_id = ?
                   ORDER BY se.timestamp_iso""",
                (shift_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT se.*, s.shift_name
                   FROM shift_events se
                   JOIN shifts s ON s.shift_id = se.shift_id
                   ORDER BY se.timestamp_iso DESC
                   LIMIT 100""",
            ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/api/shifts/{shift_id}/delete")
def delete_shift(shift_id: str, mode: str = "orphan"):
    """Soft-delete a shift.

    mode=orphan  (default): keep events but detach them from this shift
                            (events.shift_id → NULL, shift marked is_deleted=1)
    mode=cascade           : soft-delete all events too
                            (events.commit_status → DELETED, shift marked is_deleted=1)
    """
    if mode not in ("orphan", "cascade"):
        raise HTTPException(400, "mode must be 'orphan' or 'cascade'")

    with db.db_conn(DB_PATH) as conn:
        shift = conn.execute(
            "SELECT * FROM shifts WHERE shift_id=?", (shift_id,)
        ).fetchone()
        if not shift:
            raise HTTPException(404, "Shift not found")

        if mode == "orphan":
            # Detach events — they become shift-less but are preserved
            conn.execute(
                "UPDATE events SET shift_id=NULL WHERE shift_id=?", (shift_id,)
            )
        else:
            # Soft-delete all events in this shift
            conn.execute(
                "UPDATE events SET commit_status='DELETED' WHERE shift_id=?", (shift_id,)
            )

        # Soft-delete the shift_events rows
        conn.execute(
            "UPDATE shift_events SET commit_status='DELETED' WHERE shift_id=?", (shift_id,)
        )
        # Soft-delete the shift itself
        conn.execute(
            "UPDATE shifts SET is_deleted=1 WHERE shift_id=?", (shift_id,)
        )

    return {"deleted": True, "mode": mode}


@router.post("/api/shifts/merge")
def merge_shifts(src_shift_id: str, dst_shift_id: str):
    """Merge src shift into dst shift. Result: dst has earliest start, latest end, all events."""
    with db.db_conn(DB_PATH) as conn:
        src = conn.execute(
            "SELECT * FROM shifts WHERE shift_id=?", (src_shift_id,)
        ).fetchone()
        dst = conn.execute(
            "SELECT * FROM shifts WHERE shift_id=?", (dst_shift_id,)
        ).fetchone()
        if not src or not dst:
            raise HTTPException(400, "Shift not found")

        # Determine merge order: find which started earlier
        if src["started_at"] <= dst["started_at"]:
            earlier = dict(src)
            later = dict(dst)
        else:
            earlier = dict(dst)
            later = dict(src)

        # Update dst shift with new boundaries
        conn.execute(
            """UPDATE shifts SET started_at=?, ended_at=?
               WHERE shift_id=?""",
            (
                earlier["started_at"],
                later["ended_at"] if later["ended_at"] else None,
                dst_shift_id,
            ),
        )

        # Move all events and shift_events
        conn.execute(
            "UPDATE events SET shift_id=? WHERE shift_id=?",
            (dst_shift_id, src_shift_id),
        )
        conn.execute(
            "UPDATE shift_events SET shift_id=? WHERE shift_id=?",
            (dst_shift_id, src_shift_id),
        )

        # Update shift_event times to match new boundaries
        if earlier["started_at"]:
            conn.execute(
                "UPDATE shift_events SET timestamp_iso=? WHERE shift_id=? AND status='SHIFT_START'",
                (earlier["started_at"], dst_shift_id),
            )
        if later["ended_at"]:
            conn.execute(
                "UPDATE shift_events SET timestamp_iso=? WHERE shift_id=? AND status='SHIFT_END'",
                (later["ended_at"], dst_shift_id),
            )

        # Delete src shift
        conn.execute("DELETE FROM shifts WHERE shift_id=?", (src_shift_id,))

    return {"merged": True, "src": src_shift_id, "dst": dst_shift_id}
