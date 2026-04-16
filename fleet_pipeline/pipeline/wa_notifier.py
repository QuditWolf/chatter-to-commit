"""
WA Notifier — sends HITL clarification requests back to the WhatsApp group.

When a HITL question is created, this module calls the Node.js WA listener's
/send-reply endpoint, which sends a bot reply *quoting the original message*.
The operator can then reply to that bot message directly in WA — the reply
is routed back as an HITL answer, bypassing the normal pipeline.

Entry point: notify_hitl_questions(questions, group_jid)
"""

import json
import logging
import os
import urllib.request
from typing import Dict, List, Optional, Any

log = logging.getLogger(__name__)

WA_LISTENER_URL = os.environ.get("WA_LISTENER_URL", "http://localhost:3001")


def _resolve_group_jid(group_jid: Optional[str]) -> Optional[str]:
    """
    Always route bot messages to the control group if configured.
    Falls back to the provided group_jid for backward compatibility.
    """
    control = os.environ.get("WA_CONTROL_GROUP_JID", "")
    return control if control else group_jid


def _format_hitl_message(q_type: str, context: dict, raw_text: str) -> str:
    """Return a concise, actionable WA reply text for each HITL type.

    All messages include:
    - Why it was flagged (from LLM reasoning where available)
    - Clear reply options
    - Explicit note that natural language is accepted
    """
    orig = f'"{raw_text}"' if raw_text else "(unknown message)"
    reasoning = context.get("reasoning", "")

    if q_type == "UNKNOWN_TRUCK":
        alias = context.get("truck_alias", "?")
        why = f"\n_Reason: {reasoning}_" if reasoning else ""
        return (
            f"❓ *Unknown truck* — '{alias}' not in registry.{why}\n\n"
            f"Original: {orig}\n\n"
            f"Reply with:\n"
            f"• Truck code  e.g.  `TB`  (adds '{alias}' as alias)\n"
            f"• Natural language  e.g.  _that's TB_  or  _it's the big Tata_\n"
            f"• Register new:  `new:TX:Display Name:alias`\n"
            f"• Full corrected message  e.g.  `TB LS SOC`"
        )

    if q_type == "UNRECOGNIZED_TRUCK":
        alias = context.get("truck_alias", "?")
        llm_id = context.get("llm_truck_id", "?")
        why = f"\n_Reason: {reasoning}_" if reasoning else ""
        return (
            f"❓ *Unrecognized truck* — '{alias}' identified as '{llm_id}' but not in registry.{why}\n\n"
            f"Original: {orig}\n\n"
            f"Reply with:\n"
            f"• `YES` to add '{llm_id}' as new truck (alias: '{alias}')\n"
            f"• Existing code  e.g.  `TB`  (maps '{alias}' → TB)\n"
            f"• Natural language  e.g.  _that's TB_  or  _add it as T05_\n"
            f"• Register with details:  `new:TX:Display Name:alias`"
        )

    if q_type == "UNKNOWN_SITE":
        alias = context.get("site_alias", "")
        if alias and alias.lower() not in ("none", "null", ""):
            desc = f"'{alias}' not in registry"
        else:
            desc = "could not be determined"
        why = f"\n_Reason: {reasoning}_" if reasoning else ""
        return (
            f"❓ *Unknown site* — {desc}.{why}\n\n"
            f"Original: {orig}\n\n"
            f"Reply with:\n"
            f"• Site code  e.g.  `SOC`  (adds '{alias}' as alias)\n"
            f"• Natural language  e.g.  _that's SOC_  or  _it's the Bagha pit_\n"
            f"• Full corrected message  e.g.  `D LS SOC`\n"
            f"• Register new:  `new:SNAME:Display Name:loading:alias`"
        )

    if q_type == "UNRECOGNIZED_SITE":
        alias = context.get("site_alias", "")
        llm_id = context.get("llm_site_id", "?")
        why = f"\n_Reason: {reasoning}_" if reasoning else ""
        return (
            f"❓ *Unrecognized site* — '{alias}' identified as '{llm_id}' but not in registry.{why}\n\n"
            f"Original: {orig}\n\n"
            f"Reply with:\n"
            f"• `YES` to add '{llm_id}' as new site (alias: '{alias}')\n"
            f"• Existing code  e.g.  `SOC`  (maps '{alias}' → SOC)\n"
            f"• Natural language  e.g.  _that's SOC_  or  _it's the new Bagha pit_\n"
            f"• Full corrected message  e.g.  `D LS SOC`\n"
            f"• Register with details:  `new:SNAME:Display Name:loading:alias`"
        )

    if q_type == "LOW_CONFIDENCE":
        conf = context.get("confidence", 0)
        parsed = context.get("parsed", "")
        pct = f"{int(conf * 100)}%" if conf else "?"
        why = f"\n_Reason: {reasoning}_" if reasoning else ""
        return (
            f"⚠️ *Low confidence ({pct})* — please verify.{why}\n\n"
            f"Original: {orig}\n"
            f"Parsed as: _{parsed}_\n\n"
            f"Reply with:\n"
            f"• `CONFIRM` to accept\n"
            f"• Natural language  e.g.  _actually it's TB not TA_\n"
            f"• Corrected message  e.g.  `TB LS SOC`\n"
            f"• Just the missing piece  e.g.  `SOC`  or  `truck TB`"
        )

    if q_type == "CORRECTION_AMBIGUOUS":
        why = f"\n_Reason: {reasoning}_" if reasoning else ""
        return (
            f"? *Ambiguous correction* — unclear what to change.{why}\n\n"
            f"Original: {orig}\n\n"
            f"Please clarify — reply in any form:\n"
            f"• _D left not B, B is still at KN4_\n"
            f"• Corrected full message  e.g.  `TD LS SOC`\n"
            f"• Which truck / status / site to change and what it should be"
        )

    if q_type == "ENTER_ENTER_GAP":
        gap_min = context.get("gap_minutes", 0)
        prev_ts = context.get("previous_enter_ts", "")
        new_ts = context.get("new_enter_ts", "")
        truck_alias = context.get("truck_alias", "?")
        site_alias = context.get("site_alias", "?")
        return (
            f"? *ENTER->ENTER gap* — {truck_alias} at {site_alias}\n\n"
            f"Gap: {gap_min} min ({prev_ts} -> {new_ts}).\n"
            f"No loading activity recorded after the first ENTER.\n\n"
            f"Reply with:\n"
            f"• YES to confirm first ENTER started a cycle (we'll infer LS/LO/LEFT)\n"
            f"• Corrected events  e.g.  `{truck_alias} LS {site_alias}` to add missing LS\n"
            f"• NO if this is NOT a new cycle"
        )

    # Fallback
    return f"❓ *Clarification needed*\n\nOriginal: {orig}"


