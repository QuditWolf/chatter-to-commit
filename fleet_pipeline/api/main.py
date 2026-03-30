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
import sys
import os
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

@asynccontextmanager
async def lifespan(application: FastAPI):
    """Run migrations once at startup, then warm up the LLM KV cache.

    GLM-4.7-Flash has a cold-cache issue where the first ~3 calls return
    empty content (model stops after </think> without generating JSON).
    Sending a warmup call at startup primes the KV cache so real traffic
    works immediately. The warmup result is discarded.
    """
    from fleet_pipeline.db.migrate import run_migrations
    from fleet_pipeline.config import DB_PATH as _DB_PATH
    run_migrations(_DB_PATH)

    if not LLM_MOCK and LLM_BASE_URL:
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _llm_warmup)
    yield


def _llm_warmup():
    """Blocking warmup — runs in thread so it doesn't delay startup."""
    import logging
    log = logging.getLogger(__name__)
    try:
        from fleet_pipeline.api.pipeline_service import process_raw_text
        process_raw_text(
            raw_text="warmup",
            sender_name="system",
            sender_id="warmup",
            source="warmup",
        )
        log.info("[Startup] LLM warmup complete")
    except Exception as exc:
        # Warmup failure is expected (model may return nothing useful) — ignore
        log.info("[Startup] LLM warmup done (result discarded): %s", exc)


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
    "/static/", "/api/auth/", "/api/ingest/wa-message",
    "/docs", "/openapi.json", "/redoc",
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
    uvicorn.run("fleet_pipeline.api.main:app", host=API_HOST, port=API_PORT, reload=True)
