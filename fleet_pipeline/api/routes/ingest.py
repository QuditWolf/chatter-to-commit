"""
Message ingestion.

POST /api/ingest/wa-message     — called by Node.js WA listener (non-blocking)
POST /api/ingest/manual         — operator panel injection (blocking)
GET  /api/ingest/status         — LLM mode / endpoint info
POST /api/ingest/reprocess-held         — re-run messages that were HELD due to LLM being offline
POST /api/ingest/reprocess-event/{id}  — re-run a single HELD event by event_id
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

# Serialise LLM calls: later messages in a batch depend on context from earlier ones
_llm_semaphore = asyncio.Semaphore(1)


class WAMessageRequest(BaseModel):
    wa_message_id: str
    sender_phone: str
    sender_name: Optional[str] = (
        None  # WhatsApp display name (pushName); falls back to sender_phone
    )
    group_jid: str
    raw_text: str
    received_at: str  # ISO string from Node
    message_type: str = "fleet_event"
    quoted_wa_message_id: Optional[str] = (
        None  # set when operator replies to a bot message
    )


class ManualMessageRequest(BaseModel):
    text: str
    sender_name: Optional[str] = "operator"
    sender_id: Optional[str] = "manual"
    timestamp_iso: Optional[str] = None


async def _broadcast_summary(summary: dict, source: str):
    from fleet_pipeline.api.main import ws_manager
    from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

    invalidate_kpi_cache()
    await ws_manager.broadcast(
        "commit_created",
        {
            "msg_id": summary.get("msg_id", ""),
            "raw_text": summary.get("raw_text", ""),
            "committed": summary.get("committed", 0),
            "flagged": summary.get("flagged", 0),
            "held": summary.get("held", 0),
            "unmapped": summary.get("unmapped", False),
            "source": source,
        },
    )
    if summary.get("committed", 0) > 0:
        await ws_manager.broadcast("fleet_state_updated", {"source": source})
    if summary.get("hitl_created", 0) > 0:
        await ws_manager.broadcast("hitl_created", {"source": source})


async def _process_and_broadcast(
    raw_text: str,
    sender_name: str,
    sender_id: str,
    timestamp_iso: Optional[str],
    source: str,
    msg_id: Optional[str] = None,
    wa_message_id: Optional[str] = None,
    group_jid: Optional[str] = None,
    quoted_wa_message_id: Optional[str] = None,
):
    """Run pipeline and broadcast result. Used as a background task.

    process_raw_text() is synchronous (blocks on LLM HTTP call).
    We run it in a thread-pool executor so it never blocks the event loop,
    keeping the server responsive for other requests while LLM is running.
    """
    from functools import partial
    from fleet_pipeline.api.pipeline_service import process_raw_text
    from fleet_pipeline.api.main import ws_manager

    try:
        async with _llm_semaphore:
            loop = asyncio.get_event_loop()
            summary = await loop.run_in_executor(
                None,
                partial(
                    process_raw_text,
                    raw_text=raw_text,
                    sender_name=sender_name,
                    sender_id=sender_id,
                    timestamp_iso=timestamp_iso,
                    source=source,
                    wa_message_id=wa_message_id,
                    group_jid=group_jid,
                    quoted_wa_message_id=quoted_wa_message_id,
                ),
            )
        # If a pending msg_id was known up front, attach it so frontend can correlate
        if msg_id and not summary.get("msg_id"):
            summary["msg_id"] = msg_id
        await _broadcast_summary(summary, source)
    except Exception as exc:
        log.error("Background pipeline error: %s", exc)
        await ws_manager.broadcast(
            "commit_error",
            {
                "raw_text": raw_text,
                "error": str(exc)[:200],
                "source": source,
            },
        )


@router.post("/wa-message", status_code=202)
async def ingest_wa_message(req: WAMessageRequest, background_tasks: BackgroundTasks):
    """
    Called by the Node.js WA listener for every group message.
    Returns 202 immediately. Processing happens in background.

    If quoted_wa_message_id matches an open HITL question's bot_wa_message_id,
    the message is routed as a HITL answer (skipping the normal pipeline).
    """
    import sqlite3
    from fleet_pipeline.config import DB_PATH
    from fleet_pipeline.api.main import ws_manager
    from fleet_pipeline.db import database as db

    # Store in wa_messages immediately (fast)
    with sqlite3.connect(DB_PATH) as conn:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO wa_messages
                   (wa_message_id, sender_phone, group_jid, raw_text, received_at, message_type, processed)
                   VALUES (?, ?, ?, ?, ?, ?, 0)""",
                (
                    req.wa_message_id,
                    req.sender_phone,
                    req.group_jid,
                    req.raw_text,
                    req.received_at,
                    req.message_type,
                ),
            )
        except Exception:
            pass

    # ── HITL reply routing ────────────────────────────────────────────────────
    if req.quoted_wa_message_id:
        with db.db_conn(DB_PATH) as conn:
            hitl_q = db.get_open_question_by_bot_wa_id(conn, req.quoted_wa_message_id)
        if hitl_q:
            # Operator replied to a bot HITL message — route as answer
            background_tasks.add_task(
                _handle_wa_hitl_answer,
                question=hitl_q,
                answer_text=req.raw_text.strip(),
                answered_by=req.sender_phone,
                group_jid=req.group_jid,
            )
            return {
                "queued": True,
                "routed_as": "hitl_answer",
                "question_id": hitl_q["question_id"],
            }

    # ── Normal pipeline ───────────────────────────────────────────────────────
    await ws_manager.broadcast(
        "message_received",
        {
            "wa_message_id": req.wa_message_id,
            "raw_text": req.raw_text,
            "sender": req.sender_phone,
            "timestamp_iso": req.received_at,
            "source": "whatsapp",
        },
    )

    background_tasks.add_task(
        _process_and_broadcast,
        raw_text=req.raw_text,
        sender_name=req.sender_name or req.sender_phone,
        sender_id=req.sender_phone,
        timestamp_iso=req.received_at,
        source="whatsapp",
        msg_id=req.wa_message_id,
        wa_message_id=req.wa_message_id,
        group_jid=req.group_jid,
        quoted_wa_message_id=req.quoted_wa_message_id,
    )

    return {"queued": True, "wa_message_id": req.wa_message_id}


