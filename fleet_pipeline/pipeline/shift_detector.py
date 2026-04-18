"""
Shift detector — new logic (2026-03-23).

Rules:
1. Shifts are named  YYYY-MM-DD_NN  where NN is a per-day counter (01, 02, …).
   The date is taken from the message that *starts* the shift, so a shift that
   crosses midnight is named after the day it began.

2. Shifts start only via explicit WA signals from the control group:
   - "shift start", "s1/s2/s3", "shift 1/2/3"
   - Mobilisation messages: "tracking volunteers please reach X and Y"
   - Fleet messages do NOT auto-start a shift; shiftless events trigger a WA alert.

3. Shifts end via explicit WA signals (from the control group, non-reply):
   - "shift end/over/done"
   - Standalone "Loading Over" (exact message, no truck mention, not a reply)
   - "ALL trucks LEFT" / "ALL trolleys LEFT" (not a reply)
   - Auto-end after AUTO_END_GAP (3h) inactivity — background task in main.py

4. Inactivity gap no longer auto-starts a new shift (removed). It only auto-ends.

5. Shifts never auto-close on a timer; they close only when:
   - A WA end signal arrives (control group)
   - The background task detects AUTO_END_GAP inactivity
   - Operator presses End / Start

6. No gap shifts are created automatically. If no existing shift covers the message/event
   timestamp, shift_id=None is returned. The committer sends a WA alert to the control
   group asking the operator to insert a shift manually.

Operator API helpers (module-level, no singleton state needed):
   operator_start(db_path)   → start a new shift now
   operator_end(db_path)     → end the current shift now
   operator_resume(db_path)  → reopen the most recently ended shift
"""

import json
import logging
import sqlite3
import warnings
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4
import re
import pytz

from fleet_pipeline.utils import now_ist

_IST = pytz.timezone("Asia/Kolkata")

log = logging.getLogger(__name__)

INACTIVITY_GAP = (
    10800  # seconds — 3 hours (kept for reference; no longer auto-starts shifts)
)
AUTO_END_GAP = (
    10800  # seconds — 3 hours (background task closes shift with no activity)
)


# ── WA signal patterns ────────────────────────────────────────────────────────

_SHIFT_START_RE = [
    re.compile(r"\bshift\s+(start|started|begin|begins|shuru)\b", re.I),
    re.compile(r"\bs([123])\b", re.I),
    re.compile(r"\bshift\s+([123])\b", re.I),
    # Mobilisation messages: "tracking volunteers please reach X and Y"
    re.compile(r"\btracking\s+volunteers?\b", re.I),
    re.compile(r"\bvolunteers?\b.*\breach\b", re.I),
]
_SHIFT_END_RE = [
    re.compile(r"\bshift\s+(end|over|ended|finish|done|khatam)\b", re.I),
    # Standalone "Loading Over" (no truck mention, not a reply) → end shift
    re.compile(r"^\s*loading\s+over\s*$", re.I),
    # "ALL trucks LEFT" / "ALL trolleys LEFT" → all vehicles have departed → end shift
    re.compile(r"\ball\s+(trucks?|trolleys?)\s+left\b", re.I),
]

