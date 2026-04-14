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