async def _handle_wa_hitl_answer(
    question: dict,
    answer_text: str,
    answered_by: str,
    group_jid: str,
):
    """Process a HITL answer that arrived via WA reply."""
    from functools import partial
    from fleet_pipeline.api.routes.hitl import submit_answer, AnswerRequest
    from fleet_pipeline.api.main import ws_manager
    from fleet_pipeline.pipeline.wa_notifier import _post_send_reply
    from fastapi import BackgroundTasks as BT

    question_id = question["question_id"]
    log.info("WA HITL answer: question=%s answer=%r", question_id, answer_text[:80])

    try:
        req = AnswerRequest(
            question_id=question_id,
            answer=answer_text,
            answered_by=f"wa:{answered_by}",
        )
        bt = BT()
        result = await submit_answer(req, bt)
        # Run any background tasks (re-process) that submit_answer scheduled
        for task in bt.tasks:
            await task()

        status = result.get("status", "ok")
        ack = f"✅ Received — {status}"
        if status == "reprocessing":
            ack = "✅ Clarification received — re-processing message…"
        _post_send_reply(group_jid, ack, question.get("bot_wa_message_id"))

        await ws_manager.broadcast(
            "hitl_answered_wa",
            {
                "question_id": question_id,
                "answer": answer_text,
            },
        )
    except Exception as exc:
        log.error("WA HITL answer error: %s", exc)
        _post_send_reply(
            group_jid, f"⚠️ Error processing answer: {str(exc)[:100]}", None
        )


