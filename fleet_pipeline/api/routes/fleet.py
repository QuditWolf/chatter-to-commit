"""
API routes for fleet state queries.
GET /fleet/state       — current status of all trucks (latest committed event per truck)
GET /fleet/kpis        — 4 KPI tiles + site summary (< 100ms, in-memory cache)
GET /fleet/truck/{id}  — recent events for a specific truck
GET /fleet/events      — recent committed events (feed)
"""
import time
from fastapi import APIRouter, Query
from typing import List

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db.database import (
    db_conn, get_fleet_state, get_committed_events,
    get_recent_events_for_truck, get_fleet_kpis, get_site_load_summary,
)

router = APIRouter(prefix="/fleet", tags=["fleet"])

# Simple in-memory cache for KPIs (invalidated on WS commit events)
_kpi_cache: dict = {"data": None, "ts": 0.0}
_KPI_TTL = 5.0  # seconds fallback TTL


def invalidate_kpi_cache():
    """Call this whenever a new commit is created."""
    _kpi_cache["data"] = None
    _kpi_cache["ts"] = 0.0


@router.get("/state")
def fleet_state():
    """Current status of all active trucks (latest committed event per truck)."""
    with db_conn(DB_PATH) as conn:
        state = get_fleet_state(conn)
    return {"trucks": state, "count": len(state)}


@router.get("/kpis")
def fleet_kpis():
    """
    4 KPI tile values + site load summary.
    Served from in-memory cache; cache is invalidated on each new commit.
    Falls back to TTL of 5s for polling clients.
    """
    now = time.time()
    if _kpi_cache["data"] is not None and (now - _kpi_cache["ts"]) < _KPI_TTL:
        return _kpi_cache["data"]

    with db_conn(DB_PATH) as conn:
        kpis = get_fleet_kpis(conn)
        site_summary = get_site_load_summary(conn)
        # Shift summary from active shift
        active_shift = conn.execute(
            "SELECT * FROM shifts WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        shift_data = dict(active_shift) if active_shift else None
        if shift_data:
            shift_data["event_count"] = conn.execute(
                "SELECT COUNT(*) FROM events WHERE shift_id=? AND commit_status='COMMITTED'",
                (shift_data["shift_id"],),
            ).fetchone()[0]

    result = {
        "kpis": kpis,
        "site_summary": site_summary,
        "active_shift": shift_data,
    }
    _kpi_cache["data"] = result
    _kpi_cache["ts"] = now
    return result


@router.get("/truck/{truck_id}")
def truck_detail(truck_id: str, limit: int = Query(default=10, le=100)):
    """Recent committed events for a specific truck."""
    with db_conn(DB_PATH) as conn:
        events = get_recent_events_for_truck(conn, truck_id, limit=limit)
    return {"truck_id": truck_id, "events": events, "count": len(events)}


@router.get("/events")
def recent_events(limit: int = Query(default=50, le=500)):
    """Recent committed events across all trucks."""
    with db_conn(DB_PATH) as conn:
        events = get_committed_events(conn, limit=limit)
    return {"events": events, "count": len(events)}