# Fleet status auto-start: any message containing a cycle verb word triggers auto-start.
_FLEET_STATUS_RE = re.compile(r"\b(enter|ls|lo|left|us|uo)\b", re.I)


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

    def __init__(
        self, conn: sqlite3.Connection, simulation_run_id: Optional[str] = None
    ):
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

        Rule: look up existing shift covering the message timestamp. No gap-fill.
        """
        ts = _parse_iso(timestamp_iso)
        if ts is None:
            return self._active_id()

        signal = detect_shift_signal(raw_text)
        if signal == "start":
            self._start_new(ts, method="wa_signal", raw_text=raw_text)
            self._last_ts = ts
            return self._active_id()
        if signal == "end":
            self._end(ts)
            self._last_ts = ts
            return None

        shift_id = self._find_shift_for_timestamp(ts)
        if shift_id:
            self._active = self._load_shift(shift_id)
        self._last_ts = ts
        return shift_id

    def resolve_shift_for_event(self, event_timestamp_iso: str) -> Optional[str]:
        """
        Find the existing shift covering a specific event timestamp (timestamp_effective).
        NEVER creates a gap shift. Returns shift_id or None.
        Used for per-event assignment in the committer.
        Returns None for blank/malformed timestamps (not the active shift).
        """
        if not event_timestamp_iso:
            return None
        ts = _parse_iso(event_timestamp_iso)
        if ts is None:
            return None
        return self._find_shift_for_timestamp(ts)

    def _find_shift_for_timestamp(self, ts: datetime) -> Optional[str]:
        """Find the shift that covers timestamp ts. Returns shift_id or None."""
        try:
            ts_iso = ts.isoformat()
            row = self.conn.execute(
                """SELECT shift_id FROM shifts
                   WHERE started_at <= ?
                     AND (ended_at IS NULL OR ended_at > ?)
                     AND (is_deleted IS NULL OR is_deleted = 0)
                   ORDER BY started_at DESC
                   LIMIT 1""",
                (ts_iso, ts_iso),
            ).fetchone()
            return row["shift_id"] if row else None
        except Exception:
            return None

    def _load_shift(self, shift_id: str) -> Optional[dict]:
        """Load a shift by its ID."""
        try:
            row = self.conn.execute(
                "SELECT * FROM shifts WHERE shift_id=?",
                (shift_id,),
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _load_active(self):
        try:
            # A shift is only truly active if:
            # 1. ended_at IS NULL
            # 2. No newer shift (started_at > this one) exists — if a newer shift was
            #    created after this one it means this one was superseded and should be
            #    treated as closed even if its ended_at was never set.
            row = self.conn.execute(
                """SELECT * FROM shifts sh
                   WHERE sh.ended_at IS NULL
                     AND (sh.is_deleted IS NULL OR sh.is_deleted = 0)
                     AND NOT EXISTS (
                       SELECT 1 FROM shifts s2
                       WHERE s2.started_at > sh.started_at
                         AND (s2.is_deleted IS NULL OR s2.is_deleted = 0)
                     )
                   ORDER BY sh.started_at DESC LIMIT 1"""
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

        shift_id = str(uuid4())
        shift_name = self._make_name(ts)
        day_num = self._day_count(
            ts
        )  # already incremented by _make_name logic, so recount

        # Recount after potential _end() above
        day_num = self._day_count(ts) + 1

        # Extract default site(s) from shift start message (e.g. "shift start KN4" or "volunteers reach KN4 and SOC")
        default_site_ids = (
            _extract_sites_from_text(self.conn, raw_text) if raw_text else []
        )
        default_site_id = default_site_ids[0] if default_site_ids else None
        default_site_ids_json = (
            json.dumps(default_site_ids) if default_site_ids else None
        )

        try:
            self.conn.execute(
                """INSERT INTO shifts
                   (shift_id, shift_number, shift_name, started_at, detection_method,
                    simulation_run_id, default_site_id, default_site_ids)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    shift_id,
                    day_num,
                    shift_name,
                    ts.isoformat(),
                    method,
                    self.simulation_run_id,
                    default_site_id,
                    default_site_ids_json,
                ),
            )
            log.info(
                "[SHIFT] Started: %s (method=%s default_sites=%s)",
                shift_name,
                method,
                default_site_ids or "none",
            )
            self._active = {
                "shift_id": shift_id,
                "shift_number": day_num,
                "shift_name": shift_name,
                "started_at": ts.isoformat(),
                "ended_at": None,
                "default_site_id": default_site_id,
                "default_site_ids": default_site_ids_json,
            }
        except Exception as e:
            warnings.warn(f"[ShiftDetector] Failed to create shift: {e}")

    def _end(self, ts: datetime):
        if not self._active:
            return
        shift_name = self._active.get("shift_name", self._active.get("shift_id", "?"))
        try:
            self.conn.execute(
                "UPDATE shifts SET ended_at=? WHERE shift_id=?",
                (ts.isoformat(), self._active["shift_id"]),
            )
            log.info("[SHIFT] Ended: %s", shift_name)
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


