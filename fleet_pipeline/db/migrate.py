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


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
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
        _add_column(conn, "events", "commit_path", "TEXT")       # green | amber | red
        _add_column(conn, "events", "corrected", "BOOLEAN DEFAULT FALSE")
        _add_column(conn, "events", "corrected_at", "TIMESTAMP")
        _add_column(conn, "events", "shift_id", "TEXT")

        print("\n[shifts] Adding new columns...")
        _add_column(conn, "shifts", "shift_name", "TEXT")

        print("\n[hitl_queue] Adding new columns...")
        _add_column(conn, "hitl_queue", "wa_message_id", "TEXT")

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
