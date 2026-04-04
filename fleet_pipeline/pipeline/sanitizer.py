"""
LLM Output Sanitizer — defensive layer between LLM and the database.

Protects against:
  1. Prompt injection / jailbreak attempts in LLM output
  2. SQL injection via event fields
  3. Data pollution (hallucinated trucks/sites/statuses)
  4. Schema violations (missing/wrong-type fields)
  5. Confidence manipulation (LLM inflating its own confidence)
  6. Excessive output (truncated or runaway generation)

All sanitization is logged to the audit table with full raw output preserved
for forensic recovery.

Usage:
    sanitized, issues = sanitize_llm_output(raw_llm_output, level2_msg, db_path)
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── Injection patterns ────────────────────────────────────────────────────────

# Patterns that indicate the LLM is trying to inject instructions or break out
# of its role. These should never appear in valid fleet-event JSON output.
_INJECTION_PATTERNS = [
    # Prompt injection / role break
    re.compile(r"(ignore\s+(all\s+)?previous|override\s+(the\s+)?system)", re.I),
    re.compile(r"(you\s+are\s+now|act\s+as|pretend\s+to\s+be|role\s*:\s*)", re.I),
    re.compile(r"(new\s+instruction|system\s*:\s*|admin\s*:\s*)", re.I),
    # SQL injection attempts
    re.compile(r"(DROP\s+TABLE|DELETE\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET)", re.I),
    re.compile(r"(;\s*--|;\s*/\*|UNION\s+SELECT)", re.I),
    # Schema manipulation attempts
    re.compile(r"(ALTER\s+TABLE|CREATE\s+TABLE|PRAGMA\s+)", re.I),
    # Data exfiltration attempts
    re.compile(r"(SELECT\s+\*\s+FROM|dump\s+all|export\s+data)", re.I),
]

# ── Field constraints ─────────────────────────────────────────────────────────

# Maximum lengths for text fields — prevents runaway output
_MAX_FIELD_LENGTHS = {
    "reasoning": 200,
    "notes": 500,
    "truck_alias": 50,
    "site_alias": 50,
    "material": 100,
    "query": 500,
}

# Allowed status values — nothing else gets through
_ALLOWED_STATUSES = {"ENTER", "LS", "LO", "LEFT", "US", "UO", "UNKNOWN"}

# Allowed msg_type values
_ALLOWED_MSG_TYPES = {
    "STATUS_UPDATE",
    "TALLY_UPDATE",
    "QUERY",
    "NOISE",
    "CORRECTION",
    "OPS_NOTE",
    "ERROR",
    "SHIFT_SIGNAL",
}

# Allowed commit recommendations
_ALLOWED_COMMIT = {"COMMIT", "COMMIT_FLAG", "HOLD"}

# Confidence must be in this range — LLM cannot self-inflate beyond 1.0
_CONFIDENCE_MIN = 0.0
_CONFIDENCE_MAX = 1.0

# Maximum number of events in a single response
_MAX_EVENTS = 10

# ── Dangerous characters in event fields ──────────────────────────────────────

# Characters that should never appear in truck_id, site_id, status, etc.
# These would indicate either LLM hallucination or injection attempt.
_DANGEROUS_CHARS = re.compile(r"[;'\"]|/\*|\*/|--|\\x|\\u00")


class SanitizationIssue:
    """A single sanitization finding."""

    def __init__(self, severity: str, field: str, message: str, raw_value: str = ""):
        self.severity = severity  # "block" | "warn" | "info"
        self.field = field
        self.message = message
        self.raw_value = raw_value[:200]  # truncate for logging

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "field": self.field,
            "message": self.message,
            "raw_value": self.raw_value,
        }


def sanitize_llm_output(
    parsed_output: Dict[str, Any],
    raw_llm_text: str = "",
    level2_msg: Optional[Dict] = None,
    db_path: str = "",
) -> Tuple[Dict[str, Any], List[SanitizationIssue]]:
    """
    Sanitize and validate LLM output before it reaches the committer.

    Returns:
        (sanitized_output, list_of_issues)

    Issues with severity="block" mean the output should be rejected entirely
    and treated as an LLM error.
    """
    issues: List[SanitizationIssue] = []

    # ── 1. Check for injection patterns in raw LLM text ───────────────────
    if raw_llm_text:
        for pat in _INJECTION_PATTERNS:
            if pat.search(raw_llm_text):
                issues.append(
                    SanitizationIssue(
                        "block",
                        "raw_output",
                        f"Possible injection pattern detected: {pat.pattern[:40]}",
                        raw_llm_text[:200],
                    )
                )
                # Return error output — do not let this through
                return _error_result(
                    "Sanitizer blocked: possible injection attempt"
                ), issues

    # ── 2. Validate top-level structure ───────────────────────────────────
    if not isinstance(parsed_output, dict):
        issues.append(
            SanitizationIssue(
                "block",
                "root",
                "LLM output is not a JSON object",
                str(parsed_output)[:200],
            )
        )
        return _error_result("Sanitizer blocked: output is not a JSON object"), issues

    # ── 3. Validate msg_type ──────────────────────────────────────────────
    msg_type = parsed_output.get("msg_type", "")
    if msg_type not in _ALLOWED_MSG_TYPES:
        issues.append(
            SanitizationIssue(
                "block",
                "msg_type",
                f"Invalid msg_type: {msg_type!r}",
                msg_type,
            )
        )
        return _error_result(
            f"Sanitizer blocked: invalid msg_type {msg_type!r}"
        ), issues

    # ── 4. Validate commit_recommendation ─────────────────────────────────
    commit_rec = parsed_output.get("commit_recommendation", "")
    if commit_rec and commit_rec not in _ALLOWED_COMMIT:
        issues.append(
            SanitizationIssue(
                "warn",
                "commit_recommendation",
                f"Invalid commit_recommendation: {commit_rec!r} — defaulting to HOLD",
                commit_rec,
            )
        )
        parsed_output = {**parsed_output, "commit_recommendation": "HOLD"}

    # ── 5. Validate overall_confidence ────────────────────────────────────
    conf = parsed_output.get("overall_confidence")
    if isinstance(conf, (int, float)):
        if conf < _CONFIDENCE_MIN or conf > _CONFIDENCE_MAX:
            issues.append(
                SanitizationIssue(
                    "warn",
                    "overall_confidence",
                    f"Confidence {conf} out of range — clamping to [{_CONFIDENCE_MIN}, {_CONFIDENCE_MAX}]",
                    str(conf),
                )
            )
            parsed_output = {
                **parsed_output,
                "overall_confidence": max(_CONFIDENCE_MIN, min(_CONFIDENCE_MAX, conf)),
            }
    elif conf is not None:
        issues.append(
            SanitizationIssue(
                "warn",
                "overall_confidence",
                f"Confidence is not a number: {conf!r} — defaulting to 0.5",
                str(conf),
            )
        )
        parsed_output = {**parsed_output, "overall_confidence": 0.5}

    # ── 6. Validate events array ──────────────────────────────────────────
    events = parsed_output.get("events", [])
    if not isinstance(events, list):
        issues.append(
            SanitizationIssue(
                "block",
                "events",
                f"events is not a list: {type(events).__name__}",
                str(events)[:200],
            )
        )
        return _error_result("Sanitizer blocked: events is not a list"), issues

    if len(events) > _MAX_EVENTS:
        issues.append(
            SanitizationIssue(
                "warn",
                "events",
                f"Too many events ({len(events)}) — truncating to {_MAX_EVENTS}",
                str(len(events)),
            )
        )
        events = events[:_MAX_EVENTS]
        parsed_output = {**parsed_output, "events": events}

    sanitized_events = []
    for i, ev in enumerate(events):
        ev_issues, clean_ev = _sanitize_event(ev, i, msg_type)
        issues.extend(ev_issues)
        if clean_ev is not None:
            sanitized_events.append(clean_ev)

    parsed_output = {**parsed_output, "events": sanitized_events}

    # ── 7. Validate text fields for length and dangerous chars ────────────
    for field_name, max_len in _MAX_FIELD_LENGTHS.items():
        val = parsed_output.get(field_name)
        if isinstance(val, str):
            if len(val) > max_len:
                issues.append(
                    SanitizationIssue(
                        "warn",
                        field_name,
                        f"Field too long ({len(val)} > {max_len}) — truncated",
                        val[:100],
                    )
                )
                parsed_output = {**parsed_output, field_name: val[:max_len]}
            if _DANGEROUS_CHARS.search(val):
                issues.append(
                    SanitizationIssue(
                        "warn",
                        field_name,
                        f"Dangerous characters in {field_name} — stripped",
                        val[:100],
                    )
                )
                clean_val = _DANGEROUS_CHARS.sub("", val)
                parsed_output = {**parsed_output, field_name: clean_val}

    # ── 8. Validate tally data (if present) ───────────────────────────────
    tally = parsed_output.get("tally")
    if tally is not None and msg_type == "TALLY_UPDATE":
        if not isinstance(tally, (dict, list)):
            issues.append(
                SanitizationIssue(
                    "warn",
                    "tally",
                    f"Tally is not a dict/list: {type(tally).__name__}",
                    str(tally)[:200],
                )
            )
            parsed_output = {**parsed_output, "tally": {}}

    # ── 9. Log all issues ─────────────────────────────────────────────────
    if issues:
        blocked = any(i.severity == "block" for i in issues)
        log.warning(
            "LLM sanitization: %s — %d issue(s): %s",
            "BLOCKED" if blocked else "WARNINGS",
            len(issues),
            "; ".join(f"{i.severity}:{i.field}:{i.message}" for i in issues),
        )

    return parsed_output, issues


def _sanitize_event(
    ev: Any, index: int, msg_type: str
) -> Tuple[List[SanitizationIssue], Optional[Dict]]:
    """Sanitize a single event dict. Returns (issues, clean_event_or_None)."""
    issues: List[SanitizationIssue] = []

    if not isinstance(ev, dict):
        issues.append(
            SanitizationIssue(
                "warn",
                f"events[{index}]",
                f"Event is not a dict: {type(ev).__name__}",
                str(ev)[:200],
            )
        )
        return issues, None

    clean_ev = {}

    # ── truck_id ──────────────────────────────────────────────────────────
    truck_id = ev.get("truck_id")
    if truck_id is not None:
        if not isinstance(truck_id, str):
            issues.append(
                SanitizationIssue(
                    "warn",
                    f"events[{index}].truck_id",
                    f"truck_id is not a string: {type(truck_id).__name__}",
                    str(truck_id)[:100],
                )
            )
            truck_id = None
        elif _DANGEROUS_CHARS.search(truck_id):
            issues.append(
                SanitizationIssue(
                    "warn",
                    f"events[{index}].truck_id",
                    "Dangerous characters in truck_id — nulled",
                    truck_id[:100],
                )
            )
            truck_id = None
        elif len(truck_id) > 50:
            issues.append(
                SanitizationIssue(
                    "warn",
                    f"events[{index}].truck_id",
                    f"truck_id too long ({len(truck_id)}) — nulled",
                    truck_id[:100],
                )
            )
            truck_id = None
    clean_ev["truck_id"] = truck_id

    # ── truck_alias ───────────────────────────────────────────────────────
    truck_alias = ev.get("truck_alias", "")
    if isinstance(truck_alias, str):
        if len(truck_alias) > _MAX_FIELD_LENGTHS["truck_alias"]:
            truck_alias = truck_alias[: _MAX_FIELD_LENGTHS["truck_alias"]]
        truck_alias = _DANGEROUS_CHARS.sub("", truck_alias)
    else:
        truck_alias = ""
    clean_ev["truck_alias"] = truck_alias

    # ── status ────────────────────────────────────────────────────────────
    status = ev.get("status")
    if status not in _ALLOWED_STATUSES:
        issues.append(
            SanitizationIssue(
                "warn",
                f"events[{index}].status",
                f"Invalid status: {status!r} — setting to UNKNOWN",
                str(status)[:50],
            )
        )
        status = "UNKNOWN"
    clean_ev["status"] = status

    # ── site_id ───────────────────────────────────────────────────────────
    site_id = ev.get("site_id")
    if site_id is not None:
        if not isinstance(site_id, str):
            issues.append(
                SanitizationIssue(
                    "warn",
                    f"events[{index}].site_id",
                    f"site_id is not a string: {type(site_id).__name__}",
                    str(site_id)[:100],
                )
            )
            site_id = None
        elif _DANGEROUS_CHARS.search(site_id):
            issues.append(
                SanitizationIssue(
                    "warn",
                    f"events[{index}].site_id",
                    "Dangerous characters in site_id — nulled",
                    site_id[:100],
                )
            )
            site_id = None
        elif len(site_id) > 50:
            issues.append(
                SanitizationIssue(
                    "warn",
                    f"events[{index}].site_id",
                    f"site_id too long ({len(site_id)}) — nulled",
                    site_id[:100],
                )
            )
            site_id = None
    clean_ev["site_id"] = site_id

    # ── site_alias ────────────────────────────────────────────────────────
    site_alias = ev.get("site_alias", "")
    if isinstance(site_alias, str):
        if len(site_alias) > _MAX_FIELD_LENGTHS["site_alias"]:
            site_alias = site_alias[: _MAX_FIELD_LENGTHS["site_alias"]]
        site_alias = _DANGEROUS_CHARS.sub("", site_alias)
    else:
        site_alias = ""
    clean_ev["site_alias"] = site_alias

    # ── material ──────────────────────────────────────────────────────────
    material = ev.get("material")
    if isinstance(material, str) and len(material) > _MAX_FIELD_LENGTHS["material"]:
        material = material[: _MAX_FIELD_LENGTHS["material"]]
    clean_ev["material"] = material

    # ── confidence ────────────────────────────────────────────────────────
    ev_conf = ev.get("confidence")
    if isinstance(ev_conf, (int, float)):
        ev_conf = max(_CONFIDENCE_MIN, min(_CONFIDENCE_MAX, ev_conf))
    else:
        ev_conf = 0.5
    clean_ev["confidence"] = ev_conf

    # ── inferred ──────────────────────────────────────────────────────────
    clean_ev["inferred"] = bool(ev.get("inferred", False))

    # ── reasoning ─────────────────────────────────────────────────────────
    reasoning = ev.get("reasoning", "")
    if isinstance(reasoning, str):
        if len(reasoning) > _MAX_FIELD_LENGTHS["reasoning"]:
            reasoning = reasoning[: _MAX_FIELD_LENGTHS["reasoning"]]
            issues.append(
                SanitizationIssue(
                    "info",
                    f"events[{index}].reasoning",
                    "Reasoning truncated",
                    reasoning[:100],
                )
            )
        reasoning = _DANGEROUS_CHARS.sub("", reasoning)
    else:
        reasoning = ""
    clean_ev["reasoning"] = reasoning

    # ── timestamp_effective ───────────────────────────────────────────────
    ts = ev.get("timestamp_effective", "")
    if not isinstance(ts, str):
        ts = ""
    clean_ev["timestamp_effective"] = ts

    # ── event_id (LLM-generated, we ignore it — committer generates its own)
    # Do NOT use LLM-provided event_id — committer always generates fresh UUID

    return issues, clean_ev


def _error_result(message: str) -> Dict[str, Any]:
    """Return a safe error dict that the committer can handle."""
    return {
        "msg_type": "ERROR",
        "events": [],
        "tally": None,
        "query": None,
        "notes": message,
        "overall_confidence": 0.0,
        "shift_id": None,
        "commit_recommendation": "HOLD",
    }