def _is_fleet_group(group_jid: str) -> bool:
    """Return True if group_jid is the read-only fleet group (must never receive bot messages)."""
    fleet = os.environ.get("WA_GROUP_JID", "")
    return bool(fleet and group_jid and group_jid == fleet)


def _post_send_reply(
    group_jid: str, text: str, quote_id: Optional[str]
) -> Optional[str]:
    """POST to Node.js /send-reply. Returns bot_message_id or None on failure.
    Hard-blocks any attempt to send to the fleet (read-only) group.
    """
    if _is_fleet_group(group_jid):
        log.error(
            "wa_notifier: BLOCKED send-reply to fleet group %s — fleet group is read-only. "
            "Set WA_CONTROL_GROUP_JID to route bot messages correctly.",
            group_jid,
        )
        return None
    payload = json.dumps(
        {
            "group_jid": group_jid,
            "text": text,
            "quote_id": quote_id,
        }
    ).encode()
    try:
        req = urllib.request.Request(
            f"{WA_LISTENER_URL}/send-reply",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read())
            return body.get("bot_message_id")
    except Exception as exc:
        log.warning("wa_notifier: failed to send WA reply: %s", exc)
        return None


def notify_hitl_questions(
    questions: List[Dict[str, Any]],
    group_jid: str,
    db_path: str,
) -> None:
    """
    For each newly created HITL question, send a WA bot reply quoting the original
    message. Stores the returned bot_wa_message_id back in hitl_queue so that the
    operator's reply can be matched to the question.

    Always sends to WA_CONTROL_GROUP_JID if configured, ignoring group_jid to ensure
    the fleet group (read-only) never receives bot messages.

    questions: list of dicts with keys: question_id, question_type, context,
               raw_text, original_wa_message_id
    """
    group_jid = _resolve_group_jid(group_jid)
    if not group_jid:
        return

    from fleet_pipeline.db import database as db

    for q in questions:
        q_type = q.get("question_type", "")
        context = q.get("context") or {}
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except Exception:
                context = {}

        raw_text = context.get("raw_text", q.get("raw_text", ""))
        original_wa_id = q.get("original_wa_message_id")

        text = _format_hitl_message(q_type, context, raw_text)
        bot_msg_id = _post_send_reply(group_jid, text, original_wa_id)

        if bot_msg_id:
            try:
                with db.db_conn(db_path) as conn:
                    db.set_hitl_bot_wa_message_id(conn, q["question_id"], bot_msg_id)
            except Exception as exc:
                log.warning("wa_notifier: could not store bot_wa_message_id: %s", exc)
            log.info(
                "wa_notifier: sent HITL reply for %s (bot_msg=%s)",
                q["question_id"],
                bot_msg_id,
            )
        else:
            log.warning(
                "wa_notifier: no bot_message_id returned for %s", q["question_id"]
            )


