"""Shared utilities for the fleet pipeline."""

import pytz as _pytz
from datetime import datetime, timezone

_IST = _pytz.timezone("Asia/Kolkata")


def now_ist() -> datetime:
    """Current time as timezone-aware datetime in IST."""
    return datetime.now(timezone.utc).astimezone(_IST)


def now_ist_iso() -> str:
    """Current time as ISO string in IST timezone."""
    return now_ist().isoformat()


def to_ist(ts: str) -> str:
    """Format any ISO timestamp as IST HH:MM:SS for human-readable logs."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_IST).strftime("%H:%M:%S IST")
    except Exception:
        return ts[:19] if ts else "?"
