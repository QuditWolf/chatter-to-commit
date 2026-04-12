"""
Shift detector — new logic (2026-03-23).

Rules:
1. Shifts are named  YYYY-MM-DD_NN  where NN is a per-day counter (01, 02, …).
   The date is taken from the message that *starts* the shift, so a shift that
   crosses midnight is named after the day it began.

2. Auto-detection — inactivity gap:
   If the gap between the current message's timestamp and the previous message's
   timestamp is ≥ INACTIVITY_GAP (1 hour), the current message starts a new shift.

3. WA signal override:
   "shift start", "s1/s2/s3", "shift 1/2/3" → force-start a new shift.
   "shift end/over/done" → force-end the current shift.

4. No active shift → first message always starts one.

5. Shifts never auto-close on a timer; they close only when:
   - A WA end signal arrives
   - The next shift starts (inactivity gap or WA start signal)
   - Operator presses End / Start

Operator API helpers (module-level, no singleton state needed):
   operator_start(db_path)   → start a new shift now
   operator_end(db_path)     → end the current shift now
   operator_resume(db_path)  → reopen the most recently ended shift
"""
import json
import sqlite3
import warnings
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
import re

INACTIVITY_GAP = 10800  # seconds — 3 hours (auto-starts a new shift on next message)
AUTO_END_GAP   = 10800  # seconds — 3 hours (background task closes shift with no activity)


# ── WA signal patterns ────────────────────────────────────────────────────────

_SHIFT_START_RE = [
    re.compile(r"\bshift\s+(start|started|begin|begins|shuru)\b", re.I),
    re.compile(r"\bs([123])\b", re.I),
    re.compile(r"\bshift\s+([123])\b", re.I),
]
_SHIFT_END_RE = [
    re.compile(r"\bshift\s+(end|over|ended|finish|done|khatam)\b", re.I),
    # Standalone "Loading Over" message signals end of loading session → end shift
    re.compile(r"^\s*loading\s+over\s*$", re.I),
]


def detect_shift_signal(text: str) -> Optional[str]:
    """Return 'start' | 'end' | None."""
    t = (text or "").strip()
    for p in _SHIFT_END_RE:
        if p.search(t):
            return "end"
    for p in _SHIFT_START_RE:
        if p.search(t):
            return "start"
    return None


# ── ShiftDetector ─────────────────────────────────────────────────────────────