def send_shift_list(group_jid: str, db_path: str) -> None:
    """
    Post a listing of today's and yesterday's shifts (with event counts) to the group.
    Format:
      ── Shifts ──
      📅 Today (2026-04-15):
        _01  active  45 events
        _02  10:30–12:45  23 events
      📅 Yesterday (2026-04-14):
        _03  08:00–11:30  67 events
    """
    group_jid = _resolve_group_jid(group_jid)
    if not group_jid:
        return

    import sqlite3
    import pytz
    from datetime import datetime, timedelta, timezone

    _IST = pytz.timezone("Asia/Kolkata")

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            now_ist = datetime.now(_IST)
            today_str = now_ist.strftime("%Y-%m-%d")
            yest_str = (now_ist - timedelta(days=1)).strftime("%Y-%m-%d")

            rows = conn.execute(
                """SELECT s.shift_name, s.started_at, s.ended_at, s.is_deleted,
                          (SELECT COUNT(*) FROM events e
                           WHERE e.shift_id=s.shift_id
                             AND e.commit_status IN ('COMMITTED','FLAGGED')) AS event_count
                     FROM shifts s
                     WHERE (s.shift_name LIKE ? OR s.shift_name LIKE ?)
                     ORDER BY s.started_at ASC""",
                (f"{today_str}_%", f"{yest_str}_%"),
            ).fetchall()

        def _fmt_time(iso: str) -> str:
            if not iso:
                return "?"
            try:
                dt = datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(_IST).strftime("%H:%M")
            except Exception:
                return "?"

        today_rows = [r for r in rows if r["shift_name"].startswith(today_str)]
        yest_rows = [r for r in rows if r["shift_name"].startswith(yest_str)]

        lines = ["\u2500\u2500 Shifts \u2500\u2500"]
        for label, day_rows, day_str in [
            (f"\U0001f4c5 Today ({today_str})", today_rows, today_str),
            (f"\U0001f4c5 Yesterday ({yest_str})", yest_rows, yest_str),
        ]:
            if not day_rows:
                continue
            lines.append(label + ":")
            for r in day_rows:
                suffix = r["shift_name"][len(day_str) :]  # e.g. "_01"
                start_t = _fmt_time(r["started_at"])
                end_t = "active" if not r["ended_at"] else _fmt_time(r["ended_at"])
                ev = r["event_count"]
                deleted_mark = " (deleted)" if r["is_deleted"] else ""
                lines.append(
                    f"  {suffix}  {start_t}–{end_t}  {ev} events{deleted_mark}"
                )

        if len(lines) == 1:
            lines.append("No shifts today or yesterday.")

        _post_send_message(group_jid, "\n".join(lines))
        log.info("wa_notifier: posted shift list to group %s", group_jid)
    except Exception as exc:
        log.warning("send_shift_list: error: %s", exc)
        _post_send_message(group_jid, f"Error listing shifts: {str(exc)[:80]}")


