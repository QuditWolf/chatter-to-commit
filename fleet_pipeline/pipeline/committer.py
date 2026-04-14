"""
Committer — sits between LLM output and the database.

Applies commit rules:
  COMMIT     → events table with commit_status=COMMITTED  (confidence >= 0.85, all known)
  COMMIT_FLAG→ events table with commit_status=FLAGGED + optional HITL question
  HOLD       → NOT USED. Every event is committed as COMMITTED or FLAGGED.
  CORRECTION → find previous event(s), update them, set corrects_event_id
  DELETED    → mark referenced events DELETED + HITL question
  confidence < 0.6 → FLAGGED + LOW_CONFIDENCE HITL (not blocked)
  truck_id=null → FLAGGED + UNKNOWN_TRUCK HITL (committed, human reviews via HITL)
  site_id=null AND status requires site → FLAGGED + UNKNOWN_SITE HITL (after all inference)

Site inference order (when LLM site_id is null):
  1. Sender's most recent event in the same shift (capped at 0.72 confidence)
  2. Shift default site announced at shift start (capped at 0.88 confidence)
  3. If still null → FLAGGED + UNKNOWN_SITE HITL

Duplicate detection (stored as NOISE, excluded from fleet state):
  - LO/UO within 20 min of previous LO/UO for same truck+site → duplicate
  - ENTER within 20 min of LS/US for same truck+site → ordering error
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

log = logging.getLogger(__name__)

from fleet_pipeline.utils import to_ist
from fleet_pipeline.config import CONFIDENCE_THRESHOLDS, DB_PATH
from fleet_pipeline.db import database as db
from fleet_pipeline.pipeline import hitl_queue as hitl
from fleet_pipeline.pipeline.validator import validate_level3_output

# Statuses that logically require a site
SITE_REQUIRED_STATUSES = {"ENTER", "LS", "LO", "LEFT", "US", "UO"}

# All valid truck event statuses — anything outside this set is an LLM hallucination
VALID_STATUSES = {"ENTER", "LS", "LO", "LEFT", "US", "UO", "UNKNOWN"}

# Short confirmations that carry no useful correction context
_NOISE_PHRASES = {
    "yes",
    "yes ok",
    "ok",
    "okay",
    "ок",
    "haan",
    "ha",
    "ji",
    "theek hai",
    "tik",
    "thik",
    "right",
    "sahi",
    "sure",
    "noted",
    "👍",
    "✓",
    "✔",
    "k",
}

# Correction keywords — when a reply contains these, it's likely a correction
_CORRECTION_KEYWORDS = [
    "sorry",
    "not ",
    "actually",
    "correction",
    "it was ",
    "my bad",
    "oops",
    "wrong",
    "mistake",
    "no it was",
    "no,",
    "no.",
]


def _is_substantive_correction(text: str) -> bool:
    """Return True only if raw_text is long/informative enough to be actionable."""
    stripped = (text or "").strip()
    if len(stripped) < 12:
        return False
    return stripped.lower() not in _NOISE_PHRASES


def _parse_ts(ts_str: str):
    """Parse an ISO timestamp string into a timezone-aware datetime, or None."""
    if not ts_str:
        return None
    try:
        from datetime import datetime, timezone
        import pytz

        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.timezone("Asia/Kolkata"))
        return dt
    except Exception:
        return None


def _fmt_ts(dt) -> str:
    """Format a datetime back to an ISO string with IST offset."""
    if dt is None:
        return ""
    try:
        import pytz

        ist = pytz.timezone("Asia/Kolkata")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ist)
        return dt.astimezone(ist).isoformat()
    except Exception:
        return dt.isoformat()


def _spread_same_message_timestamps(events: list) -> None:
    """
    When a single message produces multiple explicit (non-inferred) events for
    the same truck that all share the same timestamp_effective, back-calculate
    earlier events from the last one in the loading/unloading sequence.

    The last (most-advanced) event keeps the message timestamp; earlier events
    are stepped back using the same durations as inferred events:
        … → LS/US  : 5 min before LS (i.e. ENTER–LS gap)
        … → LO/UO  : 30 min before LO (i.e. LS–LO gap)
        … → LEFT   : 5 min before LEFT (i.e. LO–LEFT gap)

    Example — message "LS LO LEFT" at T:
        LEFT  = T
        LO    = T − 5 min
        LS    = T − 5 − 30 = T − 35 min

    This only fires when all events for a truck share the exact same timestamp
    (meaning the LLM copied the message timestamp verbatim for each one), and
    only affects non-inferred events (inferred events are handled separately by
    _apply_inferred_timestamps which resolves against their neighbours).
    """
    from collections import defaultdict
    from datetime import timedelta

    # Gap that precedes each status (how long before this status the prior one was)
    _GAP_BEFORE = {
        "LS": timedelta(minutes=5),
        "US": timedelta(minutes=5),
        "LO": timedelta(minutes=30),
        "UO": timedelta(minutes=30),
        "LEFT": timedelta(minutes=5),
    }
    _STATUS_ORDER = {"ENTER": 0, "LS": 1, "US": 1, "LO": 2, "UO": 2, "LEFT": 3}

    # Group non-inferred, valid-status events by truck_id
    by_truck: dict = defaultdict(list)
    for i, ev in enumerate(events):
        if (
            not ev.get("inferred")
            and ev.get("truck_id")
            and ev.get("status") in _STATUS_ORDER
        ):
            by_truck[ev["truck_id"]].append(ev)

    for truck_id, tevs in by_truck.items():
        if len(tevs) < 2:
            continue
        # Only spread if all share the exact same timestamp_effective
        ts_vals = {ev.get("timestamp_effective", "") for ev in tevs}
        if len(ts_vals) != 1:
            continue
        shared_ts = next(iter(ts_vals))
        anchor = _parse_ts(shared_ts)
        if not anchor:
            continue

        # Sort earliest-first by status order, stable for ties
        tevs_sorted = sorted(
            tevs, key=lambda e: _STATUS_ORDER.get(e.get("status", ""), 99)
        )

        # Last event keeps the anchor; work backwards assigning timestamps
        current_ts = anchor
        for idx in range(len(tevs_sorted) - 1, -1, -1):
            tevs_sorted[idx]["timestamp_effective"] = _fmt_ts(current_ts)
            if idx > 0:
                # All events except the anchor (last) had their time back-calculated.
                # Tag them so the UI can show a "~" approximate-time indicator.
                tevs_sorted[idx]["timestamp_approximate"] = True
                # Gap is determined by THIS (later) event's status
                current_ts = current_ts - _GAP_BEFORE.get(
                    tevs_sorted[idx]["status"], timedelta(minutes=5)
                )


def _apply_inferred_timestamps(events: list) -> None:
    """
    Adjust timestamps on inferred events so they reflect approximate real times
    rather than being identical to the triggering explicit event:

        ENTER (inferred)  →  5 min before the next LS/US
        LS    (inferred)  →  30 min before the next LO/UO
        LO    (inferred)  →  30 min after  the preceding LS/US
        LEFT  (inferred)  →  5 min after   the preceding LO/UO

    Also force inferred=True on any event whose reasoning starts with "inferred:"
    so that LLM-generated gap-fills are always tagged correctly.
    """
    from datetime import timedelta

    # First pass: propagate inferred=True from reasoning text (LLM sometimes forgets the flag).
    for ev in events:
        if (ev.get("reasoning") or "").lstrip().lower().startswith("inferred"):
            ev["inferred"] = True

    # Build index by position for neighbour lookups
    for i, ev in enumerate(events):
        if not ev.get("inferred"):
            continue
        status = ev.get("status", "")

        if status in ("ENTER",):
            # 5 min before the LS/US that follows (look forward)
            for j in range(i + 1, len(events)):
                if events[j].get("status") in ("LS", "US"):
                    anchor = _parse_ts(events[j].get("timestamp_effective", ""))
                    if anchor:
                        ev["timestamp_effective"] = _fmt_ts(
                            anchor - timedelta(minutes=5)
                        )
                    break

        elif status in ("LS", "US"):
            # 30 min before the LO/UO that follows (look forward)
            for j in range(i + 1, len(events)):
                if events[j].get("status") in ("LO", "UO"):
                    anchor = _parse_ts(events[j].get("timestamp_effective", ""))
                    if anchor:
                        ev["timestamp_effective"] = _fmt_ts(
                            anchor - timedelta(minutes=30)
                        )
                    break

        elif status in ("LO", "UO"):
            # 30 min after the LS/US that precedes (look backward)
            for j in range(i - 1, -1, -1):
                if events[j].get("status") in ("LS", "US"):
                    anchor = _parse_ts(events[j].get("timestamp_effective", ""))
                    if anchor:
                        ev["timestamp_effective"] = _fmt_ts(
                            anchor + timedelta(minutes=30)
                        )
                    break

        elif status == "LEFT":
            # 5 min after the LO/UO that precedes (look backward)
            for j in range(i - 1, -1, -1):
                if events[j].get("status") in ("LO", "UO"):
                    anchor = _parse_ts(events[j].get("timestamp_effective", ""))
                    if anchor:
                        ev["timestamp_effective"] = _fmt_ts(
                            anchor + timedelta(minutes=5)
                        )
                    break


class Committer:
    def __init__(
        self,
        db_path: str = DB_PATH,
        simulation_run_id: Optional[str] = None,
        shift_detector=None,
        wa_message_id: Optional[str] = None,
        group_jid: Optional[str] = None,
        sender_id: Optional[str] = None,
    ):
        self.db_path = db_path
        self.simulation_run_id = simulation_run_id
        self.shift_detector = shift_detector
        self.wa_message_id = (
            wa_message_id  # original WA msg ID, stored on HITL questions
        )
        self.group_jid = group_jid  # WA group, stored on HITL questions
        self.sender_id = sender_id  # WA sender phone, used for site inference
        if simulation_run_id:
            # Ensure the run record exists so FK constraints don't fire
            with db.db_conn(db_path) as conn:
                existing = conn.execute(
                    "SELECT run_id FROM simulation_runs WHERE run_id=?",
                    (simulation_run_id,),
                ).fetchone()
                if not existing:
                    db.insert_simulation_run(conn, {"run_id": simulation_run_id})

    def commit(self, level3_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process one Level 3 result dict.
        Writes to DB. Returns a summary dict with counts.
        """
        valid, validation = validate_level3_output(level3_result)

        msg_id = level3_result.get("raw_message", {}).get("msg_id")
        processing_id = level3_result.get("processing_id")
        msg_type = level3_result.get("msg_type", "NOISE")
        commit_rec = level3_result.get("commit_recommendation", "HOLD")
        overall_confidence = level3_result.get("overall_confidence", 0.0)
        raw_text = level3_result.get("raw_message", {}).get("raw_text", "")
        timestamp_iso = level3_result.get("raw_message", {}).get("timestamp_iso", "")

        # Resolve shift_id: prefer LLM's shift_id, fall back to detector, then None
        llm_shift_id = level3_result.get("shift_id")
        if self.shift_detector and not llm_shift_id:
            llm_shift_id = self.shift_detector.process_message(raw_text, timestamp_iso)
        resolved_shift_id = llm_shift_id

        summary = {
            "msg_id": msg_id,
            "msg_type": msg_type,
            "committed": 0,
            "flagged": 0,
            "held": 0,
            "hitl_created": 0,
            "errors": 0 if valid else 1,
        }

        # Extract reply context from L3 result (passed through from L2)
        reply_context = level3_result.get("l3_context_summary", {}).get("reply_context")

        if msg_type == "ERROR":
            summary["errors"] += 1
            return summary

        with db.db_conn(self.db_path) as conn:
            # Insert raw message if present
            raw_msg = level3_result.get("raw_message")
            if raw_msg and msg_id:
                db.insert_raw_message(conn, {**raw_msg, "msg_id": msg_id})

            if msg_type == "STATUS_UPDATE":
                self._handle_status_update(
                    conn,
                    level3_result,
                    msg_id,
                    processing_id,
                    commit_rec,
                    overall_confidence,
                    raw_text,
                    summary,
                    resolved_shift_id,
                    reply_context=reply_context,
                )

            elif msg_type == "TALLY_UPDATE":
                self._handle_tally(conn, level3_result, msg_id, summary)

            elif msg_type == "CORRECTION":
                self._handle_correction(conn, level3_result, msg_id, raw_text, summary)

            elif msg_type in ("NOISE", "QUERY", "OPS_NOTE", "SHIFT_SIGNAL"):
                log.info(
                    "[NOISE] msg_type=%s | %r | reason: %s",
                    msg_type,
                    raw_text[:60],
                    level3_result.get("notes") or f"Classified as {msg_type}",
                )
                # Save LLM classification to DB so message map can display it,
                # but DO NOT affect fleet state (commit_status='NOISE' is excluded
                # from all fleet state / KPI queries).
                if msg_id:
                    db.insert_event(
                        conn,
                        {
                            "event_id": str(uuid4()),
                            "msg_id": msg_id,
                            "status": msg_type,
                            "commit_status": "NOISE",
                            "confidence": overall_confidence,
                            "reasoning": level3_result.get("notes")
                            or f"Classified as {msg_type}",
                            "processing_id": processing_id,
                            "simulation_run_id": self.simulation_run_id,
                            "timestamp_effective": timestamp_iso,
                        },
                    )

            # Deleted messages: original text is unrecoverable — no HITL question created
            # since there is no context for a human to act on.

        # Send WA commit notifications (after DB commit, separate connection)
        for notif_ev in summary.pop("_commit_notifications", []):
            if self.group_jid:
                try:
                    from fleet_pipeline.pipeline.wa_notifier import (
                        send_commit_notification,
                        _resolve_group_jid,
                    )
                    from fleet_pipeline.config import WA_CONTROL_GROUP_JID

                    notify_jid = _resolve_group_jid(
                        WA_CONTROL_GROUP_JID or self.group_jid
                    )
                    if notify_jid:
                        send_commit_notification(notif_ev, notify_jid, self.db_path)
                except Exception as _exc:
                    log.warning("Failed to send commit notification: %s", _exc)

        # Send WA notifications for auto-created trucks (after DB commit)
        for new_truck_id, alias in summary.pop("_new_trucks", []):
            if self.group_jid:
                try:
                    from fleet_pipeline.pipeline.wa_notifier import (
                        _post_send_message,
                        _resolve_group_jid,
                    )

                    notify_jid = _resolve_group_jid(self.group_jid)
                    if notify_jid:
                        msg = (
                            f"\u2705 New trolley auto-added: *{alias}* (ID: {new_truck_id})\n"
                            f"To merge with an existing trolley, reply in this group:\n"
                            f"`MERGE {alias} <existing_id>`"
                        )
                        _post_send_message(notify_jid, msg)
                except Exception as _exc:
                    log.warning("Failed to notify new truck creation: %s", _exc)

        return summary

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    # Minimum time between two LO/UO events for the same truck at the same site.
    # A loading cycle takes at least 20 minutes, so a second LO within this window
    # is almost certainly a duplicate transmission.
    MIN_CYCLE_SECONDS = 20 * 60  # 20 minutes

    def _check_duplicate(
        self,
        conn,
        truck_id: str,
        site_id: Optional[str],
        status: str,
        timestamp_effective: str,
    ) -> Optional[str]:
        """
        Return a duplicate/ordering-error reason string if this event should be
        skipped, or None if it is valid.

        Rules:
        - LO or UO within MIN_CYCLE_SECONDS of a previous LO/UO for the same
          truck+site → duplicate (a full cycle takes >= 20 min).
        - ENTER within MIN_CYCLE_SECONDS after an LS/US for the same truck+site
          → ordering error (truck is mid-load, not starting a new cycle).
        """
        # Only check when truck and site are both known
        if not truck_id or not site_id or not timestamp_effective:
            return None

        from fleet_pipeline.pipeline.shift_detector import _parse_iso

        ts = _parse_iso(timestamp_effective)
        if ts is None:
            return None

        if status in ("LO", "UO"):
            row = conn.execute(
                """SELECT timestamp_effective FROM events
                   WHERE truck_id=? AND site_id=? AND status=?
                     AND commit_status IN ('COMMITTED','FLAGGED')
                   ORDER BY timestamp_effective DESC LIMIT 1""",
                (truck_id, site_id, status),
            ).fetchone()
            if row and row[0]:
                prev_ts = _parse_iso(row[0])
                if prev_ts and (ts - prev_ts).total_seconds() < self.MIN_CYCLE_SECONDS:
                    mins = int((ts - prev_ts).total_seconds() / 60)
                    return (
                        f"duplicate {status}: previous {status} for {truck_id} "
                        f"at {site_id} was only {mins}min ago (min cycle = "
                        f"{self.MIN_CYCLE_SECONDS // 60}min)"
                    )

        elif status == "ENTER":
            # ENTER after LS/US within min cycle window = ordering error
            row = conn.execute(
                """SELECT status, timestamp_effective FROM events
                   WHERE truck_id=? AND site_id=? AND status IN ('LS','US')
                     AND commit_status IN ('COMMITTED','FLAGGED')
                   ORDER BY timestamp_effective DESC LIMIT 1""",
                (truck_id, site_id),
            ).fetchone()
            if row and row[1]:
                prev_ts = _parse_iso(row[1])
                if prev_ts and (ts - prev_ts).total_seconds() < self.MIN_CYCLE_SECONDS:
                    mins = int((ts - prev_ts).total_seconds() / 60)
                    return (
                        f"ordering error: {truck_id} already has open {row[0]} "
                        f"at {site_id} from {mins}min ago — ENTER within "
                        f"{self.MIN_CYCLE_SECONDS // 60}min suggests duplicate, "
                        f"not a new cycle"
                    )

        return None

    def _infer_site_from_sender_shift(
        self, conn, sender_id: str, shift_id: Optional[str]
    ) -> Optional[Tuple[str, str]]:
        """
        Look up the most recent committed/flagged event from the same sender
        in the same shift (not a previous shift). Returns (site_id, site_alias) or None.
        Used to infer location for a new truck with no known site.
        """
        if not sender_id or not shift_id:
            return None
        row = conn.execute(
            """SELECT e.site_id, e.site_alias
               FROM events e
               JOIN raw_messages rm ON e.msg_id = rm.msg_id
               WHERE rm.sender_id = ?
                 AND e.shift_id = ?
                 AND e.site_id IS NOT NULL
                 AND e.commit_status IN ('COMMITTED', 'FLAGGED')
               ORDER BY e.timestamp_effective DESC, e.rowid DESC
               LIMIT 1""",
            (sender_id, shift_id),
        ).fetchone()
        if row and row[0]:
            return (row[0], row[1] or "")
        return None

    def _infer_site_from_any_sender_shift(
        self, conn, shift_id: Optional[str]
    ) -> Optional[Tuple[str, str]]:
        """
        Find the most recent site used by ANY sender in the current shift.
        Used as a lower-confidence fallback when the same-sender inference fails.
        Returns (site_id, site_alias) or None.
        """
        if not shift_id:
            return None
        row = conn.execute(
            """SELECT e.site_id, e.site_alias
               FROM events e
               WHERE e.shift_id = ?
                 AND e.site_id IS NOT NULL
                 AND e.commit_status IN ('COMMITTED', 'FLAGGED')
               ORDER BY e.timestamp_effective DESC, e.rowid DESC
               LIMIT 1""",
            (shift_id,),
        ).fetchone()
        if row and row[0]:
            return (row[0], row[1] or "")
        return None

    def _auto_create_truck_in_db(self, conn, truck_alias: str) -> Optional[str]:
        """
        Auto-create a new truck entry from its alias.
        Returns new truck_id, or None if creation not possible/appropriate.
        """
        if not truck_alias or not truck_alias.strip():
            return None
        new_id = db.auto_create_truck(conn, truck_alias)
        if new_id:
            log.info(
                "[AUTO-TRUCK] Created new truck: %s (alias=%s)", new_id, truck_alias
            )
        return new_id

    def _needs_inferred_enter(self, conn, truck_id: str, site_id: str) -> bool:
        """
        Return True if truck_id has no ENTER event at site_id that is more recent
        than its last LEFT/departure from that site.
        Used to inject an inferred ENTER before LS/US when the LLM missed it.
        """
        if not truck_id or not site_id:
            return False
        # Latest event for this truck at this site
        row = conn.execute(
            """SELECT status FROM events
               WHERE truck_id=? AND site_id=? AND commit_status IN ('COMMITTED','FLAGGED')
               ORDER BY timestamp_effective DESC, rowid DESC LIMIT 1""",
            (truck_id, site_id),
        ).fetchone()
        if row is None:
            # No history at this site at all → need inferred ENTER
            return True
        last_status = row[0]
        # If the last event was already ENTER, LS, or US — no need to inject
        return last_status not in ("ENTER", "LS", "US")

    def _detect_correction_from_reply(
        self, conn, reply_text: str, reply_context: dict
    ) -> Optional[Dict[str, Any]]:
        """
        Detect if a reply message is a correction of the original message
        it's replying to.

        Examples:
          - "Sorry C" replying to "D enter kn4" → correct truck D→C
          - "It was D not Massy" → correct truck Massy→D
          - "Correction *SoC" → correct site

        Returns correction dict or None.
        """
        txt = (reply_text or "").lower().strip()
        if not txt:
            return None

        # Check for correction keywords
        has_correction_kw = any(kw in txt for kw in _CORRECTION_KEYWORDS)
        if not has_correction_kw:
            return None

        orig_trucks = reply_context.get("reply_to_trucks", [])
        orig_sites = reply_context.get("reply_to_sites", [])
        orig_statuses = reply_context.get("reply_to_statuses", [])

        if not orig_trucks and not orig_sites:
            return None

        # Try to extract a truck code from the reply (single letter or known alias)
        truck_match = re.search(r"\b([A-Z])\b", reply_text.upper())
        corrected_truck = None
        if truck_match:
            letter = truck_match.group(1)
            # Look up in registry
            row = conn.execute(
                "SELECT truck_id FROM trucks WHERE aliases LIKE ?",
                (f'%"{letter}"%',),
            ).fetchone()
            if row:
                corrected_truck = row[0]

        # Try to extract a site code
        site_match = re.search(r"\b([A-Z]{2,5})\b", reply_text.upper())
        corrected_site = None
        if site_match:
            site_candidate = site_match.group(1)
            # Skip common non-site words
            if site_candidate not in (
                "NOT",
                "THE",
                "WAS",
                "ITS",
                "SORRY",
                "ACTUALLY",
                "CORRECTION",
            ):
                row = conn.execute(
                    "SELECT site_id FROM sites WHERE aliases LIKE ?",
                    (f'%"{site_candidate}"%',),
                ).fetchone()
                if row:
                    corrected_site = row[0]

        if corrected_truck and orig_trucks:
            return {
                "field": "truck_id",
                "corrected_value": corrected_truck,
                "original_values": orig_trucks,
            }
        elif corrected_site and orig_sites:
            return {
                "field": "site_id",
                "corrected_value": corrected_site,
                "original_values": orig_sites,
            }

        return None

    def _handle_status_update(
        self,
        conn,
        level3_result,
        msg_id,
        processing_id,
        commit_rec,
        overall_confidence,
        raw_text,
        summary,
        resolved_shift_id=None,
        reply_context=None,
    ):
        events = level3_result.get("events", []) or []
        # Message metadata for commit notifications
        _msg_timestamp_iso = level3_result.get("raw_message", {}).get(
            "timestamp_iso", ""
        )
        try:
            _msg_timestamp_ist = (
                to_ist(_msg_timestamp_iso) if _msg_timestamp_iso else ""
            )
        except Exception:
            _msg_timestamp_ist = ""
        _msg_sender = (
            level3_result.get("raw_message", {}).get("sender_name", "")
            or self.sender_id
            or ""
        )

        # Pre-validate all truck_id/site_id values in the LLM output once.
        # Any ID not present in the DB is nulled out so downstream logic
        # (inferred ENTER injection, _determine_commit_status, insert_event)
        # never sees a non-existent FK value — which would raise a FK constraint error.
        # Save the original LLM-proposed ID so we can ask the operator whether to
        # register it or map it to an existing one (UNRECOGNIZED_* HITL questions).
        _valid_trucks = {
            r[0] for r in conn.execute("SELECT truck_id FROM trucks").fetchall()
        }
        _valid_sites = {
            r[0] for r in conn.execute("SELECT site_id FROM sites").fetchall()
        }
        # Build alias→canonical maps so LLM alias leakage (e.g. "a" instead of "TA")
        # is silently corrected rather than triggering a false UNRECOGNIZED_TRUCK.
        _truck_alias_map: dict = {}
        for row in conn.execute("SELECT truck_id, aliases FROM trucks").fetchall():
            for alias in json.loads(row[1] or "[]"):
                _truck_alias_map[alias.lower()] = row[0]
        _site_alias_map: dict = {}
        for row in conn.execute("SELECT site_id, aliases FROM sites").fetchall():
            for alias in json.loads(row[1] or "[]"):
                _site_alias_map[alias.lower()] = row[0]
        for ev in events:
            # Normalise LLM quirks before any validation:
            # 1. String "null" → JSON null for id fields
            for _k in ("truck_id", "site_id"):
                if ev.get(_k) in ("null", "None", ""):
                    ev[_k] = None
            # 2. "timestamp" → "timestamp_effective" (model sometimes uses wrong key)
            if "timestamp_effective" not in ev and "timestamp" in ev:
                ev["timestamp_effective"] = ev["timestamp"]
            # 3. site_id missing but site_alias known → look up via alias map
            if not ev.get("site_id") and ev.get("site_alias"):
                _looked_up = _site_alias_map.get((ev["site_alias"] or "").lower())
                if _looked_up:
                    ev["site_id"] = _looked_up
            # 4. truck_id missing but truck_alias known → look up via alias map
            if not ev.get("truck_id") and ev.get("truck_alias"):
                _looked_up = _truck_alias_map.get((ev["truck_alias"] or "").lower())
                if _looked_up:
                    ev["truck_id"] = _looked_up

            if ev.get("truck_id") and ev["truck_id"] not in _valid_trucks:
                resolved = _truck_alias_map.get(ev["truck_id"].lower())
                if resolved:
                    ev["truck_id"] = resolved
                else:
                    ev["_orig_truck_id"] = ev["truck_id"]
                    ev["truck_id"] = None
            if ev.get("site_id") and ev["site_id"] not in _valid_sites:
                resolved = _site_alias_map.get(ev["site_id"].lower())
                if resolved:
                    ev["site_id"] = resolved
                else:
                    ev["_orig_site_id"] = ev["site_id"]
                    ev["site_id"] = None

        expanded = []
        enters_in_batch: set = set()
        for ev in events:
            truck_id = ev.get("truck_id")
            site_id = ev.get("site_id")
            status = ev.get("status", "")
            ts = ev.get("timestamp_effective", "")
            if status == "ENTER" and truck_id and site_id:
                enters_in_batch.add((truck_id, site_id))
            if (
                status in ("LS", "US")
                and not ev.get("inferred")
                and truck_id
                and site_id
            ):
                if (
                    truck_id,
                    site_id,
                ) not in enters_in_batch and self._needs_inferred_enter(
                    conn, truck_id, site_id
                ):
                    # Cap inferred-ENTER confidence at the LS/US event's own confidence.
                    # If the site was inferred (low conf), the ENTER is equally uncertain.
                    enter_conf = min(ev.get("confidence", overall_confidence), 0.88)
                    expanded.append(
                        {
                            "truck_id": truck_id,
                            "truck_alias": ev.get("truck_alias", ""),
                            "site_id": site_id,
                            "site_alias": ev.get("site_alias", ""),
                            "status": "ENTER",
                            "material": ev.get("material"),
                            "timestamp_effective": ts,
                            "inferred": True,
                            "confidence": enter_conf,
                            "reasoning": f"inferred: {ev.get('truck_alias') or truck_id} reported {status} with no prior ENTER at {ev.get('site_alias') or site_id}",
                            "shift_id": ev.get("shift_id"),
                        }
                    )
                    enters_in_batch.add((truck_id, site_id))
            expanded.append(ev)
        events = expanded

        # Sort events by logical cycle order so that ENTER → LS/US → LO/UO → LEFT
        # are committed in the correct sequence regardless of LLM output order.
        _STATUS_ORDER = {"ENTER": 0, "LS": 1, "US": 1, "LO": 2, "UO": 2, "LEFT": 3}
        events.sort(key=lambda e: _STATUS_ORDER.get(e.get("status", ""), 99))

        # ── Inferred-event timestamp offsetting ──────────────────────────────
        # When a message contains multiple explicit events for the same truck
        # (e.g. "LS LO LEFT"), they all arrive with the same timestamp_effective.
        # Back-calculate earlier events from the last one in the sequence so the
        # timeline is realistic: LEFT=T, LO=T−5min, LS=T−35min, etc.
        _spread_same_message_timestamps(events)

        # Inferred events get approximate timestamps based on their neighbours:
        #   ENTER (inferred)  →  5 min before the LS/US that follows it
        #   LS    (inferred)  →  30 min before the LO/UO that follows it
        #   LO    (inferred)  →  30 min after  the LS/US that precedes it
        #   LEFT  (inferred)  →  5 min after  the LO/UO that precedes it
        # Also force inferred=True on any event whose reasoning begins with
        # "inferred:" so that LLM-generated inferences are always tagged.
        # Runs after _spread_same_message_timestamps so inferred ENTERs anchor
        # against the already-adjusted explicit LS timestamp.
        _apply_inferred_timestamps(events)

        for ev in events:
            # Drop events with hallucinated / non-existent status values
            if ev.get("status") not in VALID_STATUSES:
                import warnings

                warnings.warn(
                    f"[Committer] Dropping event with invalid status {ev.get('status')!r} "
                    f"for truck {ev.get('truck_id')!r} — likely LLM hallucination"
                )
                continue

            # Duplicate / ordering-error check (skip fleet-state change, record as NOISE)
            dup_reason = self._check_duplicate(
                conn,
                ev.get("truck_id"),
                ev.get("site_id"),
                ev.get("status", ""),
                ev.get("timestamp_effective", ""),
            )
            if dup_reason:
                log.info(
                    "[DUPLICATE] Skipping %s %s@%s — %s",
                    status,
                    truck_alias or truck_id,
                    site_id,
                    dup_reason,
                )
                if msg_id:
                    db.insert_event(
                        conn,
                        {
                            "event_id": str(uuid4()),
                            "msg_id": msg_id,
                            "truck_id": ev.get("truck_id"),
                            "truck_alias": ev.get("truck_alias", ""),
                            "status": ev.get("status", ""),
                            "site_id": ev.get("site_id"),
                            "site_alias": ev.get("site_alias", ""),
                            "timestamp_effective": ev.get("timestamp_effective", ""),
                            "inferred": False,
                            "confidence": ev.get("confidence", overall_confidence),
                            "reasoning": dup_reason,
                            "commit_status": "NOISE",
                            "processing_id": processing_id,
                            "simulation_run_id": self.simulation_run_id,
                            "shift_id": ev.get("shift_id") or resolved_shift_id,
                            "commit_path": "grey",
                            "wa_message_id": self.wa_message_id,
                        },
                    )
                continue

            # Site inference — applied only when LLM didn't provide a site.
            # Priority: same sender same shift → any sender same shift → single default site.
            # Multiple default sites → UNKNOWN_SITE HITL.
            if (
                ev.get("site_id") is None
                and ev.get("status", "") in SITE_REQUIRED_STATUSES
            ):
                ev_shift_id = ev.get("shift_id") or resolved_shift_id

                # 1. Same sender, same shift (most reliable)
                inferred_site = None
                if self.sender_id and ev_shift_id:
                    inferred_site = self._infer_site_from_sender_shift(
                        conn, self.sender_id, ev_shift_id
                    )
                    if inferred_site:
                        ev["site_id"] = inferred_site[0]
                        ev["site_alias"] = inferred_site[1]
                        ev["inferred"] = True
                        ev["confidence"] = min(
                            ev.get("confidence", overall_confidence), 0.72
                        )
                        note = f"site inferred from same sender in current shift ({inferred_site[1] or inferred_site[0]})"
                        ev["reasoning"] = f"{ev.get('reasoning') or ''}; {note}".lstrip(
                            "; "
                        )

                # 2. Any sender, same shift (fallback)
                if ev.get("site_id") is None and ev_shift_id:
                    any_site = self._infer_site_from_any_sender_shift(conn, ev_shift_id)
                    if any_site:
                        ev["site_id"] = any_site[0]
                        ev["site_alias"] = any_site[1]
                        ev["inferred"] = True
                        ev["confidence"] = min(
                            ev.get("confidence", overall_confidence), 0.65
                        )
                        note = f"site inferred from recent shift activity ({any_site[1] or any_site[0]})"
                        ev["reasoning"] = f"{ev.get('reasoning') or ''}; {note}".lstrip(
                            "; "
                        )

                # 3. Shift default site (announced at shift start)
                # Filter default sites by operation type:
                #   ENTER/LS/LO/LEFT → loading sites only
                #   US/UO            → unloading sites only
                if ev.get("site_id") is None and ev_shift_id:
                    default_sites = db.get_shift_default_sites(conn, ev_shift_id)
                    if default_sites:
                        _loading_ops = {"ENTER", "LS", "LO", "LEFT"}
                        _unloading_ops = {"US", "UO"}
                        _ev_status = ev.get("status", "")
                        if _ev_status in _loading_ops or _ev_status in _unloading_ops:
                            _target_type = (
                                "loading" if _ev_status in _loading_ops else "unloading"
                            )
                            _typed = []
                            for _ds in default_sites:
                                _sr = conn.execute(
                                    "SELECT site_type FROM sites WHERE site_id=?",
                                    (_ds,),
                                ).fetchone()
                                if _sr and _sr["site_type"] == _target_type:
                                    _typed.append(_ds)
                            if _typed:
                                default_sites = _typed
                            # If no typed matches, leave default_sites as-is (fall to HITL)
                            else:
                                default_sites = []

                    if len(default_sites) == 1:
                        ev["site_id"] = default_sites[0]
                        ev["site_alias"] = ev.get("site_alias") or default_sites[0]
                        ev["inferred"] = True
                        ev["confidence"] = min(
                            ev.get("confidence", overall_confidence), 0.82
                        )
                        note = f"site from shift default ({default_sites[0]})"
                        ev["reasoning"] = f"{ev.get('reasoning') or ''}; {note}".lstrip(
                            "; "
                        )
                    elif len(default_sites) > 1:
                        # Multiple default sites of same type — need operator to clarify
                        # Leave site_id=None so UNKNOWN_SITE HITL fires below
                        note = f"multiple default sites {default_sites} — operator must clarify"
                        ev["reasoning"] = f"{ev.get('reasoning') or ''}; {note}".lstrip(
                            "; "
                        )

            # 4. Reply context fallback: if LLM didn't extract truck/site, use from reply chain
            if reply_context and ev.get("truck_id") is None:
                reply_trucks = reply_context.get("reply_to_trucks") or []
                if len(reply_trucks) == 1:
                    _rt = reply_trucks[0]
                    # Resolve truck alias → truck_id
                    _tr_row = conn.execute(
                        "SELECT truck_id FROM trucks WHERE truck_id=? AND is_active=1",
                        (_rt,),
                    ).fetchone()
                    if not _tr_row:
                        for _tr in conn.execute(
                            "SELECT truck_id, aliases FROM trucks WHERE is_active=1"
                        ):
                            try:
                                _als = json.loads(_tr["aliases"] or "[]")
                                if _rt in _als or _rt.lower() in [
                                    a.lower() for a in _als
                                ]:
                                    _tr_row = _tr
                                    break
                            except Exception:
                                pass
                    if _tr_row:
                        ev["truck_id"] = _tr_row["truck_id"]
                        ev["truck_alias"] = _rt
                        ev["inferred"] = True
                        ev["confidence"] = min(
                            ev.get("confidence", overall_confidence), 0.75
                        )
                        reply_text = reply_context.get("reply_to_raw_text", "")
                        ev["reasoning"] = (
                            f"truck from reply chain ({reply_text[:40]}); {ev.get('reasoning') or ''}".strip(
                                "; "
                            )
                        )

            if reply_context and ev.get("site_id") is None:
                reply_sites = reply_context.get("reply_to_sites") or []
                if len(reply_sites) == 1:
                    _rs = reply_sites[0]
                    _si_row = conn.execute(
                        "SELECT site_id FROM sites WHERE site_id=? AND is_active=1",
                        (_rs,),
                    ).fetchone()
                    if not _si_row:
                        for _si in conn.execute(
                            "SELECT site_id, aliases FROM sites WHERE is_active=1"
                        ):
                            try:
                                _als = json.loads(_si["aliases"] or "[]")
                                if _rs in _als or _rs.lower() in [
                                    a.lower() for a in _als
                                ]:
                                    _si_row = _si
                                    break
                            except Exception:
                                pass
                    if _si_row:
                        ev["site_id"] = _si_row["site_id"]
                        ev["site_alias"] = _rs
                        ev["inferred"] = True
                        ev["confidence"] = min(
                            ev.get("confidence", overall_confidence), 0.75
                        )
                        reply_text = reply_context.get("reply_to_raw_text", "")
                        ev["reasoning"] = (
                            f"site from reply chain ({reply_text[:40]}); {ev.get('reasoning') or ''}".strip(
                                "; "
                            )
                        )

            truck_id = ev.get("truck_id")
            site_id = ev.get("site_id")
            truck_alias = ev.get("truck_alias", "")
            site_alias = ev.get("site_alias", "")
            status = ev.get("status", "")
            ev_confidence = ev.get("confidence", overall_confidence)

            # Reply context: cap confidence at 0.82 so reply-based events get
            # committed but flagged for review (amber highlight in UI).
            if reply_context:
                ev_confidence = min(ev_confidence, 0.82)
                if not ev.get("inferred"):
                    ev["inferred"] = True
                reply_text = reply_context.get("reply_to_raw_text", "")
                if reply_text and "reply to:" not in (ev.get("reasoning") or ""):
                    ev["reasoning"] = (
                        f'reply to: "{reply_text[:60]}"; {ev.get("reasoning") or ""}'
                    ).strip("; ")

            # Determine final commit status for this event
            event_commit_status, questions = self._determine_commit_status(
                commit_rec,
                overall_confidence,
                ev_confidence,
                truck_id,
                truck_alias,
                site_id,
                site_alias,
                status,
                raw_text,
            )

            # Always generate a fresh UUID — never trust the LLM's event_id.
            # GLM-4 consistently returns the RFC-4122 example UUID
            # (550e8400-e29b-41d4-a716-446655440000) which causes INSERT OR REPLACE
            # to overwrite every previous event with the same primary key.
            event_id = str(uuid4())

            # Spec rule: if shift is ambiguous → assign last active shift + flag amber
            ev_shift_id = ev.get("shift_id") or resolved_shift_id
            if not ev_shift_id:
                ev_shift_id = resolved_shift_id  # may still be None
                # Downgrade to FLAGGED if we had COMMITTED but no shift
                if event_commit_status == "COMMITTED":
                    event_commit_status = "FLAGGED"

            # Derive commit_path label for UI
            # "red" (HELD) is no longer produced — every event is COMMITTED or FLAGGED.
            commit_path = (
                "green"
                if event_commit_status == "COMMITTED"
                else "amber"
                if event_commit_status == "FLAGGED"
                else "red"  # fallback, should not occur
            )

            db.insert_event(
                conn,
                {
                    "event_id": event_id,
                    "msg_id": msg_id,
                    "truck_id": truck_id,
                    "truck_alias": truck_alias,
                    "status": status,
                    "site_id": site_id,
                    "site_alias": site_alias,
                    "material": ev.get("material"),
                    "timestamp_effective": ev.get("timestamp_effective", ""),
                    "inferred": ev.get("inferred", False),
                    "confidence": ev_confidence,
                    "reasoning": ev.get("reasoning"),
                    "commit_status": event_commit_status,
                    "processing_id": processing_id,
                    "simulation_run_id": self.simulation_run_id,
                    "shift_id": ev_shift_id,
                    "commit_path": commit_path,
                    "wa_message_id": self.wa_message_id,
                },
            )

            db.log_audit(
                conn,
                "INSERT",
                "events",
                event_id,
                new_value={"commit_status": event_commit_status},
            )

            # Queue WA notification for inferred or low-confidence committed events.
            # Sent after the DB transaction closes (separate connection).
            if (
                event_commit_status == "COMMITTED"
                and (ev_confidence < 0.85 or ev.get("inferred"))
                and not self.simulation_run_id
            ):
                summary.setdefault("_commit_notifications", []).append(
                    {
                        "event_id": event_id,
                        "truck_id": truck_id,
                        "truck_alias": truck_alias,
                        "site_id": site_id,
                        "site_alias": site_alias,
                        "status": status,
                        "confidence": ev_confidence,
                        "inferred": ev.get("inferred", False),
                        "raw_text": raw_text,
                        "timestamp_ist": _msg_timestamp_ist,
                        "sender_name": _msg_sender,
                    }
                )

            _inferred_tag = " [inferred]" if ev.get("inferred") else ""
            log.info(
                "[%s] %s %s → @%s  conf=%.2f%s",
                event_commit_status,
                truck_alias or truck_id or "?",
                status,
                site_alias or site_id or "?",
                ev_confidence,
                _inferred_tag,
            )

            # Tally summary counters
            if event_commit_status == "COMMITTED":
                summary["committed"] += 1
            elif event_commit_status == "FLAGGED":
                summary["flagged"] += 1
            else:
                summary["held"] += 1

            # Create HITL questions
            wa_ctx = dict(
                original_wa_message_id=self.wa_message_id,
                group_jid=self.group_jid,
            )
            ev_reasoning = ev.get("reasoning") or ""
            orig_truck_id = ev.get(
                "_orig_truck_id"
            )  # LLM-proposed ID that wasn't in DB
            orig_site_id = ev.get("_orig_site_id")
            for q_type, q_args in questions:
                if q_type == "UNKNOWN_TRUCK":
                    if orig_truck_id:
                        # LLM suggested a specific ID that wasn't in DB — ask operator to confirm
                        hitl.create_unrecognized_truck_question(
                            conn,
                            msg_id,
                            event_id,
                            orig_truck_id,
                            truck_alias,
                            ev_reasoning,
                            raw_text,
                            self.simulation_run_id,
                            **wa_ctx,
                        )
                    elif truck_alias:
                        # Unknown alias with no LLM ID suggestion — auto-create the truck.
                        # Only skip if we're certain it's ambiguous (no alias at all).
                        new_truck_id = self._auto_create_truck_in_db(conn, truck_alias)
                        if new_truck_id:
                            # Update the event we just inserted to use the new truck_id
                            conn.execute(
                                "UPDATE events SET truck_id=?, commit_status='FLAGGED', commit_path='amber' WHERE event_id=?",
                                (new_truck_id, event_id),
                            )
                            summary.setdefault("_new_trucks", []).append(
                                (new_truck_id, truck_alias)
                            )
                            log.info(
                                "[AUTO-TRUCK] Event updated: truck_id=%s alias=%s",
                                new_truck_id,
                                truck_alias,
                            )
                            # Don't create HITL question — truck is now registered
                            continue
                        else:
                            hitl.create_unknown_truck_question(
                                conn,
                                msg_id,
                                event_id,
                                truck_alias,
                                raw_text,
                                self.simulation_run_id,
                                **wa_ctx,
                                reasoning=ev_reasoning,
                            )
                    else:
                        hitl.create_unknown_truck_question(
                            conn,
                            msg_id,
                            event_id,
                            truck_alias,
                            raw_text,
                            self.simulation_run_id,
                            **wa_ctx,
                            reasoning=ev_reasoning,
                        )
                elif q_type == "UNKNOWN_SITE":
                    if orig_site_id:
                        hitl.create_unrecognized_site_question(
                            conn,
                            msg_id,
                            event_id,
                            orig_site_id,
                            site_alias,
                            ev_reasoning,
                            raw_text,
                            self.simulation_run_id,
                            **wa_ctx,
                        )
                    else:
                        hitl.create_unknown_site_question(
                            conn,
                            msg_id,
                            event_id,
                            site_alias,
                            raw_text,
                            self.simulation_run_id,
                            **wa_ctx,
                            reasoning=ev_reasoning,
                        )
                elif q_type == "LOW_CONFIDENCE":
                    parsed_summary = (
                        f"Truck {truck_alias} did {status} at {site_alias or site_id}"
                    )
                    hitl.create_low_confidence_question(
                        conn,
                        msg_id,
                        event_id,
                        overall_confidence,
                        parsed_summary,
                        raw_text,
                        self.simulation_run_id,
                        **wa_ctx,
                        reasoning=ev_reasoning,
                    )
                log.info(
                    "[HITL] Created %s question for %s",
                    q_type,
                    truck_alias or site_alias or "?",
                )
                summary["hitl_created"] += 1

    def _determine_commit_status(
        self,
        commit_rec: str,
        overall_confidence: float,
        ev_confidence: float,
        truck_id: Optional[str],
        truck_alias: str,
        site_id: Optional[str],
        site_alias: str,
        status: str,
        raw_text: str,
    ) -> Tuple[str, List[Tuple[str, dict]]]:
        """
        Apply commit rules. Returns (commit_status, list_of_hitl_questions_to_create).

        Policy: EVERY event is committed — either COMMITTED or FLAGGED.
        HELD is never used. HITL questions are still created for human review
        but they are non-blocking (operator can correct via HITL answer later).
        """
        questions = []
        thresholds = CONFIDENCE_THRESHOLDS

        # Unknown truck → FLAGGED + HITL for review (not blocked)
        if truck_id is None:
            questions.append(("UNKNOWN_TRUCK", {}))
            return "FLAGGED", questions

        # Unknown site (status requires one) → FLAGGED + HITL for review
        # Single question — low confidence is a consequence of the unknown site.
        if site_id is None and status in SITE_REQUIRED_STATUSES:
            questions.append(("UNKNOWN_SITE", {}))
            return "FLAGGED", questions

        # Everything with truck_id + site_id → COMMITTED immediately.
        # Inferred or low-confidence events are flagged via WA control group notification
        # ("I committed X (inferred/conf 72%) — reply to correct") rather than gating
        # the commit behind HITL confirmation.
        return "COMMITTED", questions

    def _handle_tally(self, conn, level3_result, msg_id, summary):
        """
        Store tally for audit/reconciliation only.
        Tallies are human cross-checks and must NOT alter fleet state (no events written,
        no committed counter incremented). They are stored with status=RECEIVED.
        """
        tally_data = level3_result.get("tally") or {}
        tally_id = str(uuid4())
        db.insert_tally(
            conn,
            {
                "tally_id": tally_id,
                "msg_id": msg_id,
                "timestamp_iso": level3_result.get("raw_message", {}).get(
                    "timestamp_iso"
                ),
                "tally_data": tally_data,
                "commit_status": "RECEIVED",
                "simulation_run_id": self.simulation_run_id,
            },
        )
        summary["tally_received"] = summary.get("tally_received", 0) + 1

    def _handle_correction(self, conn, level3_result, msg_id, raw_text, summary):
        """
        Find the most recent events that this correction refers to and mark them updated.
        Creates a HITL question only if the message is substantive enough to act on.
        """
        events = level3_result.get("events", []) or []
        notes = level3_result.get("notes", "")

        wa_ctx = dict(
            original_wa_message_id=self.wa_message_id, group_jid=self.group_jid
        )
        if not events:
            if _is_substantive_correction(raw_text):
                hitl.create_correction_ambiguous_question(
                    conn, msg_id, raw_text, notes, self.simulation_run_id, **wa_ctx
                )
                summary["hitl_created"] += 1
            return

        for ev in events:
            truck_id = ev.get("truck_id")
            if not truck_id:
                if _is_substantive_correction(raw_text):
                    hitl.create_correction_ambiguous_question(
                        conn, msg_id, raw_text, notes, self.simulation_run_id, **wa_ctx
                    )
                    summary["hitl_created"] += 1
                continue

            # Find the most recent COMMITTED event for this truck
            recent = db.get_recent_events_for_truck(conn, truck_id, limit=1)
            if recent:
                old_ev = recent[0]
                db.update_event_status(conn, old_ev["event_id"], "DELETED")
                db.log_audit(
                    conn,
                    "UPDATE",
                    "events",
                    old_ev["event_id"],
                    old_value={"commit_status": "COMMITTED"},
                    new_value={"commit_status": "DELETED"},
                    triggered_by="pipeline",
                )
            summary["held"] += 1