def operator_start(db_path: str, wa_message_id: Optional[str] = None) -> dict:
    """Force-start a new shift right now. Returns the new shift dict."""
    conn = _open_conn(db_path)
    ts = now_ist()
    try:
        sd = ShiftDetector(conn)
        sd._start_new(ts, method="operator")
        if sd._active:
            msg_id = wa_message_id or f"operator_start_{ts.isoformat()}"
            conn.execute(
                """INSERT INTO shift_events
                   (shift_event_id, shift_id, status, timestamp_iso,
                    commit_status, wa_message_id, site_id, site_ids_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"shift_start_{sd._active['shift_id']}",
                    sd._active["shift_id"],
                    "SHIFT_START",
                    ts.isoformat(),
                    "COMMITTED",
                    msg_id,
                    sd._active.get("default_site_id"),
                    sd._active.get("default_site_ids"),
                ),
            )
        conn.commit()
        return sd._active or {}
    finally:
        conn.close()


def operator_end(db_path: str, wa_message_id: Optional[str] = None) -> bool:
    """End the active shift. Returns True if a shift was ended."""
    conn = _open_conn(db_path)
    ts = now_ist()
    try:
        sd = ShiftDetector(conn)
        if not sd._active:
            return False
        msg_id = wa_message_id or f"operator_end_{ts.isoformat()}"
        conn.execute(
            """INSERT INTO shift_events
               (shift_event_id, shift_id, status, timestamp_iso,
                commit_status, wa_message_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                f"shift_end_{sd._active['shift_id']}",
                sd._active["shift_id"],
                "SHIFT_END",
                ts.isoformat(),
                "COMMITTED",
                msg_id,
            ),
        )
        sd._end(ts)
        conn.commit()
        return True
    finally:
        conn.close()


def operator_end(db_path: str, wa_message_id: Optional[str] = None) -> bool:
    """End the active shift. Returns True if a shift was ended."""
    conn = _open_conn(db_path)
    ts = now_ist()
    try:
        sd = ShiftDetector(conn)
        if not sd._active:
            return False
        msg_id = wa_message_id or f"operator_end_{ts.isoformat()}"
        conn.execute(
            """INSERT INTO events
               (event_id, msg_id, status, timestamp_effective,
                commit_status, wa_message_id, shift_id, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"shift_end_{sd._active['shift_id']}",
                msg_id,
                "SHIFT_END",
                ts.isoformat(),
                "COMMITTED",
                msg_id,
                sd._active["shift_id"],
                1.0,
            ),
        )
        sd._end(ts)
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
                (now_ist().isoformat(), active["shift_id"]),
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
    Returns the canonical site_id of the first match, or None if no match.
    Used to extract the default site from shift-start messages like "shift start KN4".
    """
    sites = _extract_sites_from_text(conn, text)
    return sites[0] if sites else None


def _extract_sites_from_text(conn: sqlite3.Connection, text: str) -> List[str]:
    """
    Scan text for ALL words that match known site aliases or site_ids.
    Returns list of canonical site_ids (deduplicated, in order of appearance).
    Used to extract default sites from shift-start messages like "tracking volunteers reach KN4 and SOC".
    """
    if not text:
        return []
    try:
        rows = conn.execute(
            "SELECT site_id, aliases FROM sites WHERE is_active=1"
        ).fetchall()
    except Exception:
        return []

    found: list = []
    seen: set = set()
    words = re.findall(r"[A-Za-z0-9]{2,8}", text)
    for word in words:
        wl = word.lower()
        for row in rows:
            site_id = row[0]
            if site_id in seen:
                continue
            if wl == site_id.lower():
                found.append(site_id)
                seen.add(site_id)
                break
            try:
                aliases = json.loads(row[1] or "[]")
            except Exception:
                aliases = []
            if wl in [a.lower() for a in aliases]:
                found.append(site_id)
                seen.add(site_id)
                break
    return found


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO timestamp string to a timezone-aware IST datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.tzinfo != _IST:
            dt = dt.astimezone(_IST)
        return dt
    except Exception:
        return None
