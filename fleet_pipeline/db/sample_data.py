"""
Seed realistic sample data for UI development and testing.
Simulates a busy shift on 2025-10-14 (one of the active historical days).

Run:
    python3 -m fleet_pipeline.db.sample_data
"""
import json
import sys
import os
from uuid import uuid4
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db.database import (
    db_conn, init_db, insert_raw_message, insert_event,
    insert_tally, insert_hitl_question, insert_simulation_run,
    update_simulation_run, log_audit,
)
from fleet_pipeline.db.seed_data import seed

# Historical shift date
BASE_DATE = "2025-10-14"
# "Current" snapshot date — used for fleet-state display
NOW_DATE  = "2026-03-15"

def ts(hour: int, minute: int = 0, date: str = BASE_DATE) -> str:
    """Return IST timestamp string for the given date at given time."""
    return f"{date}T{hour:02d}:{minute:02d}:00+05:30"


# All sample events: (truck_id, truck_alias, status, site_id, site_alias, hour, minute, confidence, sender)
SAMPLE_EVENTS = [
    # Morning start — trucks arrive at SOC
    ("TA", "A", "ENTER", "SOC", "SOC", 5, 35, 0.92, "Ramesh"),
    ("TB", "B", "ENTER", "SOC", "SOC", 5, 38, 0.91, "Ramesh"),
    ("TC", "C", "ENTER", "SOC", "SOC", 5, 42, 0.93, "Ramesh"),
    ("TA", "A", "LS",    "SOC", "SOC", 5, 50, 0.95, "Ramesh"),
    ("TB", "B", "LS",    "SOC", "SOC", 5, 52, 0.94, "Ramesh"),
    ("TD", "D", "ENTER", "SOC", "SOC", 6,  5, 0.90, "Suresh"),
    ("TC", "C", "LS",    "SOC", "SOC", 6,  8, 0.93, "Ramesh"),
    ("TA", "A", "LO",    "SOC", "SOC", 6, 30, 0.92, "Ramesh"),
    ("TB", "B", "LO",    "SOC", "SOC", 6, 35, 0.91, "Ramesh"),
    ("TA", "A", "LEFT",  "SOC", "SOC", 6, 38, 0.90, "Ramesh"),
    ("TB", "B", "LEFT",  "SOC", "SOC", 6, 40, 0.89, "Ramesh"),
    ("TD", "D", "LS",    "SOC", "SOC", 6, 42, 0.93, "Suresh"),
    ("TC", "C", "LO",    "SOC", "SOC", 7, 10, 0.92, "Ramesh"),
    # Mid-morning — BG and DAIRY action
    ("TA", "A", "ENTER", "BG",  "BG",  7, 15, 0.88, "Ramesh"),
    ("TB", "B", "ENTER", "BG",  "BG",  7, 20, 0.87, "Ramesh"),
    ("TC", "C", "LEFT",  "SOC", "SOC", 7, 18, 0.90, "Ramesh"),
    ("TE", "E", "ENTER", "SOC", "SOC", 7, 22, 0.91, "Suresh"),
    ("TD", "D", "LO",    "SOC", "SOC", 7, 35, 0.93, "Suresh"),
    ("TA", "A", "ENTER", "DAIRY","Dairy", 7, 45, 0.92, "Ramesh"),
    ("TB", "B", "ENTER", "DAIRY","Dairy", 7, 50, 0.91, "Ramesh"),
    ("TA", "A", "US",    "DAIRY","Dairy", 7, 48, 0.94, "Ramesh"),
    ("TB", "B", "US",    "DAIRY","Dairy", 7, 53, 0.93, "Ramesh"),
    ("TE", "E", "LS",    "SOC", "SOC",  8,  5, 0.94, "Suresh"),
    ("TD", "D", "LEFT",  "SOC", "SOC",  8,  8, 0.88, "Suresh"),
    ("TA", "A", "UO",    "DAIRY","Dairy", 8, 20, 0.93, "Ramesh"),
    ("TB", "B", "UO",    "DAIRY","Dairy", 8, 25, 0.92, "Ramesh"),
    ("TA", "A", "LEFT",  "DAIRY","Dairy", 8, 28, 0.90, "Ramesh"),
    ("TB", "B", "LEFT",  "DAIRY","Dairy", 8, 30, 0.89, "Ramesh"),
    # KN4 action
    ("TF", "F", "ENTER", "KN4", "KN4",  8, 35, 0.91, "Vikram"),
    ("TG", "G", "ENTER", "KN4", "KN4",  8, 38, 0.90, "Vikram"),
    ("TF", "F", "LS",    "KN4", "KN4",  8, 45, 0.93, "Vikram"),
    ("TG", "G", "LS",    "KN4", "KN4",  8, 50, 0.92, "Vikram"),
    ("TD", "D", "ENTER", "DAIRY","Dairy", 9,  5, 0.91, "Suresh"),
    ("TE", "E", "LO",    "SOC", "SOC",  9, 10, 0.93, "Suresh"),
    ("TD", "D", "US",    "DAIRY","Dairy", 9,  8, 0.92, "Suresh"),
    ("TF", "F", "LO",    "KN4", "KN4",  9, 20, 0.94, "Vikram"),
    ("TG", "G", "LO",    "KN4", "KN4",  9, 28, 0.93, "Vikram"),
    # Named trucks join
    ("T_ARJ_NOVO", "Arjun Novo", "ENTER", "SOC", "SOC",  9, 35, 0.87, "Arjun"),
    ("T_ARJ_WHITE","Arjun White","ENTER", "KN4", "KN4",  9, 40, 0.85, "Arjun"),
    ("TE", "E",    "LEFT",  "SOC", "SOC",  9, 42, 0.88, "Suresh"),
    ("TF", "F",    "LEFT",  "KN4", "KN4",  9, 50, 0.87, "Vikram"),
    ("TG", "G",    "LEFT",  "KN4", "KN4",  9, 55, 0.86, "Vikram"),
    ("T_ARJ_NOVO", "Arjun Novo", "LS",   "SOC", "SOC", 10,  5, 0.88, "Arjun"),
    ("T_ARJ_WHITE","Arjun White","LS",   "KN4", "KN4", 10, 10, 0.86, "Arjun"),
    # Midday cycle
    ("TA", "A",    "ENTER", "SOC", "SOC", 10, 20, 0.91, "Ramesh"),
    ("TB", "B",    "ENTER", "SOC", "SOC", 10, 25, 0.90, "Ramesh"),
    ("TA", "A",    "LS",    "SOC", "SOC", 10, 28, 0.93, "Ramesh"),
    ("T_ARJ_NOVO", "Arjun Novo", "LO",   "SOC", "SOC", 10, 45, 0.89, "Arjun"),
    ("T_ARJ_WHITE","Arjun White","LO",   "KN4", "KN4", 10, 50, 0.87, "Arjun"),
    ("TB", "B",    "LS",    "SOC", "SOC", 10, 52, 0.92, "Ramesh"),
    ("TA", "A",    "LO",    "SOC", "SOC", 11,  5, 0.92, "Ramesh"),
    ("T_ARJ_NOVO", "Arjun Novo", "LEFT", "SOC", "SOC", 11,  8, 0.87, "Arjun"),
    ("TA", "A",    "LEFT",  "SOC", "SOC", 11, 10, 0.90, "Ramesh"),
    ("TC", "C",    "ENTER", "KN4", "KN4", 11, 20, 0.91, "Ramesh"),
    ("TC", "C",    "LS",    "KN4", "KN4", 11, 30, 0.93, "Ramesh"),
    ("TB", "B",    "LO",    "SOC", "SOC", 11, 45, 0.91, "Ramesh"),
    ("TB", "B",    "LEFT",  "SOC", "SOC", 11, 50, 0.89, "Ramesh"),
    # Afternoon
    ("TA", "A",    "ENTER", "DAIRY","Dairy",12, 10, 0.90, "Ramesh"),
    ("TB", "B",    "ENTER", "DAIRY","Dairy",12, 15, 0.89, "Ramesh"),
    ("TA", "A",    "US",    "DAIRY","Dairy",12, 12, 0.92, "Ramesh"),
    ("TB", "B",    "US",    "DAIRY","Dairy",12, 18, 0.91, "Ramesh"),
    ("TC", "C",    "LO",    "KN4", "KN4", 12, 20, 0.93, "Ramesh"),
    ("TC", "C",    "LEFT",  "KN4", "KN4", 12, 25, 0.88, "Ramesh"),
    ("TA", "A",    "UO",    "DAIRY","Dairy",12, 40, 0.92, "Ramesh"),
    ("TB", "B",    "UO",    "DAIRY","Dairy",12, 45, 0.91, "Ramesh"),
    # Low confidence events (should be FLAGGED/HELD)
    ("T_ARJ_WHITE","Arjun White","ENTER","DAIRY","Dairy",13,  5, 0.52, "Arjun"),
    ("T_ARJ_NOVO","Arjun Novo","ENTER", "BG",  "BG",  13, 20, 0.58, "Arjun"),
    # Late afternoon
    ("TA", "A",    "ENTER", "SOC", "SOC", 14,  0, 0.91, "Ramesh"),
    ("TA", "A",    "LS",    "SOC", "SOC", 14,  5, 0.94, "Ramesh"),
    ("TB", "B",    "ENTER", "KN4", "KN4", 14, 10, 0.90, "Suresh"),
    ("TB", "B",    "LS",    "KN4", "KN4", 14, 18, 0.93, "Suresh"),
    ("TD", "D",    "ENTER", "SOC", "SOC", 14, 30, 0.91, "Suresh"),
    ("TD", "D",    "LS",    "SOC", "SOC", 14, 35, 0.92, "Suresh"),
    ("TA", "A",    "LO",    "SOC", "SOC", 15,  0, 0.93, "Ramesh"),
    ("TB", "B",    "LO",    "KN4", "KN4", 15, 10, 0.92, "Suresh"),
    ("TA", "A",    "LEFT",  "SOC", "SOC", 15,  5, 0.89, "Ramesh"),
    ("TB", "B",    "LEFT",  "KN4", "KN4", 15, 15, 0.88, "Suresh"),
    ("TD", "D",    "LO",    "SOC", "SOC", 15, 20, 0.91, "Suresh"),
    ("TD", "D",    "LEFT",  "SOC", "SOC", 15, 25, 0.88, "Suresh"),
    # Evening wind-down
    ("TE", "E",    "ENTER", "KN4", "KN4", 16, 10, 0.90, "Suresh"),
    ("TE", "E",    "LS",    "KN4", "KN4", 16, 18, 0.93, "Suresh"),
    ("TC", "C",    "ENTER", "SOC", "SOC", 16, 25, 0.91, "Ramesh"),
    ("TC", "C",    "LS",    "SOC", "SOC", 16, 32, 0.93, "Ramesh"),
    ("TE", "E",    "LO",    "KN4", "KN4", 17,  0, 0.92, "Suresh"),
    ("TC", "C",    "LO",    "SOC", "SOC", 17,  5, 0.91, "Ramesh"),
    ("TE", "E",    "LEFT",  "KN4", "KN4", 17,  5, 0.88, "Suresh"),
    ("TC", "C",    "LEFT",  "SOC", "SOC", 17, 10, 0.87, "Ramesh"),
]


