"""
System status — probes LLM and WA listener health endpoints, checks DB.

GET /api/status  — current status of all services + incident log
"""
import os
import sqlite3
import time
import urllib.request
import urllib.error

from fastapi import APIRouter

from fleet_pipeline.config import DB_PATH, LLM_BASE_URL, MODEL_NAME

router = APIRouter(prefix="/api/status", tags=["status"])

WA_HEALTH_URL = os.environ.get("FLEET_WA_HEALTH_URL", "http://wa:3001/health")

# ── In-memory state ───────────────────────────────────────────────────────────

_llm = {"up": None, "last_check": 0.0}
_wa  = {"up": None, "last_check": 0.0}
_incidents: list = []

CHECK_TTL = 30   # seconds between probes


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _record(service: str, event: str):
    _incidents.append({"service": service, "event": event, "at": _now_iso()})
    if len(_incidents) > 100:
        _incidents.pop(0)


def _probe(url: str, timeout: int = 4) -> bool:
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return resp.status < 500
    except Exception:
        return False


def _refresh(state: dict, url: str, service: str):
    now = time.time()
    if now - state["last_check"] < CHECK_TTL:
        return
    was_up = state["up"]
    is_up  = _probe(url)
    state.update({"up": is_up, "last_check": now})
    if was_up is True and not is_up:
        _record(service, "down")
    elif was_up is False and is_up:
        _record(service, "recovered")
    elif was_up is None and is_up:
        _record(service, "connected")


def _db_ok() -> bool:
    try:
        with sqlite3.connect(DB_PATH, timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


# ── Route ─────────────────────────────────────────────────────────────────────

@router.get("")
def get_status():
    if LLM_BASE_URL:
        _refresh(_llm, LLM_BASE_URL.rstrip("/") + "/models", "llm")
    else:
        _llm["up"] = None   # in-process — can't probe

    _refresh(_wa, WA_HEALTH_URL, "wa")

    return {
        "services": {
            "api": {"up": True},
            "llm": {"up": _llm["up"], "endpoint": LLM_BASE_URL or "in-process", "model": MODEL_NAME},
            "wa":  {"up": _wa["up"],  "health_url": WA_HEALTH_URL},
            "db":  {"up": _db_ok()},
        },
        "incidents": list(reversed(_incidents[-20:])),
    }