@router.post("/manual", status_code=202)
async def ingest_manual(req: ManualMessageRequest, background_tasks: BackgroundTasks):
    """Operator panel injection — returns 202 immediately, processes in background.
    Broadcasts:
      1. message_received  — instant, shows pending state in message map
      2. commit_created    — when LLM finishes
    """
    from uuid import uuid4
    from datetime import datetime, timezone
    from fleet_pipeline.api.main import ws_manager

    temp_id = str(uuid4())
    timestamp = req.timestamp_iso or datetime.now(timezone.utc).isoformat()

    # Broadcast immediately so frontend shows pending state in message map
    await ws_manager.broadcast(
        "message_received",
        {
            "wa_message_id": temp_id,
            "raw_text": req.text,
            "sender": req.sender_name or "operator",
            "timestamp_iso": timestamp,
            "source": "manual",
        },
    )

    # Process in background — same path as WA messages.
    # Pass wa_message_id=temp_id so the DB msg_id matches the pending key the
    # frontend already stored, allowing removePending(data.msg_id) to work.
    background_tasks.add_task(
        _process_and_broadcast,
        raw_text=req.text,
        sender_name=req.sender_name or "operator",
        sender_id=req.sender_id or "manual",
        timestamp_iso=req.timestamp_iso,
        source="manual",
        msg_id=temp_id,
        wa_message_id=temp_id,
    )

    return {"queued": True, "temp_id": temp_id}


@router.post("/reprocess-held")
async def reprocess_held():
    """
    Re-run all HELD messages that were held due to LLM being offline.
    Identified by events.reasoning starting with 'LLM unavailable'.
    Call this after the LLM comes back online.
    """
    import sqlite3
    from fleet_pipeline.config import DB_PATH
    from fleet_pipeline.api.main import ws_manager

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT DISTINCT rm.msg_id, rm.raw_text, rm.timestamp_iso,
                               rm.sender_name, rm.sender_id, rm.source_file
               FROM raw_messages rm
               JOIN events e ON rm.msg_id = e.msg_id
               WHERE e.commit_status = 'HELD'
                 AND e.reasoning LIKE 'LLM unavailable%'
               ORDER BY rm.created_at ASC
               LIMIT 50"""
        ).fetchall()

    if not rows:
        return {"reprocessed": 0, "message": "No LLM-offline HELD messages found"}

    msgs = [dict(r) for r in rows]

    async def _run_all():
        from functools import partial
        from fleet_pipeline.api.pipeline_service import process_raw_text
        from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

        loop = asyncio.get_event_loop()
        count = 0
        for m in msgs:
            try:
                summary = await loop.run_in_executor(
                    None,
                    partial(
                        process_raw_text,
                        raw_text=m["raw_text"],
                        sender_name=m["sender_name"] or "unknown",
                        sender_id=m["sender_id"] or "unknown",
                        timestamp_iso=m["timestamp_iso"],
                        source=m["source_file"] or "reprocess",
                    ),
                )
                count += 1
                invalidate_kpi_cache()
                await ws_manager.broadcast(
                    "commit_created",
                    {
                        "msg_id": summary.get("msg_id", ""),
                        "raw_text": summary.get("raw_text", ""),
                        "committed": summary.get("committed", 0),
                        "flagged": summary.get("flagged", 0),
                        "held": summary.get("held", 0),
                        "source": "reprocess",
                    },
                )
            except Exception as exc:
                log.warning("Reprocess failed for %s: %s", m["msg_id"], exc)
        await ws_manager.broadcast("reprocess_complete", {"count": count})

    asyncio.create_task(_run_all())
    return {
        "reprocessed": len(msgs),
        "message": f"Reprocessing {len(msgs)} held messages in background",
    }


@router.post("/reprocess-event/{event_id}")
async def reprocess_single_event(event_id: str):
    """
    Re-run a single HELD event by event_id.
    Looks up the raw message via the event's msg_id and re-processes it through the pipeline.
    """
    import sqlite3
    from fleet_pipeline.config import DB_PATH
    from fleet_pipeline.api.main import ws_manager

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT rm.msg_id, rm.raw_text, rm.timestamp_iso,
                      rm.sender_name, rm.sender_id, rm.source_file
               FROM events e
               JOIN raw_messages rm ON e.msg_id = rm.msg_id
               WHERE e.event_id = ? AND e.commit_status = 'HELD'
               LIMIT 1""",
            (event_id,),
        ).fetchone()

    if not row:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="HELD event not found")

    m = dict(row)

    async def _run():
        from functools import partial
        from fleet_pipeline.api.pipeline_service import process_raw_text
        from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

        loop = asyncio.get_event_loop()
        try:
            summary = await loop.run_in_executor(
                None,
                partial(
                    process_raw_text,
                    raw_text=m["raw_text"],
                    sender_name=m["sender_name"] or "unknown",
                    sender_id=m["sender_id"] or "unknown",
                    timestamp_iso=m["timestamp_iso"],
                    source=m["source_file"] or "reprocess",
                ),
            )
            invalidate_kpi_cache()
            await ws_manager.broadcast(
                "commit_created",
                {
                    "msg_id": summary.get("msg_id", ""),
                    "raw_text": summary.get("raw_text", ""),
                    "committed": summary.get("committed", 0),
                    "flagged": summary.get("flagged", 0),
                    "held": summary.get("held", 0),
                    "source": "reprocess",
                },
            )
            await ws_manager.broadcast("reprocess_complete", {"count": 1})
        except Exception as exc:
            log.warning("Reprocess failed for event %s: %s", event_id, exc)
            await ws_manager.broadcast(
                "reprocess_complete", {"count": 0, "error": str(exc)}
            )

    asyncio.create_task(_run())
    return {"reprocessed": 1, "message": f"Reprocessing event {event_id} in background"}