# Current-state snapshot — placed on NOW_DATE so fleet state shows active loading.
# Several trucks in LS/ENTER, a couple LO, one LEFT.
CURRENT_SNAPSHOT = [
    # truck_id,         alias,         status,  site_id, site_alias, hour, min, conf, sender
    ("TA",          "A",           "ENTER", "SOC",   "SOC",   9, 48, 0.93, "Ramesh"),
    ("TC",          "C",           "ENTER", "SOC",   "SOC",   9, 52, 0.92, "Ramesh"),
    ("TD",          "D",           "ENTER", "KN4",   "KN4",   9, 55, 0.91, "Suresh"),
    ("TG",          "G",           "ENTER", "KN4",   "KN4",  10,  2, 0.90, "Vikram"),
    ("TA",          "A",           "LS",    "SOC",   "SOC",  10,  5, 0.95, "Ramesh"),
    ("TC",          "C",           "LS",    "SOC",   "SOC",  10,  8, 0.94, "Ramesh"),
    ("TD",          "D",           "LS",    "KN4",   "KN4",  10, 10, 0.93, "Suresh"),
    ("TG",          "G",           "LS",    "KN4",   "KN4",  10, 15, 0.92, "Vikram"),
    ("TB",          "B",           "LO",    "SOC",   "SOC",  10, 20, 0.94, "Ramesh"),
    ("TE",          "E",           "LO",    "KN4",   "KN4",  10, 25, 0.93, "Suresh"),
    ("TF",          "F",           "LEFT",  "KN4",   "KN4",  10, 18, 0.91, "Vikram"),
    ("T_ARJ_WHITE", "Arjun White", "LEFT",  "SOC",   "SOC",  10,  5, 0.89, "Arjun"),
    ("T_ARJ_NOVO",  "Arjun Novo",  "ENTER", "BG",    "BG",   10, 28, 0.90, "Arjun"),
]