def send_summary_to_group(
    group_jid: str,
    db_path: str,
    shift_id: str = None,
    shift_suffix: str = None,
) -> None:
    """
    Generate a shift summary and post it to the control group.
    If shift_id is given, summarise that specific shift; otherwise use the
    active shift (or last non-deleted shift as fallback).

    shift_id="last" → force return of the most recently ended shift.
    shift_suffix="_01" → fallback: search for any shift whose shift_id ends with
    that suffix, ordered by started_at DESC (used when exact date+suffix lookup fails).

    Format:
      ── 2026-04-12_06 summary ──
      Total Trolleys Loaded (all sites) = N
        Trolleys Loaded @SITE = N
      Trolleys Reached = N
      Trolleys UNLOADED = N
      Trolleys in Loading = N  (aliases)
    Always sends to WA_CONTROL_GROUP_JID if configured.
    """
    group_jid = _resolve_group_jid(group_jid)
    if not group_jid:
        return

    import sqlite3

    _NOT_DELETED = "(is_deleted IS NULL OR is_deleted = 0)"

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row

            is_last = False
            if shift_id and shift_id.lower() == "last":
                # Explicitly requested last (most recently ended) shift
                shift = conn.execute(
                    f"""SELECT shift_id, shift_number, shift_name, ended_at
                        FROM shifts WHERE {_NOT_DELETED}
                        ORDER BY started_at DESC LIMIT 1"""
                ).fetchone()
                is_last = True
            elif shift_id:
                shift = conn.execute(
                    f"SELECT shift_id, shift_number, shift_name, ended_at FROM shifts WHERE shift_id=? AND {_NOT_DELETED}",
                    (shift_id,),
                ).fetchone()
                if not shift and shift_suffix:
                    # Primary guess (today+suffix) not found — search by suffix across all dates
                    shift = conn.execute(
                        f"""SELECT shift_id, shift_number, shift_name, ended_at
                            FROM shifts WHERE shift_id LIKE ? AND {_NOT_DELETED}
                            ORDER BY started_at DESC LIMIT 1""",
                        (f"%{shift_suffix}",),
                    ).fetchone()
                    if shift:
                        log.info(
                            "send_summary_to_group: suffix fallback matched shift %s",
                            shift["shift_id"],
                        )
                if not shift:
                    _post_send_message(group_jid, f"Shift not found: {shift_id}")
                    return
            else:
                shift = conn.execute(
                    """SELECT shift_id, shift_number, shift_name
                       FROM shifts WHERE ended_at IS NULL AND (is_deleted IS NULL OR is_deleted = 0)
                       ORDER BY started_at DESC LIMIT 1"""
                ).fetchone()

                if not shift:
                    # No active shift - get the last non-deleted shift
                    shift = conn.execute(
                        """SELECT shift_id, shift_number, shift_name, ended_at
                           FROM shifts WHERE (is_deleted IS NULL OR is_deleted = 0)
                           ORDER BY started_at DESC LIMIT 1"""
                    ).fetchone()
                    is_last = True

            if not shift:
                text = "No shift found."
            else:
                shift_id = shift["shift_id"]
                shift_name = shift["shift_name"]
                if is_last:
                    shift_name = f"(last) {shift_name}"

                # Loaded per site
                loaded_by_site = conn.execute(
                    """SELECT COALESCE(e.site_alias, s.display_name, e.site_id, '?') AS site_label,
                              COUNT(*) AS cnt
                       FROM events e
                       LEFT JOIN sites s ON s.site_id = e.site_id
                       WHERE e.shift_id=? AND e.status='LO'
                         AND e.commit_status IN ('COMMITTED','FLAGGED')
                       GROUP BY e.site_id ORDER BY cnt DESC""",
                    (shift_id,),
                ).fetchall()
                total_loaded = sum(r["cnt"] for r in loaded_by_site)

                # Reached unloading sites (ENTER at an unloading-type site)
                reached_row = conn.execute(
                    """SELECT COUNT(*) AS cnt
                       FROM events e
                       JOIN sites s ON s.site_id = e.site_id
                       WHERE e.shift_id=? AND e.status='ENTER'
                         AND s.site_type='unloading'
                         AND e.commit_status IN ('COMMITTED','FLAGGED')""",
                    (shift_id,),
                ).fetchone()
                total_reached = reached_row["cnt"] if reached_row else 0

                # Unloaded
                unloaded_row = conn.execute(
                    """SELECT COUNT(*) AS cnt FROM events
                       WHERE shift_id=? AND status='UO'
                         AND commit_status IN ('COMMITTED','FLAGGED')""",
                    (shift_id,),
                ).fetchone()
                total_unloaded = unloaded_row["cnt"] if unloaded_row else 0

                # Trucks currently in open loading cycle (LS with no subsequent LO/LEFT)
                in_loading_rows = conn.execute(
                    """SELECT e.truck_alias, e.truck_id,
                              MAX(e.timestamp_effective) AS last_ls
                       FROM events e
                       WHERE e.shift_id=? AND e.status='LS'
                         AND e.commit_status IN ('COMMITTED','FLAGGED')
                         AND NOT EXISTS (
                           SELECT 1 FROM events e2
                           WHERE e2.truck_id = e.truck_id
                             AND e2.shift_id = ?
                             AND e2.status IN ('LO','LEFT')
                             AND e2.timestamp_effective >= e.timestamp_effective
                             AND e2.commit_status IN ('COMMITTED','FLAGGED')
                         )
                       GROUP BY e.truck_id
                       ORDER BY e.truck_alias""",
                    (shift_id, shift_id),
                ).fetchall()
                in_loading_aliases = [
                    r["truck_alias"] or r["truck_id"] or "?" for r in in_loading_rows
                ]
                in_loading_count = len(in_loading_aliases)

                # Load cycles per trolley (LO count per truck in this shift)
                load_cycles_rows = conn.execute(
                    """SELECT COALESCE(e.truck_alias, e.truck_id, '?') AS alias,
                              COUNT(*) AS cnt
                       FROM events e
                       WHERE e.shift_id=? AND e.status='LO'
                         AND e.commit_status IN ('COMMITTED','FLAGGED')
                       GROUP BY e.truck_id
                       ORDER BY cnt DESC, alias ASC""",
                    (shift_id,),
                ).fetchall()

                # Build summary text
                lines = [f"\u2500\u2500 {shift_name} summary \u2500\u2500"]
                lines.append(f"*Total Trolleys Loaded (all sites) = {total_loaded}*")
                for r in loaded_by_site:
                    lines.append(f"  Trolleys Loaded @{r['site_label']} = {r['cnt']}")
                lines.append(f"Trolleys Reached = {total_reached}")
                lines.append(f"Trolleys UNLOADED = {total_unloaded}")
                if in_loading_count > 0:
                    aliases_str = ", ".join(in_loading_aliases)
                    lines.append(
                        f"Trolleys in Loading = {in_loading_count}  ({aliases_str})"
                    )
                else:
                    lines.append("Trolleys in Loading = 0")

                if load_cycles_rows:
                    lines.append("")
                    lines.append("Load cycles per trolley:")
                    cycle_parts = [
                        f"{r['alias']} = {r['cnt']}" for r in load_cycles_rows
                    ]
                    lines.append("  " + "   ".join(cycle_parts))

                text = "\n".join(lines)

    except Exception as exc:
        text = f"Error generating summary: {str(exc)[:100]}"
        log.warning("send_summary_to_group: error generating summary: %s", exc)

    _post_send_message(group_jid, text)
    log.info("wa_notifier: posted shift summary to group %s", group_jid)


