"""
API routes for the HITL (Human-in-the-Loop) question queue.
GET  /hitl/queue           — list open questions
POST /hitl/answer          — submit an answer; re-processes through LLM when clarification given
POST /hitl/dismiss/{id}    — dismiss a question
"""
import asyncio
import json
import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db import database as db
from fleet_pipeline.pipeline.hitl_queue import answer_question, dismiss_question

log = logging.getLogger(__name__)
router = APIRouter(prefix="/hitl", tags=["hitl"])

# Known site / truck code patterns (short uppercase codes — not free text)
def _looks_like_code(s: str) -> bool:
    """Return True if s looks like a truck/site code rather than free text."""
    s = s.strip()
    return len(s) <= 10 and s.replace("_","").replace("-","").isalnum() and s == s.upper()


class AnswerRequest(BaseModel):
    question_id: str
    answer: str
    answered_by: Optional[str] = "human"


@router.get("/queue")
def get_queue(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List open HITL questions with pagination."""
    with db.db_conn(DB_PATH) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM hitl_queue WHERE status='OPEN' AND question_type != 'DELETED_MESSAGE'"
        ).fetchone()[0]
        type_counts = dict(conn.execute(
            "SELECT question_type, COUNT(*) FROM hitl_queue WHERE status='OPEN' AND question_type != 'DELETED_MESSAGE' GROUP BY question_type"
        ).fetchall())
        questions = db.get_open_questions(conn, limit=limit, offset=offset)
    return {
        "questions": questions,
        "count": total,
        "offset": offset,
        "limit": limit,
        "type_counts": type_counts,
    }


@router.post("/answer")
async def submit_answer(req: AnswerRequest, background_tasks: BackgroundTasks):
    """
    Submit a human answer to a HITL question.

    Behaviour:
    - UNKNOWN_TRUCK/SITE + code answer → direct DB update (fast path)
    - UNKNOWN_TRUCK/SITE + free-text OR any LOW_CONFIDENCE/CORRECTION_AMBIGUOUS
      → re-process original message through LLM with operator clarification
    - LOW_CONFIDENCE + "CONFIRM" → force-commit the held event
    """
    with db.db_conn(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT * FROM hitl_queue WHERE question_id=?", (req.question_id,)
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Question not found")

        q = dict(rows[0])
        if q["status"] != "OPEN":
            raise HTTPException(status_code=400, detail=f"Question already {q['status']}")

        q_type = q["question_type"]
        context = q.get("context", "{}")
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except Exception:
                context = {}

        raw_text = context.get("raw_text", "")
        event_id = q.get("event_id")
        msg_id   = q.get("msg_id")
        answer   = req.answer.strip()
        reprocess = False

        # LOW_CONFIDENCE: CONFIRM = force commit; anything else = re-process
        if q_type == "LOW_CONFIDENCE":
            if answer.upper() == "CONFIRM":
                if event_id:
                    conn.execute(
                        "UPDATE events SET commit_status='COMMITTED', commit_path='manual', corrected=1 WHERE event_id=?",
                        (event_id,)
                    )
            else:
                reprocess = True

        # UNKNOWN_TRUCK: code → direct; free text → re-process
        elif q_type == "UNKNOWN_TRUCK":
            if answer.lower().startswith("new:") or _looks_like_code(answer):
                _handle_unknown_truck_answer(conn, answer, context, event_id)
            else:
                reprocess = True

        # UNKNOWN_SITE: code or new: → direct; free text → re-process
        elif q_type == "UNKNOWN_SITE":
            if answer.lower().startswith("new:") or _looks_like_code(answer):
                _handle_unknown_site_answer(conn, answer, context, event_id)
            else:
                reprocess = True

        # CORRECTION_AMBIGUOUS: always re-process
        elif q_type == "CORRECTION_AMBIGUOUS":
            reprocess = True

        # Record the answer
        db.answer_question(conn, req.question_id, answer, req.answered_by)

        # If re-processing: delete original held event so it gets replaced
        if reprocess and event_id:
            conn.execute(
                "UPDATE events SET commit_status='DELETED' WHERE event_id=? AND commit_status IN ('HELD','FLAGGED')",
                (event_id,)
            )

    if reprocess and raw_text:
        # Get original message metadata
        with db.db_conn(DB_PATH) as conn2:
            msg_row = conn2.execute(
                "SELECT sender_name, sender_id, timestamp_iso FROM raw_messages WHERE msg_id=?",
                (msg_id,)
            ).fetchone() if msg_id else None

        sender_name = (msg_row["sender_name"] if msg_row else None) or "operator"
        sender_id   = (msg_row["sender_id"]   if msg_row else None) or "operator"
        timestamp   = (msg_row["timestamp_iso"] if msg_row else None)

        background_tasks.add_task(
            _reprocess_with_clarification,
            raw_text=raw_text,
            operator_clarification=answer,
            sender_name=sender_name,
            sender_id=sender_id,
            timestamp_iso=timestamp,
        )
        return {"status": "reprocessing", "question_id": req.question_id}

    from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache
    invalidate_kpi_cache()
    return {"status": "ok", "question_id": req.question_id}


async def _reprocess_with_clarification(
    raw_text: str,
    operator_clarification: str,
    sender_name: str,
    sender_id: str,
    timestamp_iso: Optional[str],
):
    """Re-run the message through the full LLM pipeline with operator's clarification."""
    from functools import partial
    from fleet_pipeline.api.pipeline_service import process_raw_text
    from fleet_pipeline.api.routes.ingest import _broadcast_summary
    from fleet_pipeline.api.main import ws_manager

    try:
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(
            None,
            partial(
                process_raw_text,
                raw_text=raw_text,
                sender_name=sender_name,
                sender_id=sender_id,
                timestamp_iso=timestamp_iso,
                source="hitl_reprocess",
                operator_clarification=operator_clarification,
            ),
        )
        await _broadcast_summary(summary, "hitl_reprocess")
    except Exception as exc:
        log.error("HITL reprocess error: %s", exc)
        await ws_manager.broadcast("commit_error", {"raw_text": raw_text, "error": str(exc)[:200]})


@router.post("/dismiss/{question_id}")
@router.post("/{question_id}/skip")
def dismiss(question_id: str):
    """Dismiss / skip a HITL question without answering."""
    dismiss_question(question_id, DB_PATH)
    return {"status": "dismissed", "question_id": question_id}


@router.post("/{question_id}/answer")
async def answer_by_id(question_id: str, req: AnswerRequest, background_tasks: BackgroundTasks):
    """Answer a HITL question by path param (spec endpoint)."""
    req.question_id = question_id
    return await submit_answer(req, background_tasks)


def _handle_unknown_truck_answer(conn, answer: str, context: dict, event_id: Optional[str]):
    """
    Parse answer and update trucks + held event.
    Formats:
      "TB" → add alias to existing truck TB
      "new:TX:Truck X:alias1,alias2" → create new truck
    """
    answer = answer.strip()
    if answer.lower().startswith("new:"):
        parts = answer.split(":", 3)
        if len(parts) >= 4:
            _, truck_id, display_name, aliases_str = parts
            aliases = [a.strip() for a in aliases_str.split(",") if a.strip()]
            try:
                db.create_truck(conn, truck_id.strip(), display_name.strip(), aliases)
            except Exception:
                pass
    else:
        # answer is existing truck_id; add alias from context
        truck_id = answer.strip()
        truck_alias = context.get("truck_alias", "")
        if truck_alias:
            try:
                db.add_truck_alias(conn, truck_id, truck_alias)
            except Exception:
                pass

    # Update the held event with resolved truck_id if event_id present
    if event_id and answer:
        resolved_id = answer.split(":")[1] if answer.lower().startswith("new:") else answer.strip()
        conn.execute(
            "UPDATE events SET truck_id=?, commit_status='COMMITTED' WHERE event_id=? AND commit_status='HELD'",
            (resolved_id, event_id),
        )


def _handle_unknown_site_answer(conn, answer: str, context: dict, event_id: Optional[str]):
    """
    Parse answer and update sites + held event.
    Formats:
      "BG" → add alias to existing site BG
      "new:SNAME:Display Name:loading:alias" → create new site
    """
    answer = answer.strip()
    if answer.lower().startswith("new:"):
        parts = answer.split(":", 4)
        if len(parts) >= 5:
            _, site_id, display_name, site_type, aliases_str = parts
            aliases = [a.strip() for a in aliases_str.split(",") if a.strip()]
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO sites (site_id, display_name, aliases, site_type) VALUES (?,?,?,?)",
                    (site_id.strip(), display_name.strip(),
                     json.dumps(aliases), site_type.strip()),
                )
            except Exception:
                pass
    else:
        site_id = answer.strip()
        site_alias = context.get("site_alias", "")
        if site_alias:
            try:
                db.add_site_alias(conn, site_id, site_alias)
            except Exception:
                pass

    if event_id and answer:
        resolved_id = answer.split(":")[1] if answer.lower().startswith("new:") else answer.strip()
        conn.execute(
            "UPDATE events SET site_id=?, commit_status='COMMITTED' WHERE event_id=? AND commit_status IN ('HELD','FLAGGED')",
            (resolved_id, event_id),
        )
