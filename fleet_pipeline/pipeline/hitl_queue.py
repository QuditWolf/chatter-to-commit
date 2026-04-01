"""
HITL (Human-in-the-Loop) question queue management.

Creates questions when the pipeline encounters:
- Unknown trucks (truck_id=null — LLM found nothing)
- Unrecognized trucks (LLM mapped to an ID not in the DB)
- Unknown sites (site_id=null, status requires site)
- Unrecognized sites (LLM mapped to a site ID not in the DB)
- Low confidence (overall_confidence < threshold)
- Ambiguous corrections
- Deleted messages

All question texts include:
  - What was parsed / why it was flagged (from LLM reasoning)
  - Clear options for what to reply
  - Explicit note that natural-language answers are accepted
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
    reasoning: str = "",
) -> str:
    qid = _make_question_id()
    why = reasoning or f"'{truck_alias}' did not match any alias in the truck registry"
    question_text = (
        f"Unknown truck '{truck_alias}' — message held for review.\n\n"
        f"Original message: \"{raw_text}\"\n"
        f"Why flagged: {why}\n\n"
        f"Reply with any of:\n"
        f"• Existing truck code  e.g.  TB  (maps '{truck_alias}' as an alias)\n"
        f"• Natural language  e.g.  'that's TB' / 'it's the big Tata'\n"
        f"• Register new truck:  new:TX:Display Name:alias1,alias2\n"
        f"• Full corrected message  e.g.  TB LS SOC"
    )
    db.insert_hitl_question(conn, {
        "question_id": qid,
        "msg_id": msg_id,
        "event_id": event_id,
        "question_type": "UNKNOWN_TRUCK",
        "question_text": question_text,
        "context": {
            "truck_alias": truck_alias,
            "reasoning": why,
            "raw_text": raw_text,
        },
        "simulation_run_id": simulation_run_id,
        "original_wa_message_id": original_wa_message_id,
        "group_jid": group_jid,
    })
    return qid


def create_unrecognized_truck_question(
    conn,
    msg_id: str,
    event_id: Optional[str],
    llm_truck_id: str,
    truck_alias: str,
    reasoning: str,
    raw_text: str,
    simulation_run_id: Optional[str] = None,
    original_wa_message_id: Optional[str] = None,
    group_jid: Optional[str] = None,
) -> str:
    """
    LLM returned a truck_id that is not in the trucks table.
    Ask operator: add as new truck, map to existing, or clarify.
    """
    qid = _make_question_id()
    why = reasoning or f"LLM identified '{truck_alias}' as '{llm_truck_id}' but this ID is not registered"
    question_text = (
        f"Truck '{truck_alias}' was identified as '{llm_truck_id}' "
        f"but '{llm_truck_id}' is not in the registry — message held.\n\n"
        f"Original message: \"{raw_text}\"\n"
        f"Why flagged: {why}\n\n"
        f"Reply with any of:\n"
        f"• YES to register '{llm_truck_id}' as a new truck (alias: '{truck_alias}')\n"
        f"• Existing truck code  e.g.  TB  (maps '{truck_alias}' → TB)\n"
        f"• Natural language  e.g.  'that's TB' / 'add it as truck T05'\n"
        f"• Register with full details:  new:TX:Display Name:alias1,alias2"
    )
    db.insert_hitl_question(conn, {
        "question_id": qid,
        "msg_id": msg_id,
        "event_id": event_id,
        "question_type": "UNRECOGNIZED_TRUCK",
        "question_text": question_text,
        "context": {
            "llm_truck_id": llm_truck_id,
            "truck_alias": truck_alias,
            "reasoning": why,
            "raw_text": raw_text,
        },
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
    reasoning: str = "",
) -> str:
    qid = _make_question_id()
    if site_alias and site_alias.lower() not in ("none", "null", ""):
        what = f"'{site_alias}' not found in site registry"
    else:
        what = "site could not be determined from message"
    why = reasoning or what
    question_text = (
        f"Unknown site — {what} — message held for review.\n\n"
        f"Original message: \"{raw_text}\"\n"
        f"Why flagged: {why}\n\n"
        f"Reply with any of:\n"
        f"• Site code  e.g.  SOC  (maps '{site_alias}' as an alias)\n"
        f"• Natural language  e.g.  'that's SOC' / 'it's the Bagha loading site'\n"
        f"• Full corrected message  e.g.  D LS SOC\n"
        f"• Register new site:  new:SNAME:Display Name:loading:alias"
    )
    db.insert_hitl_question(conn, {
        "question_id": qid,
        "msg_id": msg_id,
        "event_id": event_id,
        "question_type": "UNKNOWN_SITE",
        "question_text": question_text,
        "context": {
            "site_alias": site_alias,
            "reasoning": why,
            "raw_text": raw_text,
        },
        "simulation_run_id": simulation_run_id,
        "original_wa_message_id": original_wa_message_id,
        "group_jid": group_jid,
    })
    return qid


def create_unrecognized_site_question(
    conn,
    msg_id: str,
    event_id: Optional[str],
    llm_site_id: str,
    site_alias: str,
    reasoning: str,
    raw_text: str,
    simulation_run_id: Optional[str] = None,
    original_wa_message_id: Optional[str] = None,
    group_jid: Optional[str] = None,
) -> str:
    """
    LLM returned a site_id that is not in the sites table.
    Ask operator: add as new site, map to existing, or clarify.
    """
    qid = _make_question_id()
    why = reasoning or f"LLM identified '{site_alias}' as '{llm_site_id}' but this ID is not registered"
    question_text = (
        f"Site '{site_alias}' was identified as '{llm_site_id}' "
        f"but '{llm_site_id}' is not in the registry — message held.\n\n"
        f"Original message: \"{raw_text}\"\n"
        f"Why flagged: {why}\n\n"
        f"Reply with any of:\n"
        f"• YES to register '{llm_site_id}' as a new site (loading type, alias: '{site_alias}')\n"
        f"• Existing site code  e.g.  SOC  (maps '{site_alias}' → SOC)\n"
        f"• Natural language  e.g.  'that's SOC' / 'it's the new Bagha pit'\n"
        f"• Full corrected message  e.g.  D LS SOC\n"
        f"• Register with full details:  new:SNAME:Display Name:loading:alias"
    )
    db.insert_hitl_question(conn, {
        "question_id": qid,
        "msg_id": msg_id,
        "event_id": event_id,
        "question_type": "UNRECOGNIZED_SITE",
        "question_text": question_text,
        "context": {
            "llm_site_id": llm_site_id,
            "site_alias": site_alias,
            "reasoning": why,
            "raw_text": raw_text,
        },
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
    reasoning: str = "",
) -> str:
    qid = _make_question_id()
    why = reasoning or "model was not confident about this interpretation"
    question_text = (
        f"Low confidence ({int(confidence * 100)}%) — message flagged for review.\n\n"
        f"Original message: \"{raw_text}\"\n"
        f"Parsed as: {parsed_summary}\n"
        f"Why uncertain: {why}\n\n"
        f"Reply with any of:\n"
        f"• CONFIRM to accept the interpretation above\n"
        f"• Natural language correction  e.g.  'actually it's TB not TA' / 'wrong site, it's SOC'\n"
        f"• The corrected full message  e.g.  TB LS SOC\n"
        f"• Just the missing piece  e.g.  SOC  or  truck TB"
    )
    db.insert_hitl_question(conn, {
        "question_id": qid,
        "msg_id": msg_id,
        "event_id": event_id,
        "question_type": "LOW_CONFIDENCE",
        "question_text": question_text,
        "context": {
            "confidence": confidence,
            "parsed": parsed_summary,
            "reasoning": why,
            "raw_text": raw_text,
        },
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
    reasoning: str = "",
) -> str:
    qid = _make_question_id()
    why = notes or reasoning or "message looks like a correction but what to change is unclear"
    question_text = (
        f"Correction message needs clarification — held for review.\n\n"
        f"Original message: \"{raw_text}\"\n"
        f"Why flagged: {why}\n\n"
        f"Please clarify — reply in any form:\n"
        f"• What was wrong and what it should be  e.g.  'D left not B, B is still at KN4'\n"
        f"• The correct full message  e.g.  TD LS SOC\n"
        f"• Which truck / status / site needs changing and to what"
    )
    db.insert_hitl_question(conn, {
        "question_id": qid,
        "msg_id": msg_id,
        "event_id": None,
        "question_type": "CORRECTION_AMBIGUOUS",
        "question_text": question_text,
        "context": {"raw_text": raw_text, "notes": why},
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
    with db.db_conn(db_path) as conn:
        db.answer_question(conn, question_id, answer, answered_by)


def dismiss_question(question_id: str, db_path: str = DB_PATH) -> None:
    with db.db_conn(db_path) as conn:
        db.dismiss_question(conn, question_id)
