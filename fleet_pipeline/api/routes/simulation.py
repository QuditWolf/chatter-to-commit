"""
API routes for running and monitoring simulations.
POST /simulate/run      — start a simulation run (async)
GET  /simulate/status/{run_id} — get run stats
GET  /simulate/list     — list all simulation runs
"""
import os
import threading
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db.database import db_conn

router = APIRouter(prefix="/simulate", tags=["simulation"])

_active_runs: dict = {}  # run_id → {"status": ..., "progress": ...}


class SimulateRequest(BaseModel):
    input_path: str
    mock: bool = True
    shift_filter: Optional[str] = None


@router.post("/run")
def start_simulation(req: SimulateRequest, background_tasks: BackgroundTasks):
    """Start a simulation run in the background."""
    if not os.path.exists(req.input_path):
        raise HTTPException(status_code=400, detail=f"File not found: {req.input_path}")

    from fleet_pipeline.simulation.run_simulation import run_simulation

    def _run():
        try:
            result = run_simulation(
                input_path=req.input_path,
                db_path=DB_PATH,
                mock=req.mock,
                shift_filter=req.shift_filter,
            )
            _active_runs[result["run_id"]] = {"status": "done", **result}
        except Exception as e:
            _active_runs["last_error"] = str(e)

    background_tasks.add_task(_run)
    return {"status": "started", "message": "Simulation running in background. Check /simulate/list for results."}


@router.get("/status/{run_id}")
def simulation_status(run_id: str):
    """Get stats for a completed simulation run."""
    with db_conn(DB_PATH) as conn:
        row = conn.execute(
            "SELECT * FROM simulation_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    return dict(row)


@router.get("/list")
def list_simulations():
    """List all simulation runs."""
    with db_conn(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT * FROM simulation_runs ORDER BY started_at DESC LIMIT 50"
        ).fetchall()
    return {"runs": [dict(r) for r in rows]}
