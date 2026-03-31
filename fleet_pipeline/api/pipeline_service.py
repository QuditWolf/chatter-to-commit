"""
Singleton pipeline service for the API.

Holds a single Level3Processor instance (expensive to initialise — loads the LLM)
and exposes a process_raw_text() function for manual message injection.

The processor is initialised lazily on first use so the API starts instantly
even if the LLM endpoint is not yet reachable.
"""
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

log = logging.getLogger(__name__)

from fleet_pipeline.config import DB_PATH, LLM_MOCK
from fleet_pipeline.db import database as db
from fleet_pipeline.pipeline.level1 import parse_timestamp
from fleet_pipeline.pipeline.level2 import Enricher, EnricherConfig
from fleet_pipeline.pipeline.level3 import Level3Processor
from fleet_pipeline.pipeline.committer import Committer
from fleet_pipeline.pipeline.registries import load_truck_registry, load_site_registry, build_vocab_from_db
from fleet_pipeline.pipeline.shift_detector import ShiftDetector

_lock = threading.Lock()
_processor: Optional[Level3Processor] = None
_enricher: Optional[Enricher] = None


def _get_processor() -> Level3Processor:
    global _processor
    if _processor is None:
        with _lock:
            if _processor is None:
                _processor = Level3Processor()
    return _processor


def _get_enricher() -> Enricher:
    global _enricher
    if _enricher is None:
        with _lock:
            if _enricher is None:
                truck_vocab, site_vocab = build_vocab_from_db(DB_PATH)
                _enricher = Enricher(EnricherConfig(
                    truck_vocab=truck_vocab,
                    site_vocab=site_vocab,
                ))
    return _enricher


def invalidate_enricher():
    """Call after registry changes so vocab stays fresh."""
    global _enricher
    _enricher = None


def process_raw_text(
    raw_text: str,
    sender_name: str = "manual",
    sender_id: str = "operator",
    timestamp_iso: Optional[str] = None,
    source: str = "manual",
    operator_clarification: Optional[str] = None,
    wa_message_id: Optional[str] = None,
    group_jid: Optional[str] = None,
) -> dict:
    """
    Run a raw message string through the full Level1→2→3→Commit pipeline.
    Returns the commit summary dict.

    wa_message_id: original WA message ID (Baileys key.id), used as msg_id and
                   stored on HITL questions for reply routing.
    group_jid:     WA group JID to send bot HITL clarification messages to.
    """
    if timestamp_iso is None:
        timestamp_iso = datetime.now(timezone.utc).astimezone(
            __import__("pytz").timezone("Asia/Kolkata")
        ).isoformat()

    # Use provided wa_message_id as msg_id so HITL questions link to the right WA message
    msg_id = wa_message_id or str(uuid4())
    level1_msg = {
        "msg_id": msg_id,
        "source_file": source,
        "timestamp_iso": timestamp_iso,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "raw_text": raw_text,
        "is_edited": False,
        "is_deleted": False,
        "media_type": None,
    }

    # Pre-save the raw message to DB immediately.
    # This ensures it always appears in get_messages_page even if the LLM or
    # committer later throws — preventing the message from silently vanishing.
    # The committer's own INSERT OR IGNORE will be a no-op for the same msg_id.
    with db.db_conn(DB_PATH) as _pre_conn:
        db.insert_raw_message(_pre_conn, level1_msg)

    # Level 2 — enrich
    enricher = _get_enricher()
    level2_msg = enricher.enrich_message(level1_msg)

    # Load registries (fresh each call — fast, cached by SQLite)
    truck_registry = load_truck_registry(DB_PATH)
    site_registry = load_site_registry(DB_PATH)

    # Load recent L3 context for state inference (last 20 committed events)
    with db.db_conn(DB_PATH) as _ctx_conn:
        l3_history = db.get_l3_context(_ctx_conn, limit=20)

    # Level 3 — LLM inference
    llm_error = None
    processor = _get_processor()
    try:
        result = processor.process_message(
            level2_msg, truck_registry, site_registry, l3_history,
            operator_clarification=operator_clarification,
        )
    except Exception as exc:
        llm_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        log.warning("L3 inference failed for msg %s: %s", msg_id, llm_error)
        result = None

    # Level3 can also return msg_type=ERROR without raising (e.g. LLM timeout, bad JSON)
    if result is None or result.get("msg_type") == "ERROR":
        raw_err = (result or {}).get("error", llm_error or "LLM returned no output")
        llm_error = raw_err
        log.warning("L3 returned ERROR for msg %s: %s", msg_id, raw_err)
        result = {
            "msg_type": "STATUS_UPDATE",
            "processing_id": str(uuid4()),
            "raw_message": level1_msg,
            "events": [{
                "event_id": str(uuid4()),
                "truck_id": None,
                "truck_alias": "",
                "status": "UNKNOWN",
                "site_id": None,
                "site_alias": "",
                "confidence": 0.0,
                "reasoning": f"LLM unavailable — {raw_err}",
                "timestamp_effective": timestamp_iso,
                "inferred": False,
            }],
            "overall_confidence": 0.0,
            "commit_recommendation": "HOLD",
        }

    # Inject shift_id via shift detector
    import sqlite3 as _sqlite3
    _sd_conn = _sqlite3.connect(DB_PATH)
    _sd_conn.row_factory = _sqlite3.Row
    _sd_conn.execute("PRAGMA journal_mode=WAL")
    _sd_conn.execute("PRAGMA foreign_keys=ON")
    sd = ShiftDetector(_sd_conn)
    shift_id = sd.process_message(raw_text, timestamp_iso)
    _sd_conn.commit()
    _sd_conn.close()

    if not result.get("shift_id") and shift_id:
        result["shift_id"] = shift_id

    # Commit
    committer = Committer(db_path=DB_PATH, wa_message_id=wa_message_id, group_jid=group_jid)
    summary = committer.commit(result)
    summary["msg_id"] = msg_id
    summary["raw_text"] = raw_text
    summary["timestamp_iso"] = timestamp_iso
    summary["shift_id"] = shift_id
    if llm_error:
        summary["llm_error"] = llm_error
        summary["unmapped"] = True

    # WA HITL notifications: if questions were created and we know the group, send bot replies
    if summary.get("hitl_created", 0) > 0 and group_jid:
        try:
            from fleet_pipeline.pipeline.wa_notifier import notify_hitl_questions
            # Fetch the newly created open questions for this message
            with db.db_conn(DB_PATH) as _hconn:
                q_rows = _hconn.execute(
                    """SELECT question_id, question_type, context, original_wa_message_id
                       FROM hitl_queue
                       WHERE msg_id=? AND status='OPEN' AND bot_wa_message_id IS NULL""",
                    (msg_id,),
                ).fetchall()
            questions = [dict(r) for r in q_rows]
            # Attach raw_text into context for formatting (context already has it, but be safe)
            for q in questions:
                q["raw_text"] = raw_text
                q["original_wa_message_id"] = q.get("original_wa_message_id") or wa_message_id
            notify_hitl_questions(questions, group_jid, DB_PATH)
        except Exception as _exc:
            log.warning("WA HITL notification failed: %s", _exc)

    return summary
