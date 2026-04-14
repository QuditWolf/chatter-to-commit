"""
API routes for the HITL (Human-in-the-Loop) question queue.
GET  /hitl/queue           — list open questions
POST /hitl/answer          — submit an answer; re-processes through LLM when clarification given
POST /hitl/dismiss/{id}    — dismiss a question

Answer handling philosophy:
  Natural language is always accepted and re-processed through the LLM with the
  operator's text injected as operator_clarification.

  Fast paths (no re-processing needed):
    - Short code  e.g.  "TB"         → map alias to existing truck/site
    - "new:..."                       → register new truck/site
    - "YES" / affirmative             → for UNRECOGNIZED_* types: register the LLM-proposed ID
    - "CONFIRM"                       → for LOW_CONFIDENCE: force-commit the held event

  All other answers → re-process original message through LLM with clarification injected.
"""
import asyncio
import json
import logging
import re
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db import database as db
from fleet_pipeline.pipeline.hitl_queue import answer_question, dismiss_question

log = logging.getLogger(__name__)
router = APIRouter(prefix="/hitl", tags=["hitl"])


# ---------------------------------------------------------------------------
# Answer parsing helpers
# ---------------------------------------------------------------------------

def _looks_like_code(s: str) -> bool:
    """Return True if s looks like a bare truck/site code (no spaces, short, alphanumeric)."""
    s = s.strip()
    return len(s) <= 10 and s.replace("_", "").replace("-", "").isalnum()


_AFFIRMATIVE = frozenset({
    "yes", "yes.", "yes!", "yep", "haan", "ha", "correct", "ok", "okay",
    "confirm", "confirmed", "right", "sure", "add it", "add", "register it",
    "yes add it", "yes please",
})

def _is_affirmative(s: str) -> bool:
    return s.strip().lower() in _AFFIRMATIVE


_CODE_IN_TEXT_RE = re.compile(
    r"""(?:
        it'?s\s+|that'?s\s+|is\s+|use\s+|
        truck\s+|site\s+|map\s+(?:it\s+)?to\s+|
        it\s+is\s+|add\s+(?:it\s+)?as\s+|register\s+as\s+
    )([A-Z][A-Z0-9_\-]{1,9})\b""",
    re.IGNORECASE | re.VERBOSE,
)

def _extract_code_from_answer(s: str) -> Optional[str]:
    """
    Try to pull a truck/site code from natural language.
    Returns uppercase code string or None.

    Handles:  'that's TB'  'it's SOC'  'use TB'  'truck TB'  'map it to SOC'
              'add it as T05'  'TB'  'SOC'
    """
    stripped = s.strip()
    if _looks_like_code(stripped):
        return stripped.upper()
    m = _CODE_IN_TEXT_RE.search(stripped)
    if m:
        return m.group(1).upper()
    return None


