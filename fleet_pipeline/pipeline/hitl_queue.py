"""
HITL (Human-in-the-Loop) question queue management.

Creates questions when the pipeline encounters:
- Unknown trucks (truck_id=null)
- Unknown sites (site_id=null, status requires site)
- Low confidence (overall_confidence < threshold)
- Ambiguous corrections
- Deleted messages

Exposes get_open_questions() and answer_question().
"""
import json
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db import database as db


def _make_question_id() -> str:
    return str(uuid4())


def create_unknown_truck_question(
    conn,
    msg_id: str,
    event_id: Optional[str],
    truck_alias: str,
    raw_text: str,
    simulation_run_id: Optional[str] = None,
    original_wa_message_id: Optional[str] = None,
    group_jid: Optional[str] = None,
) -> str:
    qid = _make_question_id()
    question_text = (
        f"Unknown truck '{truck_alias}' — does not match any registered truck.\n\n"
        f"Original message: \"{raw_text}\"\n\n"
        f"Enter the truck code (e.g. TB), or to register a new truck: new:TX:Display Name:alias"
    )
    db.insert_hitl_question(conn, {
        "question_id": qid,
        "msg_id": msg_id,
        "event_id": event_id,
        "question_type": "UNKNOWN_TRUCK",
        "question_text": question_text,
        "context": {"truck_alias": truck_alias, "raw_text": raw_text},
        "simulation_run_id": simulation_run_id,
        "original_wa_message_id": original_wa_message_id,
        "group_jid": group_jid,
    })
    return qid


def create_unknown_site_question(
    conn,
    msg_id: str,
    event_id: Optional[str],
    site_alias: str,
    raw_text: str,
    simulation_run_id: Optional[str] = None,
    original_wa_message_id: Optional[str] = None,
    group_jid: Optional[str] = None,
) -> str:
    qid = _make_question_id()
    if site_alias and site_alias.lower() not in ("none", "null", ""):
        site_desc = f"'{site_alias}' — does not match any registered site"
    else:
        site_desc = "could not be determined from the message"
    question_text = (
        f"Site {site_desc}.\n\n"
        f"Original message: \"{raw_text}\"\n\n"
        f"Type the site code (e.g. SOC), OR type the full corrected message (e.g. \"D left SOC\"), "
        f"OR register a new site: new:SNAME:Display Name:loading:alias"
    )
    db.insert_hitl_question(conn, {
        "question_id": qid,
        "msg_id": msg_id,
        "event_id": event_id,
        "question_type": "UNKNOWN_SITE",
        "question_text": question_text,
        "context": {"site_alias": site_alias, "raw_text": raw_text},
        "simulation_run_id": simulation_run_id,
        "original_wa_message_id": original_wa_message_id,
        "group_jid": group_jid,
    })
    return qid


def create_low_confidence_question(
    conn,
    msg_id: str,
    event_id: Optional[str],
    confidence: float,
    parsed_summary: str,
    raw_text: str,
    simulation_run_id: Optional[str] = None,
    original_wa_message_id: Optional[str] = None,
    group_jid: Optional[str] = None,
) -> str:
    qid = _make_question_id()
    question_text = (
        f"Low confidence ({int(confidence * 100)}%) — please verify this interpretation.\n\n"
        f"Original message: \"{raw_text}\"\n\n"
        f"Parsed as: {parsed_summary}\n\n"
        f"Type CONFIRM to accept, or enter a correction."
    )
    db.insert_hitl_question(conn, {
        "question_id": qid,
        "msg_id": msg_id,
        "event_id": event_id,
        "question_type": "LOW_CONFIDENCE",
        "question_text": question_text,
        "context": {"confidence": confidence, "raw_text": raw_text, "parsed": parsed_summary},
        "simulation_run_id": simulation_run_id,
        "original_wa_message_id": original_wa_message_id,
        "group_jid": group_jid,
    })
    return qid


def create_correction_ambiguous_question(
    conn,
    msg_id: str,
    raw_text: str,
    notes: str,
    simulation_run_id: Optional[str] = None,
    original_wa_message_id: Optional[str] = None,
    group_jid: Optional[str] = None,
) -> str:
    qid = _make_question_id()
    question_text = (
        f"This message appears to be a correction but it is unclear what it corrects.\n\n"
        f"Original message: \"{raw_text}\"\n\n"
        f"Please clarify: which truck/status/site is being corrected, and what should it be changed to?"
    )
    db.insert_hitl_question(conn, {
        "question_id": qid,
        "msg_id": msg_id,
        "event_id": None,
        "question_type": "CORRECTION_AMBIGUOUS",
        "question_text": question_text,
        "context": {"raw_text": raw_text, "notes": notes},
        "simulation_run_id": simulation_run_id,
        "original_wa_message_id": original_wa_message_id,
        "group_jid": group_jid,
    })
    return qid


def create_deleted_message_question(
    conn,
    msg_id: str,
    simulation_run_id: Optional[str] = None,
) -> str:
    qid = _make_question_id()
    question_text = (
        "A message was deleted (original text is gone). "
        "Were there any truck status updates in this message that need to be recorded?"
    )
    db.insert_hitl_question(conn, {
        "question_id": qid,
        "msg_id": msg_id,
        "event_id": None,
        "question_type": "DELETED_MESSAGE",
        "question_text": question_text,
        "context": {"msg_id": msg_id},
        "simulation_run_id": simulation_run_id,
    })
    return qid


def get_open_questions(db_path: str = DB_PATH) -> List[Dict]:
    with db.db_conn(db_path) as conn:
        return db.get_open_questions(conn)


def answer_question(
    question_id: str,
    answer: str,
    answered_by: str = "human",
    db_path: str = DB_PATH,
) -> None:
    """
    Record a human answer to a HITL question.
    Downstream logic (re-processing held events) is handled by the API routes.
    """
    with db.db_conn(db_path) as conn:
        db.answer_question(conn, question_id, answer, answered_by)


def dismiss_question(question_id: str, db_path: str = DB_PATH) -> None:
    with db.db_conn(db_path) as conn:
        db.dismiss_question(conn, question_id)
