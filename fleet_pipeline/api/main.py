"""
Fleet Pipeline — FastAPI application.

Start with:
    uvicorn fleet_pipeline.api.main:app --reload --port 8000

Or:
    python -m fleet_pipeline.api.main
"""

import asyncio
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Set

# Ensure project root is on path when run directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

from fleet_pipeline.config import API_HOST, API_PORT, BASE_DIR, LLM_MOCK, LLM_BASE_URL

from fleet_pipeline.api.auth import auth_enabled, verify_token
from fleet_pipeline.api.routes import fleet, hitl, simulation, analytics
from fleet_pipeline.api.routes import step_sim
from fleet_pipeline.api.routes import messages as messages_route
from fleet_pipeline.api.routes import registry as registry_route
from fleet_pipeline.api.routes import shifts as shifts_route
from fleet_pipeline.api.routes import ingest as ingest_route
from fleet_pipeline.api.routes import status as status_route
from fleet_pipeline.api.agent_query import agent_answer


def _setup_file_logging() -> None:
    """Add a rotating file handler to all relevant loggers so logs land in LOGS_DIR/api.log."""
    from fleet_pipeline.config import LOGS_DIR

    logs_dir = Path(LOGS_DIR)
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        logs_dir / "api.log",
        maxBytes=20 * 1024 * 1024,  # 20 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    # Root logger catches fleet_pipeline.* and anything else not listed below
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # Uvicorn writes access + error logs through these named loggers
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addHandler(handler)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Configure file logging, run DB migrations, and start background tasks on startup."""
    _setup_file_logging()
    from fleet_pipeline.db.migrate import run_migrations
    from fleet_pipeline.config import DB_PATH as _DB_PATH

    run_migrations(_DB_PATH)

    async def _auto_end_shift_loop():
        """End the active shift if no messages have arrived in AUTO_END_GAP seconds."""
        import sqlite3 as _sq3
        from datetime import datetime
        from fleet_pipeline.pipeline.shift_detector import ShiftDetector, AUTO_END_GAP
        from fleet_pipeline.utils import now_ist

        while True:
            await asyncio.sleep(300)  # check every 5 minutes
            try:
                conn = _sq3.connect(_DB_PATH)
                conn.row_factory = _sq3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                sd = ShiftDetector(conn)
                if sd._active and sd._last_ts:
                    gap = (now_ist() - sd._last_ts).total_seconds()
                    if gap >= AUTO_END_GAP:
                        shift_id_log = sd._active.get("shift_id", "")
                        _ended_shift = dict(sd._active)
                        from fleet_pipeline.config import (
                            WA_CONTROL_GROUP_JID,
                            WA_GROUP_JID,
                        )

                        summary_jid = WA_CONTROL_GROUP_JID or WA_GROUP_JID
                        from fleet_pipeline.pipeline.wa_notifier import (
                            send_summary_to_group,
                            send_shift_notification,
                        )
                        if summary_jid:
                            try:
                                send_summary_to_group(summary_jid, _DB_PATH)
                            except Exception as _exc:
                                log.warning("Auto-end summary post failed: %s", _exc)
                        _ended_shift_id = sd._active.get("shift_id") if sd._active else shift_id_log
                        _end_ts = now_ist()
                        sd._end(_end_ts)
                        conn.commit()
                        log.info(
                            "Auto-ended shift %s after %.0fs inactivity",
                            shift_id_log,
                            gap,
                        )
                        # Send shift end WA notification
                        if summary_jid:
                            try:
                                send_shift_notification(_ended_shift, "end", summary_jid, _DB_PATH)
                            except Exception as _notif_exc:
                                log.warning("Auto-end shift notification failed: %s", _notif_exc)
                        # Close any open truck cycles at shift end
                        try:
                            from fleet_pipeline.pipeline.committer import (
                                close_open_cycles_at_shift_end,
                            )

                            close_open_cycles_at_shift_end(
                                _DB_PATH,
                                _ended_shift_id or shift_id_log,
                                _end_ts.isoformat(),
                                group_jid=summary_jid,
                            )
                        except Exception as _cyc_exc:
                            log.warning("Auto-end cycle close failed: %s", _cyc_exc)
                        await ws_manager.broadcast(
                            "shift_changed", {"reason": "auto_end_inactivity"}
                        )
                conn.close()
            except Exception as _exc:
                log.warning("Auto-end shift check failed: %s", _exc)

    async def _periodic_summary_loop():
        """Post a shift summary to the control group every 15 minutes (only when a shift is active)."""
        while True:
            await asyncio.sleep(900)  # 15 minutes

            # Read config fresh each iteration — guards against import-time empty values
            import sqlite3 as _sq3
            import os as _os
            from fleet_pipeline.config import DB_PATH as _DB_PATH2

            _ctrl = _os.environ.get("WA_CONTROL_GROUP_JID", "").strip()
            _fleet = _os.environ.get("WA_GROUP_JID", "").strip()
            summary_jid = _ctrl or _fleet
            if not summary_jid:
                log.info("Periodic summary: no WA group JID configured — skipping")
                continue

            # Only send when there is an active (open) shift
            try:
                with _sq3.connect(_DB_PATH2) as _chk:
                    _active = _chk.execute(
                        "SELECT shift_id FROM shifts WHERE ended_at IS NULL AND (is_deleted IS NULL OR is_deleted = 0) LIMIT 1"
                    ).fetchone()
                if not _active:
                    log.info("Periodic summary: no active shift — skipping")
                    continue
            except Exception as _chk_exc:
                log.warning("Periodic summary shift-check failed: %s", _chk_exc)
                continue

            try:
                from fleet_pipeline.pipeline.wa_notifier import send_summary_to_group as _send_summary
                from functools import partial as _partial
                _loop = asyncio.get_event_loop()
                await _loop.run_in_executor(None, _partial(_send_summary, summary_jid, _DB_PATH2))
                log.info("Periodic 15-min summary posted (jid=%s…)", summary_jid[:8])
            except Exception as _exc:
                log.warning("Periodic summary post failed: %s", _exc)

    async def _loading_alert_loop():
        """Send a WA alert once per truck per shift when LS > 1 hour with no LO."""
        import sqlite3 as _sq3
        from fleet_pipeline.config import (
            WA_CONTROL_GROUP_JID,
            WA_GROUP_JID,
            DB_PATH as _DB_PATH3,
        )
        from fleet_pipeline.pipeline.wa_notifier import _post_send_message, _resolve_group_jid

        # Track which (shift_id, truck_id) pairs have been alerted this session
        _alerted: set = set()

        while True:
            await asyncio.sleep(600)  # check every 10 minutes
            alert_jid = _resolve_group_jid(WA_CONTROL_GROUP_JID or WA_GROUP_JID)
            if not alert_jid:
                continue
            try:
                _conn = _sq3.connect(_DB_PATH3)
                _conn.row_factory = _sq3.Row
                # Active shift
                _shift = _conn.execute(
                    "SELECT shift_id FROM shifts WHERE ended_at IS NULL AND (is_deleted IS NULL OR is_deleted = 0) ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                if not _shift:
                    _conn.close()
                    continue
                sid = _shift["shift_id"]
                # Trucks with LS > 1 hour ago and no subsequent LO/LEFT in this shift
                _rows = _conn.execute(
                    """SELECT e.truck_id, COALESCE(e.truck_alias, e.truck_id) as alias,
                              e.timestamp_effective as ls_ts
                       FROM events e
                       WHERE e.shift_id=? AND e.status='LS'
                         AND e.commit_status IN ('COMMITTED','FLAGGED')
                         AND (strftime('%s','now') - strftime('%s',e.timestamp_effective)) > 3600
                         AND NOT EXISTS (
                           SELECT 1 FROM events e2
                           WHERE e2.truck_id=e.truck_id AND e2.shift_id=?
                             AND e2.status IN ('LO','LEFT')
                             AND e2.timestamp_effective >= e.timestamp_effective
                             AND e2.commit_status IN ('COMMITTED','FLAGGED')
                         )
                       GROUP BY e.truck_id
                       ORDER BY e.truck_alias""",
                    (sid, sid),
                ).fetchall()
                _conn.close()
                for _r in _rows:
                    key = (sid, _r["truck_id"])
                    if key in _alerted:
                        continue
                    _alerted.add(key)
                    alias = _r["alias"] or _r["truck_id"]
                    try:
                        _post_send_message(
                            alert_jid,
                            f"⚠️ *{alias}* has been in loading for more than 1 hour — has it been loaded yet?",
                        )
                        log.info("Loading alert sent for truck %s shift %s", alias, sid[:8])
                    except Exception as _ae:
                        log.warning("Loading alert send failed: %s", _ae)
            except Exception as _exc:
                log.warning("Loading alert check failed: %s", _exc)

    task = asyncio.create_task(_auto_end_shift_loop())
    summary_task = asyncio.create_task(_periodic_summary_loop())
    alert_task = asyncio.create_task(_loading_alert_loop())
    yield
    task.cancel()
    summary_task.cancel()
    alert_task.cancel()


app = FastAPI(
    title="Fleet Log Pipeline",
    description="WhatsApp truck-ops log parser with LLM inference and HITL interface",
    version="2.0.0",
    lifespan=lifespan,
)

# ── Auth middleware ───────────────────────────────────────────────────────────
# Paths that never require a token:
#   /              — serves the frontend HTML (auth handled in JS)
#   /static/       — CSS/JS assets
#   /health        — liveness probe
#   /api/auth/     — login + status endpoints
#   /api/ingest/wa-message  — called by Node.js WA listener (internal network only)
#   /docs, /openapi.json, /redoc  — FastAPI Swagger UI (dev)
_AUTH_EXEMPT = (
    "/static/",
    "/api/auth/",
    "/api/ingest/wa-message",
    "/docs",
    "/openapi.json",
    "/redoc",
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in ("/", "/health") or any(path.startswith(e) for e in _AUTH_EXEMPT):
        return await call_next(request)
    if not auth_enabled():
        return await call_next(request)
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else None
    if not verify_token(token):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


# ── WebSocket connection manager ─────────────────────────────────────────────


class ConnectionManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, event_type: str, payload: dict):
        """Broadcast a typed event to all connected clients."""
        msg = json.dumps({"type": event_type, "data": payload})
        dead = set()
        for ws in list(self.active):
            try:
                await asyncio.wait_for(ws.send_text(msg), timeout=2.0)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.active.discard(ws)


ws_manager = ConnectionManager()

# Expose broadcast so pipeline code can push events
app.state.ws_manager = ws_manager


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = None):
    if not verify_token(token):
        await websocket.close(code=4001)
        return
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; client messages are ignored (server-push only)
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                continue  # no message received — keep connection open
    except (WebSocketDisconnect, Exception):
        ws_manager.disconnect(websocket)


# ── REST routes ──────────────────────────────────────────────────────────────

from fleet_pipeline.api import auth as auth_module

app.include_router(auth_module.router)
app.include_router(fleet.router)
app.include_router(hitl.router)
app.include_router(simulation.router)
app.include_router(analytics.router)
app.include_router(step_sim.router)
app.include_router(messages_route.router)
app.include_router(registry_route.router)
app.include_router(shifts_route.router)
app.include_router(ingest_route.router)
app.include_router(status_route.router)

# ── Static files ─────────────────────────────────────────────────────────────

frontend_dir = os.path.join(BASE_DIR, "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
def root():
    index = os.path.join(frontend_dir, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "Fleet Pipeline API", "docs": "/docs"}


# ── NL query ─────────────────────────────────────────────────────────────────


class QueryRequest(BaseModel):
    question: str


@app.post("/query")
@app.post("/api/query/nl")
def nl_query(req: QueryRequest):
    """Natural language query against the fleet DB state."""
    return agent_answer(req.question)


@app.get("/health")
def health():
    return {"status": "ok", "ws_clients": len(ws_manager.active)}


# ── Dev helpers ───────────────────────────────────────────────────────────────


@app.post("/api/dev/broadcast")
async def dev_broadcast(event_type: str = "fleet_state_updated"):
    """Dev-only: manually trigger a WS broadcast for testing."""
    await ws_manager.broadcast(event_type, {"source": "manual"})
    return {"broadcast": event_type, "clients": len(ws_manager.active)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "fleet_pipeline.api.main:app", host=API_HOST, port=API_PORT, reload=True
    )