@router.post("/reprocess-message/{msg_id}")
async def reprocess_message_by_id(msg_id: str):
    """
    Re-run any raw_message through the full pipeline by msg_id.
    Works regardless of whether the message has an existing event — for unparsed/failed messages.
    """
    import sqlite3
    from fleet_pipeline.config import DB_PATH
    from fleet_pipeline.api.main import ws_manager

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT msg_id, raw_text, timestamp_iso, sender_name, sender_id, source_file
               FROM raw_messages WHERE msg_id = ?""",
            (msg_id,),
        ).fetchone()

    if not row:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Message not found")

    m = dict(row)

    async def _run():
        from functools import partial
        from fleet_pipeline.api.pipeline_service import process_raw_text
        from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

        loop = asyncio.get_event_loop()
        try:
            summary = await loop.run_in_executor(
                None,
                partial(
                    process_raw_text,
                    raw_text=m["raw_text"],
                    sender_name=m["sender_name"] or "unknown",
                    sender_id=m["sender_id"] or "unknown",
                    timestamp_iso=m["timestamp_iso"],
                    source=m["source_file"] or "reprocess",
                ),
            )
            invalidate_kpi_cache()
            await ws_manager.broadcast(
                "commit_created",
                {
                    "msg_id": summary.get("msg_id", ""),
                    "raw_text": summary.get("raw_text", ""),
                    "committed": summary.get("committed", 0),
                    "flagged": summary.get("flagged", 0),
                    "held": summary.get("held", 0),
                    "source": "reprocess",
                },
            )
            await ws_manager.broadcast("reprocess_complete", {"count": 1})
        except Exception as exc:
            log.warning("Reprocess failed for msg %s: %s", msg_id, exc)
            await ws_manager.broadcast(
                "reprocess_complete", {"count": 0, "error": str(exc)}
            )

    asyncio.create_task(_run())
    return {"reprocessed": 1, "message": f"Reprocessing message {msg_id} in background"}


@router.get("/status")
def ingest_status():
    """Returns current LLM mode and endpoint config (no secrets)."""
    from fleet_pipeline.config import LLM_MOCK, LLM_BASE_URL, MODEL_NAME

    if LLM_MOCK:
        mode = "mock"
        endpoint = None
    elif LLM_BASE_URL:
        mode = "openai_compat"
        endpoint = LLM_BASE_URL
    else:
        mode = "vllm_inprocess"
        endpoint = None
    return {"mode": mode, "endpoint": endpoint, "model": MODEL_NAME}
