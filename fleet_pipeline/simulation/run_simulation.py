"""
simulation/run_simulation.py
End-to-end replay of a WhatsApp export file through all pipeline levels.

Usage:
    # Mock mode (no GPU needed, for testing pipeline skeleton):
    python -m fleet_pipeline.simulation.run_simulation --input chat.txt --mock

    # Real mode (requires vLLM + Qwen model):
    python -m fleet_pipeline.simulation.run_simulation --input chat.txt

    # Replay a specific shift:
    python -m fleet_pipeline.simulation.run_simulation --input chat.txt --shift 2023-10-15_shift_1 --mock
"""
import argparse
import json
import sys
import os
from datetime import datetime, timezone
from typing import Dict
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db.database import init_db, db_conn, insert_simulation_run, update_simulation_run
from fleet_pipeline.db.seed_data import seed
from fleet_pipeline.db.migrate import run_migrations, seed_shift_config
from fleet_pipeline.pipeline.level1 import parse_chat_file
from fleet_pipeline.pipeline.level2 import Enricher, EnricherConfig
from fleet_pipeline.pipeline.level3 import Level3Processor, update_l3_history
from fleet_pipeline.pipeline.committer import Committer
from fleet_pipeline.pipeline.registries import load_truck_registry, load_site_registry, build_vocab_from_db
from fleet_pipeline.pipeline.shift_detector import ShiftDetector


def assign_rough_shift_ids(messages: list) -> list:
    """
    Assign rough_shift_id to each message based on date.
    Each calendar day = one shift (naive approach).
    """
    for msg in messages:
        ts = msg.get("timestamp_iso", "")[:10]  # YYYY-MM-DD
        msg["rough_shift_id"] = ts.replace("-", "") + "_shift_1"
    return messages


