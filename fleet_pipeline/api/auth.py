"""
Authentication for Fleet Tracker dashboard.

Set FLEET_AUTH_PASSWORD in .env to enable.
Leave blank to disable auth (local dev).

Token = HMAC-SHA256(password, "fleet-auth-token") — stateless, no DB needed.
To revoke access change the password; sessionStorage clears on tab close.
"""
import hashlib
import hmac
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

AUTH_PASSWORD: str = os.environ.get("FLEET_AUTH_PASSWORD", "")

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _make_token(password: str) -> str:
    return hmac.new(password.encode(), b"fleet-auth-token", hashlib.sha256).hexdigest()


def auth_enabled() -> bool:
    return bool(AUTH_PASSWORD)


def verify_token(token: Optional[str]) -> bool:
    """Return True if token is valid (or auth is disabled)."""
    if not AUTH_PASSWORD:
        return True
    if not token:
        return False
    return hmac.compare_digest(token, _make_token(AUTH_PASSWORD))


# ── Public endpoints (no auth gate on these) ─────────────────────────────────

@router.get("/status")
def auth_status():
    """Frontend calls this on load to decide whether to show the login screen."""
    return {"auth_required": auth_enabled()}


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
def login(req: LoginRequest):
    if not AUTH_PASSWORD:
        return {"token": "no-auth", "auth_required": False}
    if not hmac.compare_digest(req.password.encode(), AUTH_PASSWORD.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong password")
    return {"token": _make_token(AUTH_PASSWORD), "auth_required": True}