class ShiftDetector:
    """
    Stateful shift detector backed by SQLite.
    Create a new instance per pipeline call — all state lives in the DB.
    """

    def __init__(self, conn: sqlite3.Connection, simulation_run_id: Optional[str] = None):
        self.conn = conn
        self.simulation_run_id = simulation_run_id
        self._active = self._load_active()
        self._last_ts = self._load_last_ts()

    # ── Public ────────────────────────────────────────────────────────────────

    def process_message(self, raw_text: str, timestamp_iso: str) -> Optional[str]:
        """
        Determine the shift_id for this message.
        May create or close shift records as a side-effect.
        Returns shift_id str or None.
        """
        ts = _parse_iso(timestamp_iso)
        if ts is None:
            return self._active_id()

        # 1. WA signal override
        signal = detect_shift_signal(raw_text)
        if signal == "start":
            self._start_new(ts, method="wa_signal", raw_text=raw_text)
            self._last_ts = ts
            return self._active_id()
        if signal == "end":
            self._end(ts)
            self._last_ts = ts
            return None

        # 2. Inactivity gap → new shift
        if self._last_ts and (ts - self._last_ts).total_seconds() >= INACTIVITY_GAP:
            self._start_new(ts, method="inactivity_gap")

        # 3. No shift at all → start one
        elif not self._active:
            self._start_new(ts, method="auto_start", raw_text=raw_text)

        self._last_ts = ts
        return self._active_id()

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _load_active(self):
        try:
            row = self.conn.execute(
                "SELECT * FROM shifts WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def _load_last_ts(self) -> Optional[datetime]:
        """Load the timestamp of the most recent processed message."""
        for q in [
            "SELECT MAX(received_at) FROM wa_messages",
            "SELECT MAX(timestamp_effective) FROM events WHERE commit_status != 'DELETED'",
        ]:
            try:
                row = self.conn.execute(q).fetchone()
                if row and row[0]:
                    return _parse_iso(row[0])
            except Exception:
                continue
        return None

    def _active_id(self) -> Optional[str]:
        return self._active["shift_id"] if self._active else None

    def _day_count(self, ts: datetime) -> int:
        """Number of shifts already created on ts's date."""
        date_str = ts.strftime("%Y-%m-%d")
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM shifts WHERE shift_name LIKE ?",
                (f"{date_str}_%",),
            ).fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def _make_name(self, ts: datetime) -> str:
        n = self._day_count(ts) + 1
        return f"{ts.strftime('%Y-%m-%d')}_{n:02d}"

    def _start_new(self, ts: datetime, method: str, raw_text: str = ""):
        # Close current if open
        if self._active:
            self._end(ts)

        shift_id   = str(uuid4())
        shift_name = self._make_name(ts)
        day_num    = self._day_count(ts)  # already incremented by _make_name logic, so recount

        # Recount after potential _end() above
        day_num = self._day_count(ts) + 1

        # Extract default site from shift start message (e.g. "shift start KN4")
        default_site_id = _extract_site_from_text(self.conn, raw_text) if raw_text else None

        try:
            self.conn.execute(
                """INSERT INTO shifts
                   (shift_id, shift_number, shift_name, started_at, detection_method,
                    simulation_run_id, default_site_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (shift_id, day_num, shift_name, ts.isoformat(), method,
                 self.simulation_run_id, default_site_id),
            )
            self._active = {
                "shift_id": shift_id,
                "shift_number": day_num,
                "shift_name": shift_name,
                "started_at": ts.isoformat(),
                "ended_at": None,
                "default_site_id": default_site_id,
            }
        except Exception as e:
            warnings.warn(f"[ShiftDetector] Failed to create shift: {e}")

    def _end(self, ts: datetime):
        if not self._active:
            return
        try:
            self.conn.execute(
                "UPDATE shifts SET ended_at=? WHERE shift_id=?",
                (ts.isoformat(), self._active["shift_id"]),
            )
        except Exception as e:
            warnings.warn(f"[ShiftDetector] Failed to end shift: {e}")
        self._active = None


# ── Operator API helpers ──────────────────────────────────────────────────────

def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def operator_start(db_path: str) -> dict:
    """Force-start a new shift right now. Returns the new shift dict."""
    conn = _open_conn(db_path)
    try:
        sd = ShiftDetector(conn)
        sd._start_new(datetime.now(timezone.utc), method="operator")
        conn.commit()
        return sd._active or {}
    finally:
        conn.close()


def operator_end(db_path: str) -> bool:
    """End the active shift. Returns True if a shift was ended."""
    conn = _open_conn(db_path)
    try:
        sd = ShiftDetector(conn)
        if not sd._active:
            return False
        sd._end(datetime.now(timezone.utc))
        conn.commit()
        return True
    finally:
        conn.close()


def operator_resume(db_path: str) -> Optional[dict]:
    """Reopen the most recently ended shift (closes current if any)."""
    conn = _open_conn(db_path)
    try:
        # Find last ended shift
        row = conn.execute(
            "SELECT * FROM shifts WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        # Close any currently active shift
        active = conn.execute(
            "SELECT shift_id FROM shifts WHERE ended_at IS NULL LIMIT 1"
        ).fetchone()
        if active:
            conn.execute(
                "UPDATE shifts SET ended_at=? WHERE shift_id=?",
                (datetime.now(timezone.utc).isoformat(), active["shift_id"]),
            )
        # Reopen the last ended shift
        conn.execute(
            "UPDATE shifts SET ended_at=NULL WHERE shift_id=?",
            (row["shift_id"],),
        )
        conn.commit()
        return dict(row)
    finally:
        conn.close()


# ── Utilities ─────────────────────────────────────────────────────────────────


def _extract_site_from_text(conn: sqlite3.Connection, text: str) -> Optional[str]:
    """
    Scan text for a word that matches a known site alias or site_id.
    Returns the canonical site_id, or None if no match.
    Used to extract the default site from shift-start messages like "shift start KN4".
    """
    if not text:
        return None
    try:
        rows = conn.execute("SELECT site_id, aliases FROM sites WHERE is_active=1").fetchall()
    except Exception:
        return None

    words = re.findall(r"[A-Za-z0-9]{2,8}", text)
    for word in words:
        wl = word.lower()
        for row in rows:
            if wl == row[0].lower():
                return row[0]
            try:
                aliases = json.loads(row[1] or "[]")
            except Exception:
                aliases = []
            if wl in [a.lower() for a in aliases]:
                return row[0]
    return None


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None
