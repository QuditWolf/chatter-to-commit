"""
DB migration — safely adds new columns to existing tables.
Safe to run multiple times (catches 'duplicate column' errors).

Run:
    python -m fleet_pipeline.db.migrate
"""

import sqlite3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db.database import init_db


def _add_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        print(f"  + {table}.{column}")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print(f"  = {table}.{column} (already exists)")
        else:
            raise


def run_migrations(db_path: str = DB_PATH) -> None:
    print(f"Running migrations on: {db_path}")

    # Ensure base tables exist first
    init_db(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")

        print("\n[events] Adding new columns...")
        _add_column(conn, "events", "wa_message_id", "TEXT")
        _add_column(conn, "events", "commit_path", "TEXT")  # green | amber | red
        _add_column(conn, "events", "corrected", "BOOLEAN DEFAULT FALSE")
        _add_column(conn, "events", "corrected_at", "TIMESTAMP")
        _add_column(conn, "events", "shift_id", "TEXT")
        _add_column(conn, "events", "timestamp_approximate", "BOOLEAN DEFAULT FALSE")
        _add_column(conn, "events", "commit_notif_bot_msg_id", "TEXT")

        print("\n[shifts] Adding new columns...")
        _add_column(conn, "shifts", "shift_name", "TEXT")
        _add_column(conn, "shifts", "default_site_id", "TEXT")
        _add_column(conn, "shifts", "default_site_ids", "TEXT")
        _add_column(conn, "shifts", "start_notif_bot_msg_id", "TEXT")
        _add_column(conn, "shifts", "end_notif_bot_msg_id", "TEXT")

        print("\n[shift_events] Creating table...")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shift_events (
                shift_event_id   TEXT PRIMARY KEY,
                shift_id         TEXT REFERENCES shifts(shift_id),
                status           TEXT NOT NULL,
                timestamp_iso    TEXT NOT NULL,
                commit_status    TEXT DEFAULT 'COMMITTED',
                wa_message_id    TEXT,
                site_id          TEXT,
                site_ids_json    TEXT,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shift_events_shift_id ON shift_events(shift_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shift_events_wa_message_id ON shift_events(wa_message_id)"
        )
        print("  + shift_events table created")

        # Migrate existing shifts → shift_events
        print("\n[MIGRATION] Creating shift_events for existing shifts...")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT shift_id, started_at, ended_at, default_site_id, default_site_ids FROM shifts"
        ).fetchall()
        imported = 0
        for s in existing:
            has_start = conn.execute(
                "SELECT 1 FROM shift_events WHERE shift_id=? AND status='SHIFT_START'",
                (s["shift_id"],),
            ).fetchone()
            if not has_start:
                conn.execute(
                    """INSERT INTO shift_events
                       (shift_event_id, shift_id, status, timestamp_iso, commit_status, site_id, site_ids_json)
                       VALUES (?, ?, 'SHIFT_START', ?, 'COMMITTED', ?, ?)""",
                    (
                        f"migrated_start_{s['shift_id']}",
                        s["shift_id"],
                        s["started_at"],
                        s["default_site_id"],
                        s["default_site_ids"],
                    ),
                )
                imported += 1
            if s["ended_at"]:
                has_end = conn.execute(
                    "SELECT 1 FROM shift_events WHERE shift_id=? AND status='SHIFT_END'",
                    (s["shift_id"],),
                ).fetchone()
                if not has_end:
                    conn.execute(
                        """INSERT INTO shift_events
                           (shift_event_id, shift_id, status, timestamp_iso, commit_status)
                           VALUES (?, ?, 'SHIFT_END', ?, 'COMMITTED')""",
                        (
                            f"migrated_end_{s['shift_id']}",
                            s["shift_id"],
                            s["ended_at"],
                        ),
                    )
                    imported += 1
        if imported:
            print(f"  + Created {imported} shift_events from existing shifts")
        else:
            print("  = No migration needed (shift_events already exist)")

        print("\n[shifts] Adding new columns...")
        _add_column(conn, "shifts", "is_deleted", "BOOLEAN DEFAULT FALSE")

        print("\n[hitl_queue] Adding new columns...")
        _add_column(conn, "hitl_queue", "wa_message_id", "TEXT")

        print("\n[raw_messages] Adding new columns...")
        _add_column(conn, "raw_messages", "quoted_wa_message_id", "TEXT")

        print("\n[llm_outputs] Creating table...")
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_outputs (
                    output_id          TEXT PRIMARY KEY,
                    msg_id             TEXT REFERENCES raw_messages(msg_id),
                    raw_llm_text       TEXT,
                    parsed_json        TEXT,
                    sanitizer_issues   TEXT,
                    model_name         TEXT,
                    prompt_hash        TEXT,
                    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_outputs_msg ON llm_outputs(msg_id)"
            )
            print("  + llm_outputs table created")
        except sqlite3.OperationalError as e:
            if "already exists" in str(e).lower():
                print("  = llm_outputs (already exists)")
            else:
                raise

        # ── Shift-assignment triggers ──────────────────────────────────────────
        # Drop first so that an updated trigger body is always applied on redeploy.
        # CREATE TRIGGER IF NOT EXISTS silently skips if the trigger already exists,
        # which would leave the old (broken) body in place.
        print("\n[triggers] Reinstalling shift-assignment triggers...")
        for t in ("trg_events_shift_on_insert",
                  "trg_events_shift_on_ts_update",
                  "trg_shifts_recompute_on_boundary_update"):
            conn.execute(f"DROP TRIGGER IF EXISTS {t}")
            print(f"  - dropped {t} (if existed)")

        # strftime('%s', ...) converts any ISO-8601 format (Z, +05:30, bare local)
        # to Unix epoch before comparing. This avoids string-comparison bugs when
        # timestamp_effective is stored in UTC (Z) but started_at/ended_at in IST (+05:30).
        conn.execute("""
            CREATE TRIGGER trg_events_shift_on_insert
            AFTER INSERT ON events
            BEGIN
              UPDATE events SET shift_id = (
                SELECT s.shift_id FROM shifts s
                WHERE CAST(strftime('%s', s.started_at) AS INTEGER)
                        <= CAST(strftime('%s', NEW.timestamp_effective) AS INTEGER)
                  AND (s.ended_at IS NULL OR
                       CAST(strftime('%s', s.ended_at) AS INTEGER)
                         > CAST(strftime('%s', NEW.timestamp_effective) AS INTEGER))
                  AND (s.is_deleted IS NULL OR s.is_deleted = 0)
                ORDER BY s.started_at DESC LIMIT 1
              )
              WHERE event_id = NEW.event_id;
            END
        """)
        print("  + trg_events_shift_on_insert")

        conn.execute("""
            CREATE TRIGGER trg_events_shift_on_ts_update
            AFTER UPDATE OF timestamp_effective ON events
            BEGIN
              UPDATE events SET shift_id = (
                SELECT s.shift_id FROM shifts s
                WHERE CAST(strftime('%s', s.started_at) AS INTEGER)
                        <= CAST(strftime('%s', NEW.timestamp_effective) AS INTEGER)
                  AND (s.ended_at IS NULL OR
                       CAST(strftime('%s', s.ended_at) AS INTEGER)
                         > CAST(strftime('%s', NEW.timestamp_effective) AS INTEGER))
                  AND (s.is_deleted IS NULL OR s.is_deleted = 0)
                ORDER BY s.started_at DESC LIMIT 1
              )
              WHERE event_id = NEW.event_id;
            END
        """)
        print("  + trg_events_shift_on_ts_update")

        conn.execute("""
            CREATE TRIGGER trg_shifts_recompute_on_boundary_update
            AFTER UPDATE OF started_at, ended_at ON shifts
            BEGIN
              UPDATE events SET shift_id = (
                SELECT s.shift_id FROM shifts s
                WHERE CAST(strftime('%s', s.started_at) AS INTEGER)
                        <= CAST(strftime('%s', events.timestamp_effective) AS INTEGER)
                  AND (s.ended_at IS NULL OR
                       CAST(strftime('%s', s.ended_at) AS INTEGER)
                         > CAST(strftime('%s', events.timestamp_effective) AS INTEGER))
                  AND (s.is_deleted IS NULL OR s.is_deleted = 0)
                ORDER BY s.started_at DESC LIMIT 1
              )
              WHERE commit_status != 'DELETED';
            END
        """)
        print("  + trg_shifts_recompute_on_boundary_update")

        # ── Backfill: recompute shift_id for all existing events ──────────────
        # Uses epoch comparison so mixed-timezone storage (UTC Z vs IST +05:30) works.
        print("\n[backfill] Recomputing shift_id for all existing events...")
        result = conn.execute("""
            UPDATE events SET shift_id = (
              SELECT s.shift_id FROM shifts s
              WHERE CAST(strftime('%s', s.started_at) AS INTEGER)
                      <= CAST(strftime('%s', events.timestamp_effective) AS INTEGER)
                AND (s.ended_at IS NULL OR
                     CAST(strftime('%s', s.ended_at) AS INTEGER)
                       > CAST(strftime('%s', events.timestamp_effective) AS INTEGER))
                AND (s.is_deleted IS NULL OR s.is_deleted = 0)
              ORDER BY s.started_at DESC LIMIT 1
            )
            WHERE commit_status != 'DELETED'
        """)
        print(f"  + {result.rowcount} events recomputed")

        conn.commit()

    print("\nMigrations complete.")


def seed_shift_config(db_path: str = DB_PATH) -> None:
    """Insert default shift config rows if not present."""
    defaults = [
        (1, "06:00", "09:00", "s1"),
        (2, "13:00", "16:00", "s2"),
        (3, "17:00", "20:00", "s3"),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for shift_number, start, end, kw in defaults:
            conn.execute(
                """INSERT OR IGNORE INTO shift_config
                   (shift_number, start_time, expected_end, wa_keyword)
                   VALUES (?, ?, ?, ?)""",
                (shift_number, start, end, kw),
            )
        conn.commit()
    print("Shift config seeded.")


if __name__ == "__main__":
    run_migrations()
    seed_shift_config()
