"""
Level 3 output schema validator.
Checks required keys, event fields, and confidence range.
"""
from typing import Any, Dict, List, Tuple

REQUIRED_TOP_LEVEL = [
    "msg_type", "events", "tally", "query",
    "notes", "overall_confidence", "shift_id", "commit_recommendation",
]

VALID_MSG_TYPES = {
    "STATUS_UPDATE", "TALLY_UPDATE", "QUERY", "NOISE",
    "CORRECTION", "OPS_NOTE", "ERROR",
}

VALID_COMMIT = {"COMMIT", "COMMIT_FLAG", "HOLD"}


def validate_level3_output(obj: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    errors: List[str] = []

    for k in REQUIRED_TOP_LEVEL:
        if k not in obj:
            errors.append(f"Missing key: {k}")

    msg_type = obj.get("msg_type")

    if msg_type not in VALID_MSG_TYPES:
        errors.append(f"Unknown msg_type: {msg_type}")

    if msg_type == "STATUS_UPDATE":
        if not isinstance(obj.get("events"), list):
            errors.append("STATUS_UPDATE but events is not list")
        else:
            for i, ev in enumerate(obj["events"]):
                if "status" not in ev:
                    errors.append(f"Event[{i}] missing status")
    else:
        if obj.get("events") not in ([], None):
            errors.append(f"{msg_type} must have events=[]")

    commit = obj.get("commit_recommendation")
    if commit and commit not in VALID_COMMIT:
        errors.append(f"Invalid commit_recommendation: {commit}")

    conf = obj.get("overall_confidence")
    if not isinstance(conf, (int, float)) or not (0 <= conf <= 1):
        errors.append("overall_confidence must be float in [0, 1]")

    return len(errors) == 0, {"output": obj, "errors": errors}
