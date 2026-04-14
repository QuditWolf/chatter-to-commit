"""
SQLite connection and query helpers for the fleet pipeline.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from fleet_pipeline.utils import now_ist_iso

from fleet_pipeline.config import DB_PATH


def _get_schema_path() -> str:
    return os.path.join(os.path.dirname(__file__), "schema.sql")


def init_db(db_path: str = DB_PATH) -> None:
    """Create all tables if they don't exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    schema = open(_get_schema_path()).read()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema)


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_conn(db_path: str = DB_PATH):
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Trucks
# ---------------------------------------------------------------------------


def insert_truck(conn: sqlite3.Connection, truck: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO trucks (truck_id, display_name, aliases, is_active)
           VALUES (?, ?, ?, ?)""",
        (
            truck["truck_id"],
            truck["display_name"],
            json.dumps(truck.get("aliases", []), ensure_ascii=False),
            truck.get("is_active", True),
        ),
    )


def get_all_trucks(conn: sqlite3.Connection) -> List[Dict]:
    rows = conn.execute("SELECT * FROM trucks WHERE is_active=1").fetchall()
    return [dict(r) for r in rows]


def add_truck_alias(conn: sqlite3.Connection, truck_id: str, alias: str) -> None:
    row = conn.execute(
        "SELECT aliases FROM trucks WHERE truck_id=?", (truck_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"truck_id '{truck_id}' not found")
    aliases = json.loads(row["aliases"])
    if alias not in aliases:
        aliases.append(alias)
        conn.execute(
            "UPDATE trucks SET aliases=? WHERE truck_id=?",
            (json.dumps(aliases, ensure_ascii=False), truck_id),
        )


def create_truck(
    conn: sqlite3.Connection, truck_id: str, display_name: str, aliases: List[str]
) -> None:
    conn.execute(
        "INSERT INTO trucks (truck_id, display_name, aliases) VALUES (?, ?, ?)",
        (truck_id, display_name, json.dumps(aliases, ensure_ascii=False)),
    )


def auto_create_truck(conn: sqlite3.Connection, truck_alias: str) -> Optional[str]:
    """
    Auto-create a truck from an alias when the LLM identifies a truck not in registry.
    Returns the new truck_id, or None if alias is empty/already exists.
    """
    import re as _re

    if not truck_alias or not truck_alias.strip():
        return None

    # Generate a stable truck_id from the alias
    truck_id = (
        _re.sub(r"[^A-Za-z0-9_]", "_", truck_alias.strip()).upper()[:20].strip("_")
    )
    if not truck_id:
        return None

    # If already exists (may have been created in a previous run), return it
    existing = conn.execute(
        "SELECT truck_id FROM trucks WHERE truck_id=?", (truck_id,)
    ).fetchone()
    if existing:
        # Also check if alias already points to an existing truck
        return truck_id

    # Check alias collision
    for row in conn.execute("SELECT truck_id, aliases FROM trucks").fetchall():
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except Exception:
            aliases = []
        if (
            truck_alias.lower() in [a.lower() for a in aliases]
            or truck_alias.lower() == row["truck_id"].lower()
        ):
            # Already exists under a different ID — don't create duplicate
            return None

    create_truck(conn, truck_id, truck_alias.strip(), [truck_alias.strip()])
    return truck_id


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------


def insert_site(conn: sqlite3.Connection, site: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO sites (site_id, display_name, aliases, site_type, is_active)
           VALUES (?, ?, ?, ?, ?)""",
        (
            site["site_id"],
            site["display_name"],
            json.dumps(site.get("aliases", []), ensure_ascii=False),
            site.get("site_type"),
            site.get("is_active", True),
        ),
    )


def get_all_sites(conn: sqlite3.Connection) -> List[Dict]:
    rows = conn.execute("SELECT * FROM sites WHERE is_active=1").fetchall()
    return [dict(r) for r in rows]


