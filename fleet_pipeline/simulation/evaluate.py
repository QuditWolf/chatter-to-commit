"""
simulation/evaluate.py
Compare pipeline output vs manual ground truth labels.

Usage:
    python -m fleet_pipeline.simulation.evaluate \
        --run-id <run_id> \
        --ground-truth ground_truth.json

Ground truth format (list of dicts):
[
  {
    "msg_id": "...",
    "expected_msg_type": "STATUS_UPDATE",
    "expected_events": [
      {"truck_id": "TB", "status": "LS", "site_id": "SOC"}
    ]
  },
  ...
]
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db.database import db_conn


def load_ground_truth(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_pipeline_events(conn, run_id: str) -> dict:
    """Load all events for a run, keyed by msg_id."""
    rows = conn.execute(
        "SELECT * FROM events WHERE simulation_run_id=?", (run_id,)
    ).fetchall()
    result = {}
    for r in rows:
        d = dict(r)
        msg_id = d["msg_id"]
        result.setdefault(msg_id, []).append(d)
    return result


def evaluate(run_id: str, ground_truth_path: str, db_path: str = DB_PATH) -> dict:
    gt = load_ground_truth(ground_truth_path)

    with db_conn(db_path) as conn:
        pipeline_events = load_pipeline_events(conn, run_id)

    total = len(gt)
    exact_matches = 0
    partial_matches = 0
    misses = 0
    false_positives = 0

    details = []

    for gt_item in gt:
        msg_id = gt_item["msg_id"]
        expected_events = gt_item.get("expected_events", [])
        actual_events = pipeline_events.get(msg_id, [])

        if not expected_events and not actual_events:
            exact_matches += 1
            details.append({"msg_id": msg_id, "result": "exact_match"})
            continue

        matched = []
        for exp in expected_events:
            found = False
            for act in actual_events:
                if (act.get("truck_id") == exp.get("truck_id") and
                        act.get("status") == exp.get("status") and
                        act.get("site_id") == exp.get("site_id")):
                    found = True
                    break
            matched.append(found)

        if all(matched):
            exact_matches += 1
            details.append({"msg_id": msg_id, "result": "exact_match"})
        elif any(matched):
            partial_matches += 1
            details.append({
                "msg_id": msg_id,
                "result": "partial_match",
                "matched": sum(matched),
                "total": len(matched),
            })
        else:
            misses += 1
            details.append({
                "msg_id": msg_id,
                "result": "miss",
                "expected": expected_events,
                "actual": [{"truck_id": a.get("truck_id"), "status": a.get("status"),
                             "site_id": a.get("site_id")} for a in actual_events],
            })

    # False positives: events committed for msg_ids not in ground truth
    gt_msg_ids = {item["msg_id"] for item in gt}
    for msg_id in pipeline_events:
        if msg_id not in gt_msg_ids:
            false_positives += len(pipeline_events[msg_id])

    precision = exact_matches / total if total > 0 else 0
    report = {
        "run_id": run_id,
        "total_gt_items": total,
        "exact_matches": exact_matches,
        "partial_matches": partial_matches,
        "misses": misses,
        "false_positives": false_positives,
        "precision": round(precision, 3),
        "details": details,
    }

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate pipeline against ground truth")
    parser.add_argument("--run-id", required=True, help="Simulation run_id to evaluate")
    parser.add_argument("--ground-truth", required=True, help="Path to ground truth JSON")
    parser.add_argument("--db", default=DB_PATH, help="SQLite DB path")
    args = parser.parse_args()

    evaluate(args.run_id, args.ground_truth, args.db)