def seed_sample(db_path: str = DB_PATH) -> None:
    init_db(db_path)
    seed(db_path)

    run_id = "sample-run-20251014"
    with db_conn(db_path) as conn:
        # Check if already seeded
        existing = conn.execute(
            "SELECT run_id FROM simulation_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing:
            print("Sample data already seeded. Delete data/fleet.db to re-seed.")
            return

        insert_simulation_run(conn, {
            "run_id": run_id,
            "source_file": "sample_data",
            "notes": "Manually seeded realistic sample data for 2025-10-14",
        })

        stats = {"committed": 0, "flagged": 0, "held": 0, "hitl_created": 0}

        for (truck_id, truck_alias, status, site_id, site_alias,
             hour, minute, confidence, sender) in SAMPLE_EVENTS:

            msg_id = str(uuid4())
            raw_text = f"{truck_alias} {status} {site_alias}"
            insert_raw_message(conn, {
                "msg_id": msg_id,
                "source_file": "sample_data.py",
                "timestamp_iso": ts(hour, minute),
                "sender_name": sender,
                "sender_id": None,
                "raw_text": raw_text,
                "is_edited": False,
                "is_deleted": False,
                "media_type": None,
            })

            # Decide commit status
            if confidence >= 0.85:
                commit_status = "COMMITTED"
                stats["committed"] += 1
            elif confidence >= 0.6:
                commit_status = "FLAGGED"
                stats["flagged"] += 1
            else:
                commit_status = "HELD"
                stats["held"] += 1

            event_id = str(uuid4())
            insert_event(conn, {
                "event_id": event_id,
                "msg_id": msg_id,
                "truck_id": truck_id,
                "truck_alias": truck_alias,
                "status": status,
                "site_id": site_id,
                "site_alias": site_alias,
                "material": None,
                "timestamp_effective": ts(hour, minute),
                "inferred": False,
                "confidence": confidence,
                "reasoning": f"sample data: {truck_alias} {status}",
                "commit_status": commit_status,
                "processing_id": run_id,
                "simulation_run_id": run_id,
            })
            log_audit(conn, "INSERT", "events", event_id, new_value={"commit_status": commit_status})

        # HITL questions
        hitl_samples = [
            ("UNKNOWN_TRUCK",
             "Unknown truck 'NH-123' — does not match any registered truck.\n\nOriginal message: \"NH-123 LS SOC\"\n\nEnter the truck code (e.g. TB), or to register a new truck: new:TX:Display Name:alias",
             {"truck_alias": "NH-123", "raw_text": "NH-123 LS SOC"}),
            ("UNKNOWN_SITE",
             "Unknown site 'Bhandaagar' — does not match any registered site.\n\nOriginal message: \"C LO Bhandaagar\"\n\nEnter the site code (e.g. BG), or to register a new site: new:SNAME:Display Name:loading:alias",
             {"site_alias": "Bhandaagar", "raw_text": "C LO Bhandaagar"}),
            ("LOW_CONFIDENCE",
             "Low confidence (52%) — please verify this interpretation.\n\nOriginal message: \"Arjun White enter Dairy\"\n\nParsed as: Arjun White ENTER at Dairy\n\nType CONFIRM to accept, or enter a correction.",
             {"confidence": 0.52, "raw_text": "Arjun White enter Dairy", "parsed": "T_ARJ_WHITE ENTER DAIRY"}),
            ("CORRECTION_AMBIGUOUS",
             "This message appears to be a correction but it is unclear what it corrects.\n\nOriginal message: \"Truck D was at KN4 earlier, not SOC — please fix the last entry\"\n\nPlease clarify: which truck/status/site is being corrected, and what should it be changed to?",
             {"raw_text": "Truck D was at KN4 earlier, not SOC — please fix the last entry"}),
        ]
        for qtype, qtext, ctx in hitl_samples:
            qid = str(uuid4())
            insert_hitl_question(conn, {
                "question_id": qid,
                "msg_id": None,
                "event_id": None,
                "question_type": qtype,
                "question_text": qtext,
                "context": ctx,
                "status": "OPEN",
                "simulation_run_id": run_id,
            })
            stats["hitl_created"] += 1

        # Tally
        insert_tally(conn, {
            "tally_id": str(uuid4()),
            "msg_id": None,
            "timestamp_iso": ts(18, 0),
            "tally_data": {
                "loaded": {"SOC": 28, "KN4": 14},
                "unloaded": {"DAIRY": 38},
                "trucks_active": 8,
            },
            "commit_status": "COMMITTED",
            "simulation_run_id": run_id,
        })

        stats["total_msgs"] = len(SAMPLE_EVENTS)
        update_simulation_run(conn, run_id, stats)

    print(f"Seeded {len(SAMPLE_EVENTS)} historical events | {stats['committed']} committed | "
          f"{stats['flagged']} flagged | {stats['held']} held | "
          f"{stats['hitl_created']} HITL questions")

    # ── Current-state snapshot ─────────────────────────────────────────────
    now_run_id = "sample-run-current"
    with db_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT run_id FROM simulation_runs WHERE run_id=?", (now_run_id,)
        ).fetchone()
        if existing:
            return

        insert_simulation_run(conn, {
            "run_id": now_run_id,
            "source_file": "sample_data",
            "notes": f"Current-state snapshot seeded on {NOW_DATE}",
        })

        for (truck_id, truck_alias, status, site_id, site_alias,
             hour, minute, confidence, sender) in CURRENT_SNAPSHOT:
            msg_id = str(uuid4())
            insert_raw_message(conn, {
                "msg_id": msg_id,
                "source_file": "sample_data.py",
                "timestamp_iso": ts(hour, minute, NOW_DATE),
                "sender_name": sender,
                "sender_id": None,
                "raw_text": f"{truck_alias} {status} {site_alias}",
                "is_edited": False,
                "is_deleted": False,
                "media_type": None,
            })
            event_id = str(uuid4())
            insert_event(conn, {
                "event_id": event_id,
                "msg_id": msg_id,
                "truck_id": truck_id,
                "truck_alias": truck_alias,
                "status": status,
                "site_id": site_id,
                "site_alias": site_alias,
                "material": None,
                "timestamp_effective": ts(hour, minute, NOW_DATE),
                "inferred": False,
                "confidence": confidence,
                "reasoning": f"current snapshot: {truck_alias} {status}",
                "commit_status": "COMMITTED",
                "processing_id": now_run_id,
                "simulation_run_id": now_run_id,
            })
            log_audit(conn, "INSERT", "events", event_id, new_value={"commit_status": "COMMITTED"})

        update_simulation_run(conn, now_run_id, {"committed": len(CURRENT_SNAPSHOT)})

    print(f"Seeded {len(CURRENT_SNAPSHOT)} current-state events on {NOW_DATE}")


if __name__ == "__main__":
    seed_sample()