def send_shift_notification(
    shift: dict,
    action: str,
    group_jid: str,
    db_path: str,
) -> None:
    """
    Send a WhatsApp notification to the control group when a shift starts, ends, or is resumed.

    action: 'start' | 'end' | 'resume'
    shift: dict with at least shift_id, shift_name, default_site_id, default_site_ids

    Stores the returned bot_message_id in shifts.start_notif_bot_msg_id or
    shifts.end_notif_bot_msg_id so that WA replies can be routed back as
    shift control actions (e.g. "no end" → resume).
    """
    group_jid = _resolve_group_jid(group_jid)
    if not group_jid:
        return

    shift_name = shift.get("shift_name") or "?"
    shift_id = shift.get("shift_id")

    # Resolve default site display names
    site_names: list = []
    try:
        import sqlite3 as _sqlite3
        import json as _json

        site_ids: list = []
        raw_ids = shift.get("default_site_ids")
        if raw_ids:
            try:
                site_ids = _json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            except Exception:
                pass
        if not site_ids and shift.get("default_site_id"):
            site_ids = [shift["default_site_id"]]

        if site_ids:
            with _sqlite3.connect(db_path) as _conn:
                _conn.row_factory = _sqlite3.Row
                for sid in site_ids:
                    row = _conn.execute(
                        "SELECT display_name FROM sites WHERE site_id=?", (sid,)
                    ).fetchone()
                    site_names.append(row["display_name"] if row else sid)
    except Exception as exc:
        log.warning("send_shift_notification: could not resolve site names: %s", exc)

    site_line = f"\n📍 Default site: {', '.join(site_names)}" if site_names else ""

    if action == "start":
        text = (
            f"🟢 *Shift {shift_name} started*{site_line}\n\n"
            f"Reply _no start_ to cancel  |  _resume_ to resume previous shift instead"
        )
        col = "start_notif_bot_msg_id"
    elif action == "end":
        text = (
            f"🔴 *Shift {shift_name} ended*\n\n"
            f"Reply _no end_ or _resume_ to continue this shift"
        )
        col = "end_notif_bot_msg_id"
    elif action == "resume":
        text = f"↩ *Shift {shift_name} resumed*{site_line}"
        col = "start_notif_bot_msg_id"
    else:
        return

    bot_msg_id = _post_send_message(group_jid, text)
    if bot_msg_id and shift_id:
        try:
            import sqlite3 as _sqlite3

            with _sqlite3.connect(db_path) as _conn:
                _conn.execute(
                    f"UPDATE shifts SET {col}=? WHERE shift_id=?",
                    (bot_msg_id, shift_id),
                )
                _conn.commit()
        except Exception as exc:
            log.warning("send_shift_notification: could not store bot_msg_id: %s", exc)


