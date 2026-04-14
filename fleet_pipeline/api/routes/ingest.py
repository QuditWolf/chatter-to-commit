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
import re
from typing import Optional

import pytz
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from fleet_pipeline.utils import now_ist, now_ist_iso, to_ist

log = logging.getLogger(__name__)

# Configured shift start patterns (from shift_config table)
_CONFIGURED_SHIFT_PATTERNS = [
    re.compile(r"\bshift\s+(start|started|begin|begins|shuru)\b", re.I),
    re.compile(r"\bs([123])\b", re.I),
    re.compile(r"\bshift\s+([123])\b", re.I),
    re.compile(r"\btracking\s+volunteers?\b", re.I),
    re.compile(r"\bvolunteers?\b.*breach\b", re.I),
]


def _is_configured_shift_pattern(text: str) -> bool:
    """Return True if text matches configured shift start patterns."""
    t = (text or "").strip()
    return any(p.search(t) for p in _CONFIGURED_SHIFT_PATTERNS)


def _notify_shift_clarification(
    group_jid: str, text: str, ctrl_jid: str, db_path: str
) -> None:
    """Send HITL question to control group asking for shift start clarification."""
    from fleet_pipeline.pipeline.wa_notifier import _resolve_group_jid
    from fleet_pipeline.pipeline.hitl_queue import create_hitl_question

    target_jid = _resolve_group_jid(ctrl_jid) or group_jid
    question_text = (
        f'❓ Unclear shift start — "{text}"\n\n'
        f"Please reply with:\n"
        f"• Site code to start at (e.g. `SOC`)\n"
        f"• `no shift` to cancel"
    )
    try:
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            qid = create_hitl_question(
                conn,
                msg_id=f"shift_clarify_{now_ist_iso()}",
                question_type="SHIFT_START Clarification",
                question_text=question_text,
                context={"raw_text": text},
            )
            # Send WA message
            import urllib.request

            payload = {"group_jid": target_jid, "text": question_text}.encode()
            req = urllib.request.Request(
                f"{__import__('os').environ.get('WA_LISTENER_URL', 'http://wa:3001')}/send-message",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                pass  # Fire and forget
    except Exception as e:
        log.warning("Failed to send shift clarification: %s", e)


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
    source_group: str = (
        "fleet"  # "fleet" (read-only) | "control" (HITL + shift signals)
    )
    quoted_wa_message_id: Optional[str] = (
        None  # set when operator replies to a bot message
    )


class ManualMessageRequest(BaseModel):
    text: str
    sender_name: Optional[str] = "operator"
    sender_id: Optional[str] = "manual"
    timestamp_iso: Optional[str] = None


class WAMessageDeletedRequest(BaseModel):
    wa_message_id: str
    group_jid: Optional[str] = None
    deleted_by: Optional[str] = None


class ReprocessRequest(BaseModel):
    context: Optional[str] = None  # extra context prepended to raw message text


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

    Fleet group (source_group="fleet"): READ-ONLY — messages run through the
    fleet event pipeline. Bot messages are NEVER sent to this group.

    Control group (source_group="control"): HITL answers, shift signals with
    site announcements, and summary requests. All bot replies go here.
    """
    import sqlite3
    from fleet_pipeline.config import DB_PATH, WA_CONTROL_GROUP_JID
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

    # Determine if this message is from the control group
    is_control = req.source_group == "control" or (
        WA_CONTROL_GROUP_JID and req.group_jid == WA_CONTROL_GROUP_JID
    )

    from fleet_pipeline.utils import to_ist as _to_ist

    _ts_ist = _to_ist(req.received_at)

    # ── Control group: HITL answers, shift signals, summary requests ──────────
    if is_control:
        # HITL reply routing — operator replied to a bot clarification
        if req.quoted_wa_message_id:
            with db.db_conn(DB_PATH) as conn:
                hitl_q = db.get_open_question_by_bot_wa_id(
                    conn, req.quoted_wa_message_id
                )
            if hitl_q:
                log.info(
                    "[CTRL] %s | from=%s | HITL reply → question %s | %r",
                    _ts_ist,
                    req.sender_phone,
                    hitl_q["question_id"],
                    req.raw_text[:80],
                )
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
            else:
                log.info(
                    "[CTRL] %s | from=%s | reply to unknown bot msg %s (no open HITL) — routing as control msg | %r",
                    _ts_ist,
                    req.sender_phone,
                    req.quoted_wa_message_id[:12],
                    req.raw_text[:80],
                )

        else:
            log.info(
                "[CTRL] %s | from=%s | plain control message | %r",
                _ts_ist,
                req.sender_phone,
                req.raw_text[:80],
            )

        # Shift signals + summary requests
        background_tasks.add_task(
            _handle_control_message,
            raw_text=req.raw_text,
            timestamp_iso=req.received_at,
            group_jid=req.group_jid,
            is_reply=bool(req.quoted_wa_message_id),
            quoted_wa_message_id=req.quoted_wa_message_id,
        )
        return {"queued": True, "wa_message_id": req.wa_message_id, "source": "control"}

    # ── Fleet group: pipeline only — NEVER send messages back ─────────────────
    # All bot output (HITL questions, summaries) goes to the control group.
    log.info(
        "[FLEET] %s | from=%s | queuing pipeline | %r",
        _ts_ist,
        req.sender_phone,
        req.raw_text[:80],
    )
    bot_group_jid = WA_CONTROL_GROUP_JID or req.group_jid

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
        group_jid=bot_group_jid,
        quoted_wa_message_id=req.quoted_wa_message_id,
    )

    return {"queued": True, "wa_message_id": req.wa_message_id}


async def _handle_control_message(
    raw_text: str,
    timestamp_iso: str,
    group_jid: str,
    is_reply: bool = False,
    quoted_wa_message_id: str = None,
) -> None:
    """
    Handle a message from the control group (not a HITL reply).
    Handles: shift start/end with site extraction, and summary requests.

    is_reply=True means the message was a quoted reply to another message.
    Shift signals are only processed from plain (non-reply) messages to avoid
    accidental shift-end from HITL answers like "loading over" or "ALL trucks LEFT".
    """
    import re
    import sqlite3
    from fleet_pipeline.config import DB_PATH, WA_CONTROL_GROUP_JID
    from fleet_pipeline.pipeline.shift_detector import (
        detect_shift_signal,
        ShiftDetector,
        operator_resume,
        operator_end,
    )
    from fleet_pipeline.pipeline.wa_notifier import (
        send_summary_to_group,
        send_shift_notification,
    )
    from fleet_pipeline.api.main import ws_manager
    from datetime import datetime
    from fleet_pipeline.utils import to_ist, now_ist

    text = raw_text.strip()
    _ts_ist = to_ist(timestamp_iso) if timestamp_iso else "?"
    log.info("[CTRL] %s | is_reply=%s | %r", _ts_ist, is_reply, text[:100])

    ctrl_jid = WA_CONTROL_GROUP_JID or group_jid

    # Guard: never treat bot-generated messages as new triggers.
    # (The WA listener already filters fromMe=true, but double-guard here.)
    # Covers: shift summaries (──), shift notifications (🟢/🔴/↩), commit
    # notifications (✅ Committed), deletion notifications (🗑), HITL questions (❓/⚠️).
    _BOT_PREFIXES = (
        "\u2500\u2500",  # ── shift summary
        "--",
        "\U0001f7e2",  # 🟢 shift start
        "\U0001f534",  # 🔴 shift end
        "\u21a9",  # ↩ shift resume
        "\u2705 Committed",  # commit notification
        "\U0001f5d1",  # 🗑 deletion notification
        "\u2705 New trolley",  # new truck notification
        "\u2705 Merged",
        "\u26a0\ufe0f",  # ⚠️ loading alert / HITL
        "\u274c",  # ❌
        "\u2753",  # ❓ HITL question
    )
    if any(text.startswith(p) for p in _BOT_PREFIXES):
        log.debug("[CTRL] Skipped — looks like bot-generated message (prefix guard)")
        return

    # ── Reply to a shift notification bot message ──────────────────────────────
    # Supports: "no start" / "cancel" → end shift; "resume" → resume last shift
    #           "no end" / "resume" → resume the just-ended shift
    _NO_START_RE = re.compile(r"\b(no\s+start|don.?t\s+start|cancel)\b", re.I)
    _RESUME_RE = re.compile(r"\bresume\b", re.I)
    _NO_END_RE = re.compile(r"\b(no\s+end|don.?t\s+end)\b", re.I)

    if is_reply and quoted_wa_message_id:
        try:
            _conn = sqlite3.connect(DB_PATH)
            _conn.row_factory = sqlite3.Row
            notif_row = _conn.execute(
                """SELECT shift_id, shift_name, ended_at,
                          CASE
                            WHEN start_notif_bot_msg_id=? THEN 'start_notif'
                            WHEN end_notif_bot_msg_id=?   THEN 'end_notif'
                          END AS notif_type
                   FROM shifts
                   WHERE start_notif_bot_msg_id=? OR end_notif_bot_msg_id=?
                   ORDER BY started_at DESC LIMIT 1""",
                (quoted_wa_message_id,) * 4,
            ).fetchone()
            _conn.close()
        except Exception as _exc:
            log.warning("[CTRL] Shift notif lookup failed: %s", _exc)
            notif_row = None

        if notif_row and notif_row["notif_type"]:
            ntype = notif_row["notif_type"]
            log.info(
                "[CTRL] Reply to shift notification (%s) — text=%r", ntype, text[:60]
            )
            action_taken = None
            if ntype == "start_notif":
                if _NO_START_RE.search(text):
                    operator_end(DB_PATH)
                    action_taken = "end"
                    log.info("[CTRL] 'no start' reply → shift ended")
                elif _RESUME_RE.search(text):
                    resumed = operator_resume(DB_PATH)
                    if resumed:
                        action_taken = "resume"
                        log.info("[CTRL] 'resume' reply → shift resumed")
            elif ntype == "end_notif":
                if _NO_END_RE.search(text) or _RESUME_RE.search(text):
                    resumed = operator_resume(DB_PATH)
                    if resumed:
                        action_taken = "resume"
                        log.info("[CTRL] 'no end / resume' reply → shift resumed")

            if action_taken:
                try:
                    with sqlite3.connect(DB_PATH) as _c:
                        _c.row_factory = sqlite3.Row
                        if action_taken == "resume":
                            new_shift = _c.execute(
                                "SELECT * FROM shifts WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
                            ).fetchone()
                            if new_shift:
                                send_shift_notification(
                                    dict(new_shift), "resume", ctrl_jid, DB_PATH
                                )
                except Exception as _exc:
                    log.warning("[CTRL] Post-action notification failed: %s", _exc)
                await ws_manager.broadcast(
                    "shift_changed", {"reason": f"shift_notif_reply_{action_taken}"}
                )
                return

        # ── Reply to a commit notification bot message ─────────────────────────
        # If the quoted message is a "✅ Committed (inferred)" notification, route
        # as a correction: confirm (no-op) or reprocess with operator text as context.
        _commit_ev = None
        try:
            _cconn = sqlite3.connect(DB_PATH)
            _cconn.row_factory = sqlite3.Row
            _commit_ev = _cconn.execute(
                "SELECT event_id, msg_id FROM events WHERE commit_notif_bot_msg_id=? LIMIT 1",
                (quoted_wa_message_id,),
            ).fetchone()
            _cconn.close()
        except Exception as _exc:
            log.warning("[CTRL] commit notif lookup failed: %s", _exc)

        if _commit_ev:
            _CONFIRM_SET = {
                "ok",
                "yes",
                "okay",
                "confirm",
                "confirmed",
                "correct",
                "right",
                "noted",
                "👍",
                "✓",
                "✔",
                "k",
                "ha",
                "haan",
            }
            if text.lower().strip() in _CONFIRM_SET:
                log.info("[CTRL] commit notif reply: confirmation — no action needed")
            else:
                log.info(
                    "[CTRL] commit notif reply: correction for event %s — reprocessing",
                    _commit_ev["event_id"],
                )
                try:
                    _orig = None
                    with sqlite3.connect(DB_PATH) as _c2:
                        _c2.row_factory = sqlite3.Row
                        _orig = _c2.execute(
                            "SELECT rm.raw_text, rm.timestamp_iso, rm.sender_name, "
                            "rm.sender_id, rm.source_file FROM raw_messages rm "
                            "JOIN events e ON e.msg_id=rm.msg_id "
                            "WHERE e.event_id=? LIMIT 1",
                            (_commit_ev["event_id"],),
                        ).fetchone()
                    if _orig:
                        from fleet_pipeline.pipeline.wa_notifier import (
                            _post_send_reply,
                            _resolve_group_jid,
                        )

                        _notif_jid = _resolve_group_jid(ctrl_jid)
                        asyncio.create_task(
                            _process_and_broadcast(
                                raw_text=f"[Context: {text}]\n{_orig['raw_text']}",
                                sender_name=_orig["sender_name"] or "unknown",
                                sender_id=_orig["sender_id"] or "unknown",
                                timestamp_iso=_orig["timestamp_iso"],
                                source="commit_correction",
                                group_jid=_notif_jid,
                            )
                        )
                        if _notif_jid:
                            _post_send_reply(
                                _notif_jid,
                                "\u2705 Correction received \u2014 re-processing\u2026",
                                quoted_wa_message_id,
                            )
                except Exception as _exc:
                    log.warning("[CTRL] commit notif correction failed: %s", _exc)
            return

    # ── Shift signal (start/end) — only from plain messages, not replies ──────
    # Replies (quoted messages) are HITL answers; they must not accidentally
    # end the shift via phrases like "Loading Over" or "ALL trucks LEFT".
    # Also skip if it's an unknown pattern - ask HITL instead.
    signal = detect_shift_signal(text) if not is_reply else None
    if is_reply and detect_shift_signal(text):
        log.info(
            "[CTRL] Shift signal %r suppressed — message is a reply (HITL answer)",
            detect_shift_signal(text),
        )
    # Check if shift start is from non-configured pattern without valid truck status
    if signal == "start" and not is_reply:
        # Valid truck status: truck letter followed by status keyword
        _truck_status_re = re.compile(r"^[A-Z]\s+(LS|LO|US|UO|ENTER)\b", re.I)
        has_truck_status = bool(_truck_status_re.search(text.strip()))
        # If neither configured pattern nor valid truck status, skip shift start
        if not _is_configured_shift_pattern(text) and not has_truck_status:
            log.info(
                "[CTRL] Shift start skipped — not a configured pattern or valid truck update"
            )
            try:
                _notify_shift_clarification(group_jid, text, ctrl_jid, DB_PATH)
            except Exception as _exc:
                log.warning("[CTRL] HITL clarification failed: %s", _exc)
            await ws_manager.broadcast(
                "shift_changed", {"reason": "clarification_needed"}
            )
            return
        log.info("[CTRL] Shift %s signal detected — processing", signal)
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")

            ts = None
            try:
                ts = datetime.fromisoformat(timestamp_iso)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=pytz.UTC)
            except Exception:
                ts = now_ist()

            sd = ShiftDetector(conn)
            new_shift = None
            if signal == "start":
                sd._start_new(ts, method="wa_signal", raw_text=text)
                new_shift = sd._active
                if new_shift:
                    conn.execute(
                        """INSERT INTO shift_events
                           (shift_event_id, shift_id, status, timestamp_iso,
                            commit_status, wa_message_id, site_id, site_ids_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            f"shift_start_{new_shift['shift_id']}",
                            new_shift["shift_id"],
                            "SHIFT_START",
                            ts.isoformat(),
                            "COMMITTED",
                            wa_message_id,
                            new_shift.get("default_site_id"),
                            new_shift.get("default_site_ids"),
                        ),
                    )
            else:
                new_shift = sd._active
                if new_shift:
                    conn.execute(
                        """INSERT INTO shift_events
                           (shift_event_id, shift_id, status, timestamp_iso,
                            commit_status, wa_message_id)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            f"shift_end_{new_shift['shift_id']}",
                            new_shift["shift_id"],
                            "SHIFT_END",
                            ts.isoformat(),
                            "COMMITTED",
                            wa_message_id,
                        ),
                    )
                sd._end(ts)

            conn.commit()
            conn.close()

            if new_shift:
                send_shift_notification(
                    new_shift,
                    "start" if signal == "start" else "end",
                    ctrl_jid,
                    DB_PATH,
                )

            # After ending a shift via WA signal: close open cycles, then post summary
            if signal == "end" and new_shift:
                try:
                    from fleet_pipeline.pipeline.committer import close_open_cycles_at_shift_end
                    from fleet_pipeline.pipeline.wa_notifier import _resolve_group_jid

                    _cyc_jid = _resolve_group_jid(ctrl_jid)
                    close_open_cycles_at_shift_end(
                        DB_PATH,
                        new_shift["shift_id"],
                        ts.isoformat(),
                        group_jid=_cyc_jid,
                    )
                except Exception as _cyc_exc:
                    log.warning("Shift-end cycle close failed: %s", _cyc_exc)
                try:
                    from fleet_pipeline.pipeline.wa_notifier import (
                        send_summary_to_group,
                        _resolve_group_jid,
                    )

                    _sum_jid = _resolve_group_jid(ctrl_jid)
                    if _sum_jid:
                        send_summary_to_group(_sum_jid, DB_PATH)
                except Exception as _sum_exc:
                    log.warning("Shift-end summary post failed: %s", _sum_exc)

            await ws_manager.broadcast(
                "shift_changed",
                {"reason": f"control_{signal}_signal", "raw_text": text},
            )
        except Exception as exc:
            log.error("Control shift signal handling failed: %s", exc)
        return

    # ── Standalone shift control words (not replies, not shift signals) ────────
    # "no start" / "no end" / "resume" as plain standalone messages
    _SA_NO_START = re.compile(
        r"^\s*(no\s+start|don.?t\s+start|cancel\s+(the\s+)?shift)\s*$", re.I
    )
    _SA_NO_END = re.compile(
        r"^\s*(no\s+end|don.?t\s+end|cancel\s+(the\s+)?end)\s*$", re.I
    )
    _SA_RESUME = re.compile(r"^\s*(resume|resume\s+(shift|last|it))\s*$", re.I)

    if not is_reply:
        if _SA_NO_START.match(text):
            operator_end(DB_PATH)
            log.info("[CTRL] Standalone 'no start' → shift ended")
            await ws_manager.broadcast(
                "shift_changed", {"reason": "standalone_no_start"}
            )
            return
        if _SA_NO_END.match(text) or _SA_RESUME.match(text):
            resumed = operator_resume(DB_PATH)
            if resumed:
                log.info("[CTRL] Standalone 'no end / resume' → shift resumed")
                send_shift_notification(resumed, "resume", ctrl_jid, DB_PATH)
                await ws_manager.broadcast(
                    "shift_changed", {"reason": "standalone_resume"}
                )
            return

    # ── Summary request ───────────────────────────────────────────────────────
    # Note: bare "total" or "count" removed to avoid triggering on bot's own
    # summary messages (which contain "Total Trolleys Loaded = N").
    _SUMMARY_RE = [
        re.compile(r"\b(summary|sumary|summery)\b", re.I),
        re.compile(r"\bsend\s+(report|summary)\b", re.I),
        re.compile(
            r"\b(report|give|send)\s+(me\s+)?(the\s+)?(total|count|summary)\b", re.I
        ),
        re.compile(r"\bhow\s+many\b", re.I),
    ]
    for pat in _SUMMARY_RE:
        if pat.search(text):
            log.info(
                "[CTRL] Summary request matched — sending on-demand summary to group"
            )
            try:
                send_summary_to_group(group_jid, DB_PATH)
            except Exception as exc:
                log.warning("On-demand summary failed: %s", exc)
            return

    # ── MERGE truck command ───────────────────────────────────────────────────
    # Syntax: MERGE <src_alias_or_id> <dst_alias_or_id>
    # Merges src truck into dst: copies aliases, reassigns events, deactivates src.
    _MERGE_RE = re.compile(r"^\s*MERGE\s+(\S+)\s+(\S+)\s*$", re.I)
    m = _MERGE_RE.match(text)
    if m:
        src_raw, dst_raw = m.group(1).strip(), m.group(2).strip()
        log.info("[CTRL] MERGE command: %s → %s", src_raw, dst_raw)
        import sqlite3 as _sq

        try:
            conn = _sq.connect(DB_PATH)
            conn.row_factory = _sq.Row
            conn.execute("PRAGMA foreign_keys=ON")

            # Resolve alias → truck_id
            def _resolve_truck(alias_or_id: str) -> Optional[str]:
                row = conn.execute(
                    "SELECT truck_id FROM trucks WHERE truck_id=? AND is_active=1",
                    (alias_or_id,),
                ).fetchone()
                if row:
                    return row[0]
                for r in conn.execute(
                    "SELECT truck_id, aliases FROM trucks WHERE is_active=1"
                ):
                    try:
                        aliases = __import__("json").loads(r["aliases"] or "[]")
                    except Exception:
                        aliases = []
                    if alias_or_id.lower() in [a.lower() for a in aliases]:
                        return r["truck_id"]
                return None

            src_id = _resolve_truck(src_raw)
            dst_id = _resolve_truck(dst_raw)

            from fleet_pipeline.db import database as _db
            from fleet_pipeline.pipeline.wa_notifier import (
                _post_send_message,
                _resolve_group_jid,
            )

            if not src_id or not dst_id:
                missing = src_raw if not src_id else dst_raw
                log.warning("[CTRL] MERGE failed — trolley '%s' not found", missing)
                notify_jid = _resolve_group_jid(group_jid)
                if notify_jid:
                    _post_send_message(
                        notify_jid, f"⚠️ MERGE failed: trolley '{missing}' not found."
                    )
                conn.close()
                return

            result = _db.merge_trucks(conn, src_id, dst_id)
            conn.commit()
            conn.close()

            log.info(
                "[MERGE] %s → %s: %d events reassigned, aliases added: %s",
                src_id,
                dst_id,
                result["events_reassigned"],
                result["aliases_added"],
            )

            notify_jid = _resolve_group_jid(group_jid)
            if notify_jid:
                _post_send_message(
                    notify_jid,
                    f"✅ Merged *{src_raw}* (ID: {src_id}) into *{dst_raw}* (ID: {dst_id})\n"
                    f"  Events reassigned: {result['events_reassigned']}\n"
                    f"  Aliases added: {', '.join(result['aliases_added']) or 'none'}",
                )

            from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

            invalidate_kpi_cache()
        except Exception as exc:
            log.error("MERGE command failed: %s", exc)
        return

    log.info(
        "[CTRL] No action taken — message did not match shift signal, summary, or MERGE pattern"
    )


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
    from fleet_pipeline.api.main import ws_manager

    temp_id = str(uuid4())
    timestamp = req.timestamp_iso or now_ist_iso()

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
                # Use the same semaphore as normal processing to serialize LLM calls
                async with _llm_semaphore:
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
async def reprocess_single_event(event_id: str, body: ReprocessRequest = None):
    """
    Re-run a single HELD event by event_id.
    Looks up the raw message via the event's msg_id and re-processes it through the pipeline.
    Optional body: { context: "..." } — prepended to raw_text before re-processing.
    Works for HELD events; also accepts FLAGGED or COMMITTED (for Re-LLM on any event).
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
               WHERE e.event_id = ?
               LIMIT 1""",
            (event_id,),
        ).fetchone()

    if not row:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Event not found")

    m = dict(row)
    context = (body.context or "").strip() if body else ""
    raw_text = f"[Context: {context}]\n{m['raw_text']}" if context else m["raw_text"]

    async def _run():
        from functools import partial
        from fleet_pipeline.api.pipeline_service import process_raw_text
        from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

        loop = asyncio.get_event_loop()
        try:
            async with _llm_semaphore:
                summary = await loop.run_in_executor(
                    None,
                    partial(
                        process_raw_text,
                        raw_text=raw_text,
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
async def reprocess_message_by_id(msg_id: str, body: ReprocessRequest = None):
    """
    Re-run any raw_message through the full pipeline by msg_id.
    Works regardless of whether the message has an existing event — for unparsed/failed messages.
    Optional body: { context: "..." } — prepended to raw_text before re-processing.
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
    context = (body.context or "").strip() if body else ""
    if context:
        m["raw_text"] = f"[Context: {context}]\n{m['raw_text']}"

    async def _run():
        from functools import partial
        from fleet_pipeline.api.pipeline_service import process_raw_text
        from fleet_pipeline.api.routes.fleet import invalidate_kpi_cache

        loop = asyncio.get_event_loop()
        try:
            async with _llm_semaphore:
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


@router.post("/wa-message-deleted", status_code=202)
async def wa_message_deleted(req: WAMessageDeletedRequest):
    """
    Called by Node.js when a WA message is recalled/deleted.
    Marks all events from that wa_message_id as DELETED so they don't affect fleet state.
    """
    import sqlite3
    from fleet_pipeline.config import DB_PATH
    from fleet_pipeline.api.main import ws_manager

    wa_message_id = req.wa_message_id

    deleted_events = 0
    deleted_shift_events = 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        result = conn.execute(
            """UPDATE events SET commit_status='DELETED'
               WHERE wa_message_id=? AND commit_status IN ('COMMITTED','FLAGGED')""",
            (wa_message_id,),
        )
        deleted_events = result.rowcount
        # Also mark shift_events as DELETED
        se_result = conn.execute(
            """UPDATE shift_events SET commit_status='DELETED'
               WHERE wa_message_id=? AND commit_status='COMMITTED'""",
            (wa_message_id,),
        )
        deleted_shift_events = se_result.rowcount
        conn.execute(
            "UPDATE raw_messages SET is_deleted=1 WHERE msg_id=?",
            (wa_message_id,),
        )

    log.info(
        "[DELETE] WA message %s recalled — %d event(s), %d shift_event(s) marked DELETED",
        wa_message_id[:12],
        deleted_events,
        deleted_shift_events,
    )

    # Handle shift_event deletion specially
    if deleted_shift_events > 0:
        with sqlite3.connect(DB_PATH) as _sconn:
            _sconn.row_factory = sqlite3.Row
            se = _sconn.execute(
                "SELECT shift_id, status FROM shift_events WHERE wa_message_id=? AND commit_status='DELETED'",
                (wa_message_id,),
            ).fetchone()
            if se:
                if se["status"] == "SHIFT_START":
                    _sconn.execute(
                        "UPDATE shifts SET ended_at=now WHERE shift_id=?",
                        (se["shift_id"],),
                    )
                    _prev = _sconn.execute(
                        """SELECT shift_id FROM shifts 
                           WHERE ended_at IS NOT NULL 
                           ORDER BY ended_at DESC LIMIT 1"""
                    ).fetchone()
                    if _prev:
                        _sconn.execute(
                            "UPDATE shifts SET ended_at=NULL WHERE shift_id=?",
                            (_prev["shift_id"],),
                        )
                    log.info("[DELETE] SHIFT_START deleted → shifted to previous")
                    await ws_manager.broadcast(
                        "shift_changed", {"reason": "shift_start_deleted"}
                    )
                elif se["status"] == "SHIFT_END":
                    # Shift end deleted → reopen the shift
                    _sconn.execute(
                        "UPDATE shifts SET ended_at=NULL WHERE shift_id=?",
                        (se["shift_id"],),
                    )
                    log.info(
                        "[DELETE] SHIFT_END deleted → shift reopened: %s",
                        se["shift_id"],
                    )
                    await ws_manager.broadcast(
                        "shift_changed", {"reason": "shift_end_deleted"}
                    )

    if deleted_events > 0:
        # Notify control group about the deletion
        try:
            from fleet_pipeline.config import WA_CONTROL_GROUP_JID
            from fleet_pipeline.pipeline.wa_notifier import (
                send_deletion_notification,
                _resolve_group_jid,
            )
            from fleet_pipeline.utils import to_ist

            _notif_jid = _resolve_group_jid(WA_CONTROL_GROUP_JID)
            if _notif_jid:
                with sqlite3.connect(DB_PATH) as _dc:
                    _dc.row_factory = sqlite3.Row
                    _orig = _dc.execute(
                        "SELECT raw_text, sender_name, timestamp_iso FROM raw_messages WHERE msg_id=?",
                        (wa_message_id,),
                    ).fetchone()
                _ts_ist = ""
                _sender = req.deleted_by or ""
                _raw_txt = ""
                if _orig:
                    _raw_txt = _orig["raw_text"] or ""
                    _sender = _orig["sender_name"] or _sender
                    try:
                        _ts_ist = (
                            to_ist(_orig["timestamp_iso"])
                            if _orig["timestamp_iso"]
                            else ""
                        )
                    except Exception:
                        pass
                send_deletion_notification(
                    wa_message_id=wa_message_id,
                    raw_text=_raw_txt,
                    timestamp_ist=_ts_ist,
                    sender_name=_sender,
                    events_deleted=deleted_events,
                    group_jid=_notif_jid,
                )
        except Exception as _exc:
            log.warning("Deletion notification failed: %s", _exc)

    return {
        "deleted": True,
        "wa_message_id": wa_message_id,
        "events_deleted": deleted_events,
    }


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
