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
            f"❓ *Ambiguous correction* — unclear what to change.{why}\n\n"
            f"Original: {orig}\n\n"
            f"Please clarify — reply in any form:\n"
            f"• _D left not B, B is still at KN4_\n"
            f"• Corrected full message  e.g.  `TD LS SOC`\n"
            f"• Which truck / status / site to change and what it should be"
        )

    # Fallback
    return f"❓ *Clarification needed*\n\nOriginal: {orig}"


def _post_send_reply(
    group_jid: str, text: str, quote_id: Optional[str]
) -> Optional[str]:
    """POST to Node.js /send-reply. Returns bot_message_id or None on failure."""
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


def send_summary_to_group(group_jid: str, db_path: str) -> None:
    """
    Generate the current shift summary and post it to the control group.
    Uses the same format as the frontend's copyable summary (emoji-free).
    Always sends to WA_CONTROL_GROUP_JID if configured.
    """
    group_jid = _resolve_group_jid(group_jid)
    if not group_jid:
        return

    import sqlite3
    import urllib.request

    # Generate summary text using the same logic as the analytics endpoint
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Find active shift
            shift = conn.execute(
                """SELECT shift_id, shift_number, shift_name
                   FROM shifts WHERE ended_at IS NULL
                   ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()

            if not shift:
                text = "No active shift found."
            else:
                shift_id = shift["shift_id"]
                shift_num = shift["shift_number"]

                # Aggregate by site
                loaded_by_site = conn.execute(
                    """SELECT e.site_alias, COUNT(*) as cnt
                       FROM events e
                       WHERE e.shift_id=? AND e.status='LO' AND e.commit_status IN ('COMMITTED','FLAGGED')
                       GROUP BY e.site_id ORDER BY cnt DESC""",
                    (shift_id,),
                ).fetchall()

                reached_by_site = conn.execute(
                    """SELECT e.site_alias, COUNT(*) as cnt
                       FROM events e
                       WHERE e.shift_id=? AND e.status='ENTER' AND e.commit_status IN ('COMMITTED','FLAGGED')
                         AND EXISTS (SELECT 1 FROM sites s WHERE s.site_id=e.site_id AND s.site_type='unloading')
                       GROUP BY e.site_id ORDER BY cnt DESC""",
                    (shift_id,),
                ).fetchall()

                unloaded_by_site = conn.execute(
                    """SELECT e.site_alias, COUNT(*) as cnt
                       FROM events e
                       WHERE e.shift_id=? AND e.status='UO' AND e.commit_status IN ('COMMITTED','FLAGGED')
                       GROUP BY e.site_id ORDER BY cnt DESC""",
                    (shift_id,),
                ).fetchall()

                # Per-truck cycle counts
                truck_lo = conn.execute(
                    """SELECT e.truck_alias, COUNT(*) as cnt
                       FROM events e
                       WHERE e.shift_id=? AND e.status='LO' AND e.commit_status IN ('COMMITTED','FLAGGED')
                       GROUP BY e.truck_id ORDER BY e.truck_alias""",
                    (shift_id,),
                ).fetchall()

                truck_uo = conn.execute(
                    """SELECT e.truck_alias, COUNT(*) as cnt
                       FROM events e
                       WHERE e.shift_id=? AND e.status='UO' AND e.commit_status IN ('COMMITTED','FLAGGED')
                       GROUP BY e.truck_id ORDER BY e.truck_alias""",
                    (shift_id,),
                ).fetchall()

                total_loaded = sum(r["cnt"] for r in loaded_by_site)
                total_unloaded = sum(r["cnt"] for r in unloaded_by_site)

                # Build text summary
                lines = [f"-- Shift {shift_num} summary --"]
                lines.append(f"Total Trolleys Loaded (all sites) = {total_loaded}")
                for r in loaded_by_site:
                    lines.append(
                        f"  Trolleys Loaded @{r['site_alias'] or '?'} = {r['cnt']}"
                    )
                lines.append("")
                lines.append(f"Total Trolleys Unloaded (all sites) = {total_unloaded}")
                for r in unloaded_by_site:
                    lines.append(
                        f"  Trolleys Unloaded @{r['site_alias'] or '?'} = {r['cnt']}"
                    )

                # Per-truck cycles
                if truck_lo or truck_uo:
                    lines.append("")
                    lines.append("Per-truck cycles:")
                    all_trucks = {}
                    for r in truck_lo:
                        all_trucks[r["truck_alias"]] = (
                            all_trucks.get(r["truck_alias"], 0) + r["cnt"]
                        )
                    for r in truck_uo:
                        all_trucks[r["truck_alias"]] = (
                            all_trucks.get(r["truck_alias"], 0) + r["cnt"]
                        )
                    cycle_parts = [f"{t}: {c}" for t, c in sorted(all_trucks.items())]
                    lines.append("  " + " | ".join(cycle_parts))

                text = "\n".join(lines)

    except Exception as exc:
        text = f"Error generating summary: {str(exc)[:100]}"
        log.warning("send_summary_to_group: error generating summary: %s", exc)

    # Post to WA group (plain message, no quote)
    _post_send_message(group_jid, text)
    log.info("wa_notifier: posted shift summary to group %s", group_jid)


def _post_send_message(group_jid: str, text: str) -> Optional[str]:
    """POST to Node.js /send-message. Returns bot_message_id or None on failure."""
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
