"""
Registry management routes (Admin tab).

GET  /api/registry/trucks
POST /api/registry/trucks
PUT  /api/registry/trucks/:id
GET  /api/registry/sites
POST /api/registry/sites
PUT  /api/registry/sites/:id
GET  /api/registry/shifts-config
PUT  /api/registry/shifts-config/:shift_number
"""
import json
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db import database as db

router = APIRouter(prefix="/api/registry", tags=["registry"])


# ── Pydantic models ──────────────────────────────────────────────────────────

class TruckCreate(BaseModel):
    truck_id: str
    display_name: str
    aliases: List[str] = []

class TruckUpdate(BaseModel):
    display_name: Optional[str] = None
    aliases: Optional[List[str]] = None
    is_active: Optional[bool] = None

class SiteCreate(BaseModel):
    site_id: str
    display_name: str
    site_type: str   # loading | unloading | depot
    aliases: List[str] = []

class SiteUpdate(BaseModel):
    display_name: Optional[str] = None
    site_type: Optional[str] = None
    aliases: Optional[List[str]] = None
    is_active: Optional[bool] = None

class ShiftConfigUpdate(BaseModel):
    start_time: str
    expected_end: Optional[str] = None
    wa_keyword: Optional[str] = None


# ── Trucks ───────────────────────────────────────────────────────────────────

@router.get("/trucks")
def list_trucks():
    with db.db_conn(DB_PATH) as conn:
        rows = conn.execute("SELECT * FROM trucks ORDER BY truck_id").fetchall()
    trucks = []
    for r in rows:
        t = dict(r)
        if isinstance(t.get("aliases"), str):
            try:
                t["aliases"] = json.loads(t["aliases"])
            except Exception:
                t["aliases"] = []
        trucks.append(t)
    return {"trucks": trucks}


@router.post("/trucks", status_code=201)
def create_truck(req: TruckCreate):
    with db.db_conn(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT truck_id FROM trucks WHERE truck_id=?", (req.truck_id,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Truck {req.truck_id!r} already exists")
        db.create_truck(conn, req.truck_id, req.display_name, req.aliases)
    return {"truck_id": req.truck_id, "created": True}


@router.put("/trucks/{truck_id}")
def update_truck(truck_id: str, req: TruckUpdate):
    with db.db_conn(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM trucks WHERE truck_id=?", (truck_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Truck {truck_id!r} not found")

        updates = {}
        if req.display_name is not None:
            updates["display_name"] = req.display_name
        if req.aliases is not None:
            updates["aliases"] = json.dumps(req.aliases)
        if req.is_active is not None:
            updates["is_active"] = req.is_active

        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE trucks SET {set_clause} WHERE truck_id=?",
                list(updates.values()) + [truck_id],
            )
    return {"truck_id": truck_id, "updated": True}


# ── Sites ────────────────────────────────────────────────────────────────────

@router.get("/sites")
def list_sites():
    with db.db_conn(DB_PATH) as conn:
        rows = conn.execute("SELECT * FROM sites ORDER BY site_id").fetchall()
    sites = []
    for r in rows:
        s = dict(r)
        if isinstance(s.get("aliases"), str):
            try:
                s["aliases"] = json.loads(s["aliases"])
            except Exception:
                s["aliases"] = []
        sites.append(s)
    return {"sites": sites}


@router.post("/sites", status_code=201)
def create_site(req: SiteCreate):
    with db.db_conn(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT site_id FROM sites WHERE site_id=?", (req.site_id,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Site {req.site_id!r} already exists")
        db.insert_site(conn, {
            "site_id": req.site_id,
            "display_name": req.display_name,
            "site_type": req.site_type,
            "aliases": req.aliases,
            "is_active": True,
        })
    return {"site_id": req.site_id, "created": True}


@router.put("/sites/{site_id}")
def update_site(site_id: str, req: SiteUpdate):
    with db.db_conn(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Site {site_id!r} not found")

        updates = {}
        if req.display_name is not None:
            updates["display_name"] = req.display_name
        if req.site_type is not None:
            updates["site_type"] = req.site_type
        if req.aliases is not None:
            updates["aliases"] = json.dumps(req.aliases)
        if req.is_active is not None:
            updates["is_active"] = req.is_active

        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE sites SET {set_clause} WHERE site_id=?",
                list(updates.values()) + [site_id],
            )
    return {"site_id": site_id, "updated": True}


# ── Shift config ─────────────────────────────────────────────────────────────

@router.get("/shifts-config")
def get_shifts_config():
    with db.db_conn(DB_PATH) as conn:
        config = db.get_shift_config(conn)
    return {"shift_config": config}


@router.put("/shifts-config/{shift_number}")
def update_shift_config_route(shift_number: int, req: ShiftConfigUpdate):
    with db.db_conn(DB_PATH) as conn:
        row = conn.execute(
            "SELECT shift_number FROM shift_config WHERE shift_number=?", (shift_number,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Shift {shift_number} not found in config")
        db.update_shift_config(conn, shift_number, req.start_time, req.expected_end, req.wa_keyword)
    return {"shift_number": shift_number, "updated": True}