def run_simulation(
    input_path: str,
    db_path: str = DB_PATH,
    mock: bool = False,
    shift_filter: str = None,
    verbose: bool = False,
) -> Dict:
    print(f"\n{'='*60}")
    print(f"Fleet Log Pipeline — Simulation")
    print(f"Input: {input_path}")
    print(f"DB: {db_path}")
    print(f"Mode: {'MOCK' if mock else 'REAL LLM'}")
    if shift_filter:
        print(f"Shift filter: {shift_filter}")
    print(f"{'='*60}\n")

    # Ensure DB, migrations, and seed data
    run_migrations(db_path)
    seed_shift_config(db_path)
    seed(db_path)

    # Create simulation run record
    run_id = str(uuid4())
    with db_conn(db_path) as conn:
        insert_simulation_run(conn, {
            "run_id": run_id,
            "source_file": input_path,
            "notes": f"mock={mock}, shift_filter={shift_filter}",
        })

    # Level 1: parse
    print("Level 1: Parsing chat file...")
    source_name = os.path.basename(input_path)
    messages = parse_chat_file(input_path, source_name)
    messages = assign_rough_shift_ids(messages)
    print(f"  Parsed {len(messages)} messages")

    # Filter by shift if requested
    if shift_filter:
        messages = [m for m in messages if m.get("rough_shift_id") == shift_filter]
        print(f"  After shift filter: {len(messages)} messages")
        if not messages:
            print(f"ERROR: No messages found for shift '{shift_filter}'")
            sys.exit(1)

    # Level 2: enrich
    print("Level 2: Enriching messages...")
    truck_vocab, site_vocab = build_vocab_from_db(db_path)
    config = EnricherConfig(
        truck_vocab=truck_vocab,
        site_vocab=site_vocab,
        sender_window_minutes=60,
        truck_window_minutes=180,
        prev_limit=5,
    )
    enricher = Enricher(config=config)
    level2_messages = []
    for msg in messages:
        l2 = enricher.enrich_message(msg)
        # carry rough_shift_id through
        l2["rough_shift_id"] = msg.get("rough_shift_id")
        level2_messages.append(l2)
        enricher._history.append(msg)
    print(f"  Enriched {len(level2_messages)} messages")

    # Level 3: LLM
    print(f"Level 3: Running {'mock' if mock else 'LLM'} inference...")
    truck_registry = load_truck_registry(db_path)
    site_registry = load_site_registry(db_path)
    processor = Level3Processor(mock=mock)
    committer = Committer(db_path=db_path, simulation_run_id=run_id)

    stats = {
        "total_msgs": len(level2_messages),
        "committed": 0,
        "flagged": 0,
        "held": 0,
        "errors": 0,
        "hitl_created": 0,
    }

    l3_history = []

    # Long-lived connection for ShiftDetector (maintains state across messages)
    import sqlite3 as _sqlite3
    _sd_conn = _sqlite3.connect(db_path)
    _sd_conn.row_factory = _sqlite3.Row
    _sd_conn.execute("PRAGMA journal_mode=WAL")
    _sd_conn.execute("PRAGMA foreign_keys=ON")
    shift_detector = ShiftDetector(_sd_conn, simulation_run_id=run_id)

    for idx, l2_msg in enumerate(level2_messages, start=1):
        msg_id = l2_msg.get("raw", {}).get("msg_id", f"msg_{idx}")
        raw_text = l2_msg.get("raw", {}).get("raw_text", "")
        timestamp_iso = l2_msg.get("raw", {}).get("timestamp_iso", "")
        if verbose:
            print(f"  [{idx}/{len(level2_messages)}] {msg_id} — {l2_msg.get('candidate_msg_type')}")

        # Update shift state (handles WA signals and time-based boundaries)
        try:
            shift_id = shift_detector.process_message(raw_text, timestamp_iso)
            _sd_conn.commit()
        except Exception as e:
            shift_id = None
            if verbose:
                print(f"  [shift] Error: {e}", file=sys.stderr)

        # Skip SHIFT_SIGNAL messages — no fleet event to parse
        if l2_msg.get("candidate_msg_type") == "SHIFT_SIGNAL":
            continue

        try:
            result = processor.process_message(l2_msg, truck_registry, site_registry, l3_history)
        except Exception as e:
            print(f"  ERROR at msg {idx}: {e}", file=sys.stderr)
            stats["errors"] += 1
            continue

        # Inject resolved shift_id if LLM didn't provide one
        if not result.get("shift_id") and shift_id:
            result["shift_id"] = shift_id

        l3_history = update_l3_history(l3_history, result)

        try:
            commit_summary = committer.commit(result)
            stats["committed"] += commit_summary.get("committed", 0)
            stats["flagged"] += commit_summary.get("flagged", 0)
            stats["held"] += commit_summary.get("held", 0)
            stats["errors"] += commit_summary.get("errors", 0)
            stats["hitl_created"] += commit_summary.get("hitl_created", 0)
        except Exception as e:
            print(f"  COMMIT ERROR at msg {idx}: {e}", file=sys.stderr)
            stats["errors"] += 1

        # Progress dots
        if not verbose and idx % 50 == 0:
            print(f"  ...{idx}/{len(level2_messages)}")

    _sd_conn.commit()
    _sd_conn.close()

    # Update simulation run with final stats
    with db_conn(db_path) as conn:
        update_simulation_run(conn, run_id, stats)

    # Print summary
    print(f"\n{'='*60}")
    print(f"Simulation complete — run_id: {run_id}")
    print(f"  Total messages : {stats['total_msgs']}")
    print(f"  Committed      : {stats['committed']}")
    print(f"  Flagged        : {stats['flagged']}")
    print(f"  Held           : {stats['held']}")
    print(f"  Errors         : {stats['errors']}")
    print(f"  HITL questions : {stats['hitl_created']}")
    print(f"  DB: {db_path}")
    print(f"{'='*60}\n")

    return {"run_id": run_id, **stats}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run fleet pipeline simulation")
    parser.add_argument("--input", required=True, help="WhatsApp .txt export file")
    parser.add_argument("--db", default=DB_PATH, help="SQLite DB path")
    parser.add_argument("--mock", action="store_true", help="Use mock LLM (no GPU needed)")
    parser.add_argument("--shift", default=None, help="Filter to specific shift_id")
    parser.add_argument("--verbose", action="store_true", help="Print per-message progress")
    args = parser.parse_args()

    run_simulation(
        input_path=args.input,
        db_path=args.db,
        mock=args.mock,
        shift_filter=args.shift,
        verbose=args.verbose,
    )
