"""
Reset operational data while keeping truck/site registry intact.
Run this to clear last year's data and start fresh.

  python3 reset_data.py
  python3 reset_data.py --confirm    # skip the confirmation prompt
"""
import os
import sqlite3, sys, pathlib

# Use FLEET_DB_PATH env var if set (for container), otherwise use default path
DB_PATH_STR = os.environ.get("FLEET_DB_PATH", str(pathlib.Path(__file__).parent / "fleet_pipeline" / "data" / "fleet.db"))
DB_PATH = pathlib.Path(DB_PATH_STR)

TABLES_TO_CLEAR = [
    "events",
    "raw_messages",
    "hitl_queue",
    "tallies",
    "wa_messages",
    "shifts",
    "corrections",
]

def reset(confirm=False):
    if not DB_PATH.exists():
        print(f"DB not found at {DB_PATH}")
        sys.exit(1)

    if not confirm:
        print(f"This will DELETE all operational data from:\n  {DB_PATH}\n")
        print("Tables cleared:", ", ".join(TABLES_TO_CLEAR))
        print("Kept intact:   trucks, sites, shift_config\n")
        ans = input("Type YES to continue: ").strip()
        if ans != "YES":
            print("Aborted.")
            sys.exit(0)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for t in TABLES_TO_CLEAR:
            try:
                n = conn.execute(f"DELETE FROM {t}").rowcount
                print(f"  cleared {t:20s} ({n} rows)")
            except sqlite3.OperationalError as e:
                print(f"  skipped {t:20s} ({e})")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()

    # VACUUM must run outside a transaction
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("VACUUM")

    print("\nDone. Database is clean — ready for a fresh session.")

if __name__ == "__main__":
    reset(confirm="--confirm" in sys.argv)