class AnswerRequest(BaseModel):
    question_id: Optional[str] = None
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

    Fast paths (no LLM re-processing):
      UNKNOWN_TRUCK / UNRECOGNIZED_TRUCK:
        "new:..."   → register new truck, commit held event
        code / "that's TB" / "it's TB"  → map alias to existing truck, commit
        "YES" (UNRECOGNIZED only) → register the LLM-proposed ID as new truck
      UNKNOWN_SITE / UNRECOGNIZED_SITE: same pattern
      LOW_CONFIDENCE + "CONFIRM"  → force-commit held event

    Everything else (natural language, corrections, full sentences) →
      re-process original message through LLM with answer as operator_clarification.
    """
    if not req.question_id:
        raise HTTPException(status_code=422, detail="question_id is required")
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

        # ── LOW_CONFIDENCE ────────────────────────────────────────────────────
        if q_type == "LOW_CONFIDENCE":
            if answer.upper() == "CONFIRM":
                if event_id:
                    conn.execute(
                        "UPDATE events SET commit_status='COMMITTED', commit_path='manual', corrected=1 WHERE event_id=?",
                        (event_id,)
                    )
            else:
                reprocess = True

        # ── UNKNOWN_TRUCK ─────────────────────────────────────────────────────
        elif q_type == "UNKNOWN_TRUCK":
            extracted = _extract_code_from_answer(answer)
            if answer.lower().startswith("new:") or extracted:
                _handle_unknown_truck_answer(conn, extracted or answer, context, event_id)
            else:
                reprocess = True

        # ── UNRECOGNIZED_TRUCK (LLM proposed an ID not in DB) ─────────────────
        elif q_type == "UNRECOGNIZED_TRUCK":
            llm_truck_id = context.get("llm_truck_id", "")
            truck_alias  = context.get("truck_alias", "")
            if _is_affirmative(answer) and llm_truck_id:
                # Operator confirms the LLM's suggested ID — register it
                _register_new_truck(conn, llm_truck_id, llm_truck_id, [truck_alias], event_id)
            elif answer.lower().startswith("new:"):
                _handle_unknown_truck_answer(conn, answer, context, event_id)
            else:
                extracted = _extract_code_from_answer(answer)
                if extracted:
                    # Map the alias to an existing truck code
                    _handle_unknown_truck_answer(conn, extracted, context, event_id)
                else:
                    reprocess = True

        # ── UNKNOWN_SITE ──────────────────────────────────────────────────────
        elif q_type == "UNKNOWN_SITE":
            extracted = _extract_code_from_answer(answer)
            if answer.lower().startswith("new:") or extracted:
                _handle_unknown_site_answer(conn, extracted or answer, context, event_id)
            else:
                reprocess = True

        # ── UNRECOGNIZED_SITE (LLM proposed an ID not in DB) ──────────────────
        elif q_type == "UNRECOGNIZED_SITE":
            llm_site_id = context.get("llm_site_id", "")
            site_alias  = context.get("site_alias", "")
            if _is_affirmative(answer) and llm_site_id:
                _register_new_site(conn, llm_site_id, llm_site_id, "loading", [site_alias], event_id)
            elif answer.lower().startswith("new:"):
                _handle_unknown_site_answer(conn, answer, context, event_id)
            else:
                extracted = _extract_code_from_answer(answer)
                if extracted:
                    _handle_unknown_site_answer(conn, extracted, context, event_id)
                else:
                    reprocess = True

        # ── ENTER_ENTER_GAP: commit or discard HELD inferred events ──────────
        elif q_type == "ENTER_ENTER_GAP":
            held_ids = context.get("held_event_ids", [])
            if _is_affirmative(answer) or answer.upper() == "YES":
                # Commit all held inferred events
                for eid in held_ids:
                    conn.execute(
                        "UPDATE events SET commit_status='FLAGGED', commit_path='amber', "
                        "corrected=1 WHERE event_id=? AND commit_status='HELD'",
                        (eid,),
                    )
            else:
                # Discard held inferred events (operator says no cycle occurred)
                for eid in held_ids:
                    conn.execute(
                        "UPDATE events SET commit_status='DELETED' WHERE event_id=? AND commit_status='HELD'",
                        (eid,),
                    )

        # ── CORRECTION_AMBIGUOUS: always re-process ───────────────────────────
        elif q_type == "CORRECTION_AMBIGUOUS":
            reprocess = True

        # Record the answer
        db.answer_question(conn, req.question_id, answer, req.answered_by)

        # If re-processing: soft-delete original held event so it gets replaced
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


def _register_new_truck(
    conn, truck_id: str, display_name: str, aliases: list, event_id: Optional[str]
):
    """Insert a new truck row and commit the held event to use it."""
    try:
        db.create_truck(conn, truck_id.strip(), display_name.strip() or truck_id, aliases)
    except Exception:
        pass
    if event_id:
        conn.execute(
            "UPDATE events SET truck_id=?, commit_status='COMMITTED', commit_path='manual' "
            "WHERE event_id=? AND commit_status='HELD'",
            (truck_id.strip(), event_id),
        )


def _register_new_site(
    conn, site_id: str, display_name: str, site_type: str, aliases: list, event_id: Optional[str]
):
    """Insert a new site row and commit the held event to use it."""
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sites (site_id, display_name, aliases, site_type) VALUES (?,?,?,?)",
            (site_id.strip(), display_name.strip() or site_id,
             json.dumps(aliases), site_type.strip() or "loading"),
        )
    except Exception:
        pass
    if event_id:
        conn.execute(
            "UPDATE events SET site_id=?, commit_status='COMMITTED', commit_path='manual' "
            "WHERE event_id=? AND commit_status IN ('HELD','FLAGGED')",
            (site_id.strip(), event_id),
        )


def _handle_unknown_truck_answer(conn, answer: str, context: dict, event_id: Optional[str]):
    """
    Parse answer and update trucks + held event.
    Accepts:
      "TB"                           → add context alias to existing truck TB
      "new:TX:Truck X:alias1,alias2" → create new truck
      extracted code (already done by caller via _extract_code_from_answer)
    """
    answer = answer.strip()
    if answer.lower().startswith("new:"):
        parts = answer.split(":", 3)
        if len(parts) >= 4:
            _, truck_id, display_name, aliases_str = parts
            aliases = [a.strip() for a in aliases_str.split(",") if a.strip()]
            _register_new_truck(conn, truck_id.strip(), display_name.strip(), aliases, event_id)
        return

    # Treat answer as existing truck_id; resolve alias → canonical truck_id if needed
    truck_id = answer.upper()
    if not conn.execute("SELECT 1 FROM trucks WHERE truck_id=?", (truck_id,)).fetchone():
        import json as _json
        for row in conn.execute("SELECT truck_id, aliases FROM trucks WHERE is_active=1"):
            try:
                aliases_list = _json.loads(row["aliases"] or "[]")
            except Exception:
                aliases_list = []
            if truck_id in [a.upper() for a in aliases_list]:
                truck_id = row["truck_id"]
                break
    truck_alias = context.get("truck_alias") or context.get("llm_truck_id", "")
    if truck_alias:
        try:
            db.add_truck_alias(conn, truck_id, truck_alias)
        except Exception:
            pass
    if event_id:
        conn.execute(
            "UPDATE events SET truck_id=?, commit_status='COMMITTED', commit_path='manual' "
            "WHERE event_id=? AND commit_status='HELD'",
            (truck_id, event_id),
        )


def _handle_unknown_site_answer(conn, answer: str, context: dict, event_id: Optional[str]):
    """
    Parse answer and update sites + held event.
    Accepts:
      "SOC"                                    → add context alias to existing site SOC
      "new:SNAME:Display Name:loading:alias"   → create new site
      extracted code (already done by caller)
    """
    answer = answer.strip()
    if answer.lower().startswith("new:"):
        parts = answer.split(":", 4)
        if len(parts) >= 5:
            _, site_id, display_name, site_type, aliases_str = parts
            aliases = [a.strip() for a in aliases_str.split(",") if a.strip()]
            _register_new_site(conn, site_id.strip(), display_name.strip(),
                               site_type.strip(), aliases, event_id)
        return

    # Treat answer as existing site_id; resolve alias → canonical site_id if needed
    site_id = answer.upper()
    # Check if the supplied code is an alias rather than a primary site_id
    if not conn.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone():
        import json as _json
        for row in conn.execute("SELECT site_id, aliases FROM sites WHERE is_active=1"):
            try:
                aliases_list = _json.loads(row["aliases"] or "[]")
            except Exception:
                aliases_list = []
            if site_id in [a.upper() for a in aliases_list]:
                site_id = row["site_id"]
                break
    site_alias = context.get("site_alias") or context.get("llm_site_id", "")
    if site_alias:
        try:
            db.add_site_alias(conn, site_id, site_alias)
        except Exception:
            pass
    if event_id:
        conn.execute(
            "UPDATE events SET site_id=?, commit_status='COMMITTED', commit_path='manual' "
            "WHERE event_id=? AND commit_status IN ('HELD','FLAGGED')",
            (site_id, event_id),
        )