def add_site_alias(conn: sqlite3.Connection, site_id: str, alias: str) -> None:
    row = conn.execute(
        "SELECT aliases FROM sites WHERE site_id=?", (site_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"site_id '{site_id}' not found")
    aliases = json.loads(row["aliases"])
    if alias not in aliases:
        aliases.append(alias)
        conn.execute(
            "UPDATE sites SET aliases=? WHERE site_id=?",
            (json.dumps(aliases, ensure_ascii=False), site_id),
        )


# ---------------------------------------------------------------------------
# Raw messages
# ---------------------------------------------------------------------------


def insert_raw_message(conn: sqlite3.Connection, msg: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO raw_messages
           (msg_id, source_file, timestamp_iso, sender_name, sender_id,
            raw_text, is_edited, is_deleted, media_type, quoted_wa_message_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            msg["msg_id"],
            msg.get("source_file"),
            msg["timestamp_iso"],
            msg.get("sender_name"),
            msg.get("sender_id"),
            msg.get("raw_text"),
            bool(msg.get("is_edited", False)),
            bool(msg.get("is_deleted", False)),
            msg.get("media_type"),
            msg.get("quoted_wa_message_id"),
        ),
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def insert_event(conn: sqlite3.Connection, ev: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO events
           (event_id, msg_id, truck_id, truck_alias, status, site_id, site_alias,
            material, timestamp_effective, inferred, confidence, reasoning,
            commit_status, corrects_event_id, processing_id, simulation_run_id,
            wa_message_id, commit_path, shift_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ev["event_id"],
            ev.get("msg_id"),
            ev.get("truck_id"),
            ev.get("truck_alias"),
            ev["status"],
            ev.get("site_id"),
            ev.get("site_alias"),
            ev.get("material"),
            ev["timestamp_effective"],
            bool(ev.get("inferred", False)),
            ev.get("confidence"),
            ev.get("reasoning"),
            ev.get("commit_status", "PENDING"),
            ev.get("corrects_event_id"),
            ev.get("processing_id"),
            ev.get("simulation_run_id"),
            ev.get("wa_message_id"),
            ev.get("commit_path"),
            ev.get("shift_id"),
        ),
    )


def update_event_status(
    conn: sqlite3.Connection, event_id: str, commit_status: str
) -> None:
    conn.execute(
        "UPDATE events SET commit_status=? WHERE event_id=?",
        (commit_status, event_id),
    )


def get_recent_events_for_truck(
    conn: sqlite3.Connection,
    truck_id: str,
    limit: int = 5,
    shift_id: Optional[str] = None,
) -> List[Dict]:
    if shift_id:
        rows = conn.execute(
            """SELECT * FROM events WHERE truck_id=? AND shift_id=?
               AND commit_status IN ('COMMITTED','FLAGGED')
               ORDER BY timestamp_effective DESC LIMIT ?""",
            (truck_id, shift_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM events WHERE truck_id=?
               AND commit_status IN ('COMMITTED','FLAGGED')
               ORDER BY timestamp_effective DESC LIMIT ?""",
            (truck_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_committed_events(
    conn: sqlite3.Connection, limit: int = 50, shift_id: Optional[str] = None
) -> List[Dict]:
    if shift_id:
        rows = conn.execute(
            """SELECT e.*, t.display_name as truck_name, s.display_name as site_name
               FROM events e
               LEFT JOIN trucks t ON e.truck_id = t.truck_id
               LEFT JOIN sites s ON e.site_id = s.site_id
               WHERE e.commit_status IN ('COMMITTED','FLAGGED')
                 AND e.shift_id=?
               ORDER BY e.timestamp_effective DESC LIMIT ?""",
            (shift_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT e.*, t.display_name as truck_name, s.display_name as site_name
               FROM events e
               LEFT JOIN trucks t ON e.truck_id = t.truck_id
               LEFT JOIN sites s ON e.site_id = s.site_id
               WHERE e.commit_status IN ('COMMITTED','FLAGGED')
               ORDER BY e.timestamp_effective DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_fleet_state(
    conn: sqlite3.Connection, shift_id: Optional[str] = None
) -> List[Dict]:
    """Latest committed/flagged event per truck, optionally scoped to a shift."""
    if shift_id:
        rows = conn.execute(
            """SELECT e.*, t.display_name as truck_name, s.display_name as site_name
               FROM events e
               LEFT JOIN trucks t ON e.truck_id = t.truck_id
               LEFT JOIN sites s ON e.site_id = s.site_id
               WHERE e.commit_status IN ('COMMITTED', 'FLAGGED')
                 AND e.truck_id IS NOT NULL
                 AND e.shift_id = ?
                 AND e.rowid IN (
                   SELECT MAX(rowid) FROM events
                   WHERE commit_status IN ('COMMITTED', 'FLAGGED')
                     AND truck_id IS NOT NULL
                     AND shift_id = ?
                   GROUP BY truck_id
                 )
               ORDER BY e.truck_id""",
            (shift_id, shift_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT e.*, t.display_name as truck_name, s.display_name as site_name
               FROM events e
               LEFT JOIN trucks t ON e.truck_id = t.truck_id
               LEFT JOIN sites s ON e.site_id = s.site_id
               WHERE e.commit_status IN ('COMMITTED', 'FLAGGED')
                 AND e.truck_id IS NOT NULL
                 AND e.rowid IN (
                   SELECT MAX(rowid) FROM events
                   WHERE commit_status IN ('COMMITTED', 'FLAGGED') AND truck_id IS NOT NULL
                   GROUP BY truck_id
                 )
               ORDER BY e.truck_id"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_l3_context(conn: sqlite3.Connection, limit: int = 20) -> List[Dict]:
    """
    Return recent events for L3 context — latest event per truck plus recent history.
    Includes COMMITTED and FLAGGED events (not HELD/DELETED).
    Used to populate l3_history for state inference.
    """
    rows = conn.execute(
        """SELECT truck_id, truck_alias, site_id, site_alias, status,
                  timestamp_effective, inferred, commit_status
           FROM events
           WHERE commit_status IN ('COMMITTED', 'FLAGGED') AND truck_id IS NOT NULL
           ORDER BY timestamp_effective DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tallies
# ---------------------------------------------------------------------------


def insert_tally(conn: sqlite3.Connection, tally: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO tallies
           (tally_id, msg_id, timestamp_iso, tally_data, commit_status, simulation_run_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            tally["tally_id"],
            tally.get("msg_id"),
            tally.get("timestamp_iso"),
            json.dumps(tally.get("tally_data", {}), ensure_ascii=False),
            tally.get("commit_status", "COMMITTED"),
            tally.get("simulation_run_id"),
        ),
    )


# ---------------------------------------------------------------------------
# HITL queue
# ---------------------------------------------------------------------------


def insert_hitl_question(conn: sqlite3.Connection, q: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO hitl_queue
           (question_id, msg_id, event_id, question_type, question_text, context,
            status, simulation_run_id, original_wa_message_id, group_jid)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            q["question_id"],
            q.get("msg_id"),
            q.get("event_id"),
            q["question_type"],
            q["question_text"],
            json.dumps(q.get("context", {}), ensure_ascii=False),
            q.get("status", "OPEN"),
            q.get("simulation_run_id"),
            q.get("original_wa_message_id"),
            q.get("group_jid"),
        ),
    )


def set_hitl_bot_wa_message_id(
    conn: sqlite3.Connection, question_id: str, bot_wa_message_id: str
) -> None:
    conn.execute(
        "UPDATE hitl_queue SET bot_wa_message_id=? WHERE question_id=?",
        (bot_wa_message_id, question_id),
    )


def get_open_question_by_bot_wa_id(
    conn: sqlite3.Connection, bot_wa_message_id: str
) -> Optional[Dict[str, Any]]:
    """Return the open HITL question whose bot reply has the given WA message ID."""
    row = conn.execute(
        "SELECT * FROM hitl_queue WHERE bot_wa_message_id=? AND status='OPEN'",
        (bot_wa_message_id,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("context"):
        try:
            d["context"] = json.loads(d["context"])
        except Exception:
            pass
    return d


def get_open_questions(
    conn: sqlite3.Connection, limit: int = 50, offset: int = 0
) -> List[Dict]:
    rows = conn.execute(
        "SELECT * FROM hitl_queue WHERE status='OPEN' AND question_type != 'DELETED_MESSAGE' ORDER BY created_at ASC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("context"):
            try:
                d["context"] = json.loads(d["context"])
            except Exception:
                pass
        out.append(d)
    return out


def answer_question(
    conn: sqlite3.Connection,
    question_id: str,
    answer: str,
    answered_by: str = "human",
) -> None:
    conn.execute(
        """UPDATE hitl_queue
           SET status='ANSWERED', answer=?, answered_by=?, answered_at=?
           WHERE question_id=?""",
        (answer, answered_by, now_ist_iso(), question_id),
    )


def dismiss_question(conn: sqlite3.Connection, question_id: str) -> None:
    conn.execute(
        "UPDATE hitl_queue SET status='DISMISSED' WHERE question_id=?",
        (question_id,),
    )


# ---------------------------------------------------------------------------
# Simulation runs
# ---------------------------------------------------------------------------


def insert_simulation_run(conn: sqlite3.Connection, run: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO simulation_runs
           (run_id, source_file, notes)
           VALUES (?, ?, ?)""",
        (run["run_id"], run.get("source_file"), run.get("notes")),
    )


def update_simulation_run(
    conn: sqlite3.Connection, run_id: str, stats: Dict[str, Any]
) -> None:
    conn.execute(
        """UPDATE simulation_runs SET
           finished_at=?, total_msgs=?, committed=?, flagged=?, held=?,
           errors=?, hitl_created=?
           WHERE run_id=?""",
        (
            now_ist_iso(),
            stats.get("total_msgs", 0),
            stats.get("committed", 0),
            stats.get("flagged", 0),
            stats.get("held", 0),
            stats.get("errors", 0),
            stats.get("hitl_created", 0),
            run_id,
        ),
    )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# WA messages
# ---------------------------------------------------------------------------


def insert_wa_message(conn: sqlite3.Connection, msg: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO wa_messages
           (wa_message_id, sender_phone, group_jid, raw_text, received_at, message_type, processed)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            msg["wa_message_id"],
            msg.get("sender_phone"),
            msg.get("group_jid"),
            msg.get("raw_text"),
            msg.get("received_at"),
            msg.get("message_type", "fleet_event"),
            bool(msg.get("processed", False)),
        ),
    )


def mark_wa_message_processed(conn: sqlite3.Connection, wa_message_id: str) -> None:
    conn.execute(
        "UPDATE wa_messages SET processed=TRUE WHERE wa_message_id=?",
        (wa_message_id,),
    )


# ---------------------------------------------------------------------------
# Shifts
# ---------------------------------------------------------------------------


def insert_shift(conn: sqlite3.Connection, shift: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO shifts
           (shift_id, shift_number, started_at, ended_at, detection_method, notes, simulation_run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            shift["shift_id"],
            shift["shift_number"],
            shift["started_at"],
            shift.get("ended_at"),
            shift.get("detection_method", "time_based"),
            shift.get("notes"),
            shift.get("simulation_run_id"),
        ),
    )


def get_active_shift(conn: sqlite3.Connection) -> Optional[Dict]:
    row = conn.execute(
        "SELECT * FROM shifts WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_shift_default_site(conn: sqlite3.Connection, shift_id: str) -> Optional[str]:
    """Return the default site_id announced when the shift started, or None."""
    if not shift_id:
        return None
    row = conn.execute(
        "SELECT default_site_id FROM shifts WHERE shift_id=?", (shift_id,)
    ).fetchone()
    return row[0] if row and row[0] else None


def get_shift_default_sites(conn: sqlite3.Connection, shift_id: str) -> List[str]:
    """
    Return list of default site_ids for a shift (from default_site_ids JSON column).
    Falls back to [default_site_id] if default_site_ids not set.
    Returns empty list if no default sites.
    """
    row = conn.execute(
        "SELECT default_site_id, default_site_ids FROM shifts WHERE shift_id=?",
        (shift_id,),
    ).fetchone()
    if not row:
        return []
    # Prefer JSON array column
    if row["default_site_ids"]:
        try:
            ids = json.loads(row["default_site_ids"])
            if isinstance(ids, list) and ids:
                return ids
        except Exception:
            pass
    # Fall back to single column
    if row["default_site_id"]:
        return [row["default_site_id"]]
    return []


def merge_trucks(conn: sqlite3.Connection, src_id: str, dst_id: str) -> dict:
    """
    Merge truck src_id into dst_id:
    - Copy all src aliases into dst aliases (deduplicated)
    - Reassign all events from src to dst
    - Deactivate src truck
    Returns {"merged": True, "aliases_added": [...], "events_reassigned": N}
    """
    src = conn.execute("SELECT * FROM trucks WHERE truck_id=?", (src_id,)).fetchone()
    dst = conn.execute("SELECT * FROM trucks WHERE truck_id=?", (dst_id,)).fetchone()
    if not src or not dst:
        raise ValueError(f"Truck not found: src={src_id!r} dst={dst_id!r}")

    src_aliases = json.loads(src["aliases"] or "[]")
    dst_aliases = json.loads(dst["aliases"] or "[]")

    # Add src truck_id and its aliases to dst (deduplicated, case-insensitive)
    dst_lower = {a.lower() for a in dst_aliases}
    new_aliases = list(dst_aliases)
    added = []
    for a in [src_id] + src_aliases:
        if a.lower() not in dst_lower:
            new_aliases.append(a)
            dst_lower.add(a.lower())
            added.append(a)

    conn.execute(
        "UPDATE trucks SET aliases=? WHERE truck_id=?",
        (json.dumps(new_aliases), dst_id),
    )

    # Reassign all events
    result = conn.execute(
        "UPDATE events SET truck_id=? WHERE truck_id=?", (dst_id, src_id)
    )
    events_reassigned = result.rowcount

    # Deactivate src truck
    conn.execute("UPDATE trucks SET is_active=0 WHERE truck_id=?", (src_id,))

    return {
        "merged": True,
        "src_id": src_id,
        "dst_id": dst_id,
        "aliases_added": added,
        "events_reassigned": events_reassigned,
    }


def get_all_shifts(conn: sqlite3.Connection) -> List[Dict]:
    rows = conn.execute("SELECT * FROM shifts ORDER BY started_at DESC").fetchall()
    return [dict(r) for r in rows]


def close_shift(conn: sqlite3.Connection, shift_id: str, ended_at: str) -> None:
    conn.execute(
        "UPDATE shifts SET ended_at=? WHERE shift_id=?",
        (ended_at, shift_id),
    )


def get_shift_config(conn: sqlite3.Connection) -> List[Dict]:
    rows = conn.execute("SELECT * FROM shift_config ORDER BY shift_number").fetchall()
    return [dict(r) for r in rows]


def update_shift_config(
    conn: sqlite3.Connection,
    shift_number: int,
    start_time: str,
    expected_end: Optional[str],
    wa_keyword: Optional[str],
) -> None:
    conn.execute(
        """UPDATE shift_config
           SET start_time=?, expected_end=?, wa_keyword=?, updated_at=?
           WHERE shift_number=?""",
        (
            start_time,
            expected_end,
            wa_keyword,
            now_ist_iso(),
            shift_number,
        ),
    )


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def insert_correction(conn: sqlite3.Connection, c: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO corrections
           (correction_id, original_event_id, corrected_by, corrected_at,
            field_changed, original_value, corrected_value, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            c["correction_id"],
            c["original_event_id"],
            c["corrected_by"],
            c.get("corrected_at", now_ist_iso()),
            c["field_changed"],
            c.get("original_value"),
            c.get("corrected_value"),
            c.get("note"),
        ),
    )
    # Mark event as corrected
    conn.execute(
        "UPDATE events SET corrected=TRUE, corrected_at=? WHERE event_id=?",
        (
            c.get("corrected_at", now_ist_iso()),
            c["original_event_id"],
        ),
    )


def get_corrections_for_event(conn: sqlite3.Connection, event_id: str) -> List[Dict]:
    rows = conn.execute(
        "SELECT * FROM corrections WHERE original_event_id=? ORDER BY corrected_at ASC",
        (event_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Message-commit map (operator panel)
# ---------------------------------------------------------------------------


def get_messages_page(
    conn: sqlite3.Connection,
    page: int = 1,
    limit: int = 20,
    status_filter: str = "all",
    search: str = "",
    hide_noise: bool = False,
    shift_id: str = "",
) -> Dict[str, Any]:
    """
    Paginated message list. Returns one item per raw_message with all linked
    events nested under an 'events' key (may be an empty list).
    """
    offset = (page - 1) * limit
    where_clauses: list = []
    params: list = []

    if shift_id:
        where_clauses.append(
            "EXISTS (SELECT 1 FROM events e WHERE e.msg_id = r.msg_id AND e.shift_id = ?)"
        )
        params.append(shift_id)

    if hide_noise:
        # Exclude messages whose only events are NOISE (messages with no events pass through)
        where_clauses.append(
            "(NOT EXISTS (SELECT 1 FROM events e WHERE e.msg_id = r.msg_id)"
            " OR EXISTS (SELECT 1 FROM events e WHERE e.msg_id = r.msg_id AND e.commit_status != 'NOISE'))"
        )

    if status_filter != "all":
        status_map = {
            "committed": "COMMITTED",
            "held": "HELD",
            "flagged": "FLAGGED",
            "corrected": "CORRECTED",
        }
        db_status = status_map.get(status_filter.lower())
        if db_status:
            where_clauses.append(
                "EXISTS (SELECT 1 FROM events e WHERE e.msg_id = r.msg_id AND e.commit_status = ?)"
            )
            params.append(db_status)

    if search:
        like = f"%{search}%"
        where_clauses.append(
            "(r.raw_text LIKE ?"
            " OR EXISTS (SELECT 1 FROM events e WHERE e.msg_id = r.msg_id"
            "            AND (e.truck_id LIKE ? OR e.site_id LIKE ?)))"
        )
        params.extend([like, like, like])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Count distinct messages
    total = conn.execute(
        f"SELECT COUNT(*) FROM raw_messages r {where_sql}", params
    ).fetchone()[0]

    # Fetch message rows for this page (one row per message)
    msg_rows = conn.execute(
        f"""SELECT r.msg_id, r.timestamp_iso, r.sender_name, r.sender_id, r.raw_text,
                   r.quoted_wa_message_id,
                   qr.raw_text as quoted_raw_text
            FROM raw_messages r
            LEFT JOIN raw_messages qr ON qr.msg_id = r.quoted_wa_message_id
            {where_sql}
            ORDER BY r.timestamp_iso DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()

    if not msg_rows:
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "pages": max(1, (total + limit - 1) // limit),
            "items": [],
        }

    msg_ids = [r["msg_id"] for r in msg_rows]
    placeholders = ",".join("?" * len(msg_ids))

    # Fetch all events for these messages in one query
    event_rows = conn.execute(
        f"""SELECT e.event_id, e.msg_id, e.truck_id, e.truck_alias, e.status,
                   e.site_id, e.site_alias, e.confidence, e.commit_status,
                   e.commit_path, e.corrected, e.inferred, e.reasoning,
                   e.shift_id, e.wa_message_id, e.timestamp_effective,
                   t.display_name AS truck_name, s.display_name AS site_name
            FROM events e
            LEFT JOIN trucks t ON t.truck_id = e.truck_id
            LEFT JOIN sites  s ON s.site_id  = e.site_id
            WHERE e.msg_id IN ({placeholders})
            ORDER BY e.timestamp_effective ASC, e.created_at ASC""",
        msg_ids,
    ).fetchall()

    events_by_msg: Dict[str, list] = {}
    for ev in event_rows:
        mid = ev["msg_id"]
        if mid not in events_by_msg:
            events_by_msg[mid] = []
        events_by_msg[mid].append(dict(ev))

    items = []
    for r in msg_rows:
        d = dict(r)
        d["events"] = events_by_msg.get(d["msg_id"], [])
        items.append(d)

    return {
        "total": total,
        "page": page,
        "limit": limit,
        "pages": max(1, (total + limit - 1) // limit),
        "items": items,
    }


def get_fleet_kpis(
    conn: sqlite3.Connection, shift_id: Optional[str] = None
) -> Dict[str, Any]:
    """KPI summary: in loading, unloading, transit counts + loaded in current shift."""
    fleet = get_fleet_state(conn, shift_id=shift_id)
    in_loading = [
        t
        for t in fleet
        if t.get("status") == "LS" and _is_loading_site(conn, t.get("site_id"))
    ]
    in_unloading = [
        t
        for t in fleet
        if t.get("status") == "US" and not _is_loading_site(conn, t.get("site_id"))
    ]
    in_transit = [t for t in fleet if t.get("status") == "LEFT"]

    loaded_rows = (
        conn.execute(
            """SELECT DISTINCT truck_id FROM events
           WHERE commit_status IN ('COMMITTED','FLAGGED') AND status='LO'
             AND shift_id=?""",
            (shift_id,),
        ).fetchall()
        if shift_id
        else []
    )
    loaded_in_shift = [r[0] for r in loaded_rows]

    return {
        "in_loading": {
            "count": len(in_loading),
            "trucks": [t.get("truck_id") for t in in_loading],
        },
        "in_unloading": {
            "count": len(in_unloading),
            "trucks": [t.get("truck_id") for t in in_unloading],
        },
        "in_transit": {
            "count": len(in_transit),
            "trucks": [t.get("truck_id") for t in in_transit],
        },
        "loaded_shift": {
            "count": len(loaded_in_shift),
            "trucks": loaded_in_shift,
        },
    }


def _is_loading_site(conn: sqlite3.Connection, site_id: Optional[str]) -> bool:
    if not site_id:
        return False
    row = conn.execute(
        "SELECT site_type FROM sites WHERE site_id=?", (site_id,)
    ).fetchone()
    return row and row[0] == "loading"


def get_site_load_summary(
    conn: sqlite3.Connection, shift_id: Optional[str] = None
) -> List[Dict]:
    """Per-site count of trolleys that completed a load (LO) in the current shift."""
    if not shift_id:
        return []
    rows = conn.execute(
        """SELECT s.display_name as site_name, e.site_id,
                  COUNT(DISTINCT e.truck_id) as count
           FROM events e
           JOIN sites s ON e.site_id = s.site_id
           WHERE e.commit_status IN ('COMMITTED','FLAGGED') AND e.status='LO'
             AND e.shift_id=?
           GROUP BY e.site_id
           ORDER BY count DESC""",
        (shift_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def log_audit(
    conn: sqlite3.Connection,
    action: str,
    table_name: str,
    record_id: str,
    old_value: Any = None,
    new_value: Any = None,
    triggered_by: str = "pipeline",
) -> None:
    from uuid import uuid4

    conn.execute(
        """INSERT INTO audit_log
           (log_id, action, table_name, record_id, old_value, new_value, triggered_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid4()),
            action,
            table_name,
            record_id,
            json.dumps(old_value, ensure_ascii=False)
            if old_value is not None
            else None,
            json.dumps(new_value, ensure_ascii=False)
            if new_value is not None
            else None,
            triggered_by,
        ),
    )