def send_commit_notification(event: dict, group_jid: str, db_path: str) -> None:
    """
    Send a WA notification to the control group when an event is auto-committed
    with inferred data or low confidence.  Stores bot_message_id in
    events.commit_notif_bot_msg_id so that operator replies can be routed
    back as corrections.
    """
    group_jid = _resolve_group_jid(group_jid)
    if not group_jid:
        return

    truck = event.get("truck_alias") or event.get("truck_id") or "?"
    status = event.get("status", "?")
    site = event.get("site_alias") or event.get("site_id") or "?"
    conf = event.get("confidence", 0)
    pct = f"{int(conf * 100)}%"
    event_id = event.get("event_id", "")

    tags = []
    if event.get("inferred"):
        tags.append("inferred")
    if conf < 0.85:
        tags.append(f"conf {pct}")
    tag_str = " · ".join(tags)

    # Original message context
    raw_text = (event.get("raw_text") or "").strip()
    timestamp_ist = (event.get("timestamp_ist") or "").strip()
    sender = (event.get("sender_name") or "").strip()
    msg_line = f'\n_Msg: "{raw_text[:80]}"_' if raw_text else ""
    separator = " \u00b7 "
    meta_parts = [p for p in (timestamp_ist, sender) if p]
    meta_line = f"\n_{separator.join(meta_parts)}_" if meta_parts else ""

    text = (
        f"\u2705 Committed _{tag_str}_: *{truck} {status}* @ {site}"
        f"{msg_line}{meta_line}\n"
        f"Reply to correct or clarify"
    )

    bot_msg_id = _post_send_message(group_jid, text)
    if bot_msg_id and event_id:
        try:
            import sqlite3 as _sqlite3

            with _sqlite3.connect(db_path) as _conn:
                _conn.execute(
                    "UPDATE events SET commit_notif_bot_msg_id=? WHERE event_id=?",
                    (bot_msg_id, event_id),
                )
                _conn.commit()
        except Exception as exc:
            log.warning("send_commit_notification: could not store bot_msg_id: %s", exc)


def send_deletion_notification(
    wa_message_id: str,
    raw_text: str,
    timestamp_ist: str,
    sender_name: str,
    events_deleted: int,
    group_jid: str,
) -> None:
    """
    Send a WA notification to the control group when a fleet message is recalled
    and its committed/flagged events are deleted from the state.
    """
    group_jid = _resolve_group_jid(group_jid)
    if not group_jid:
        return

    msg_preview = (raw_text or "").strip()[:80] or "(unknown)"
    separator = " \u00b7 "
    meta_parts = [p for p in (timestamp_ist, sender_name) if p]
    meta_line = f"\n_{separator.join(meta_parts)}_" if meta_parts else ""

    text = (
        f"\U0001f5d1 *Message deleted* \u2014 {events_deleted} event(s) removed\n"
        f'_Msg: "{msg_preview}"_{meta_line}\n'
        f"_(WA ID: {wa_message_id[:16]}\u2026)_"
    )
    _post_send_message(group_jid, text)


def _post_send_message(group_jid: str, text: str) -> Optional[str]:
    """POST to Node.js /send-message. Returns bot_message_id or None on failure.
    Hard-blocks any attempt to send to the fleet (read-only) group.
    """
    if _is_fleet_group(group_jid):
        log.error(
            "wa_notifier: BLOCKED send-message to fleet group %s — fleet group is read-only. "
            "Set WA_CONTROL_GROUP_JID to route bot messages correctly.",
            group_jid,
        )
        return None
    payload = json.dumps(
        {
            "group_jid": group_jid,
            "text": text,
        }
    ).encode()
    try:
        req = urllib.request.Request(
            f"{WA_LISTENER_URL}/send-message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read())
            return body.get("bot_message_id")
    except Exception as exc:
        log.warning("wa_notifier: failed to send WA message: %s", exc)
        return None
