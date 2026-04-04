"""
Committer — sits between LLM output and the database.

Applies commit rules from prompt.md:
  COMMIT     → events table with commit_status=COMMITTED
  COMMIT_FLAG→ events table with commit_status=FLAGGED + HITL question
  HOLD       → events table with commit_status=HELD + HITL question
  CORRECTION → find previous event(s), update them, set corrects_event_id
  DELETED    → mark referenced events DELETED + HITL question
  confidence < 0.6 → always HITL regardless of commit_recommendation
  truck_id=null → always HOLD + UNKNOWN_TRUCK HITL
  site_id=null AND status requires site → COMMIT_FLAG + UNKNOWN_SITE HITL
"""
import json
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

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
    "yes", "yes ok", "ok", "okay", "ок", "haan", "ha", "ji", "theek hai",
    "tik", "thik", "right", "sahi", "sure", "noted", "👍", "✓", "✔", "k",
}


def _is_substantive_correction(text: str) -> bool:
    """Return True only if raw_text is long/informative enough to be actionable."""
    stripped = (text or "").strip()
    if len(stripped) < 12:
        return False
    return stripped.lower() not in _NOISE_PHRASES


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
        self.wa_message_id = wa_message_id    # original WA msg ID, stored on HITL questions
        self.group_jid = group_jid            # WA group, stored on HITL questions
        self.sender_id = sender_id            # WA sender phone, used for site inference
        if simulation_run_id:
            # Ensure the run record exists so FK constraints don't fire
            with db.db_conn(db_path) as conn:
                existing = conn.execute(
                    "SELECT run_id FROM simulation_runs WHERE run_id=?", (simulation_run_id,)
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
                    conn, level3_result, msg_id, processing_id,
                    commit_rec, overall_confidence, raw_text, summary,
                    resolved_shift_id
                )

            elif msg_type == "TALLY_UPDATE":
                self._handle_tally(conn, level3_result, msg_id, summary)

            elif msg_type == "CORRECTION":
                self._handle_correction(conn, level3_result, msg_id, raw_text, summary)

            elif msg_type in ("NOISE", "QUERY", "OPS_NOTE", "SHIFT_SIGNAL"):
                # Save LLM classification to DB so message map can display it,
                # but DO NOT affect fleet state (commit_status='NOISE' is excluded
                # from all fleet state / KPI queries).
                if msg_id:
                    db.insert_event(conn, {
                        "event_id": str(uuid4()),
                        "msg_id": msg_id,
                        "status": msg_type,
                        "commit_status": "NOISE",
                        "confidence": overall_confidence,
                        "reasoning": level3_result.get("notes") or f"Classified as {msg_type}",
                        "processing_id": processing_id,
                        "simulation_run_id": self.simulation_run_id,
                        "timestamp_effective": timestamp_iso,
                    })

            # Deleted messages: original text is unrecoverable — no HITL question created
            # since there is no context for a human to act on.

        return summary

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

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

    def _handle_status_update(
        self, conn, level3_result, msg_id, processing_id,
        commit_rec, overall_confidence, raw_text, summary,
        resolved_shift_id=None
    ):
        events = level3_result.get("events", []) or []

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
            if status in ("LS", "US") and not ev.get("inferred") and truck_id and site_id:
                if (truck_id, site_id) not in enters_in_batch and self._needs_inferred_enter(conn, truck_id, site_id):
                    # Cap inferred-ENTER confidence at the LS/US event's own confidence.
                    # If the site was inferred (low conf), the ENTER is equally uncertain.
                    enter_conf = min(ev.get("confidence", overall_confidence), 0.88)
                    expanded.append({
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
                    })
                    enters_in_batch.add((truck_id, site_id))
            expanded.append(ev)
        events = expanded

        # Sort events by logical cycle order so that ENTER → LS/US → LO/UO → LEFT
        # are committed in the correct sequence regardless of LLM output order.
        _STATUS_ORDER = {"ENTER": 0, "LS": 1, "US": 1, "LO": 2, "UO": 2, "LEFT": 3}
        events.sort(key=lambda e: _STATUS_ORDER.get(e.get("status", ""), 99))

        for ev in events:
            # Drop events with hallucinated / non-existent status values
            if ev.get("status") not in VALID_STATUSES:
                import warnings
                warnings.warn(
                    f"[Committer] Dropping event with invalid status {ev.get('status')!r} "
                    f"for truck {ev.get('truck_id')!r} — likely LLM hallucination"
                )
                continue

            # Site inference: if site_id is unknown, try same sender's last event in this shift
            if (ev.get("site_id") is None
                    and ev.get("status", "") in SITE_REQUIRED_STATUSES
                    and self.sender_id):
                ev_shift_for_infer = ev.get("shift_id") or resolved_shift_id
                inferred_site = self._infer_site_from_sender_shift(
                    conn, self.sender_id, ev_shift_for_infer
                )
                if inferred_site:
                    ev["site_id"] = inferred_site[0]
                    ev["site_alias"] = inferred_site[1]
                    ev["inferred"] = True
                    # Cap confidence to FLAGGED range so it always gets reviewed
                    ev["confidence"] = min(ev.get("confidence", overall_confidence), 0.72)
                    note = (f"site inferred from sender history in current shift "
                            f"({inferred_site[1] or inferred_site[0]})")
                    ev["reasoning"] = (
                        f"{ev.get('reasoning') or ''}; {note}".lstrip("; ")
                    )

            truck_id = ev.get("truck_id")
            site_id = ev.get("site_id")
            truck_alias = ev.get("truck_alias", "")
            site_alias = ev.get("site_alias", "")
            status = ev.get("status", "")
            ev_confidence = ev.get("confidence", overall_confidence)

            # Determine final commit status for this event
            event_commit_status, questions = self._determine_commit_status(
                commit_rec, overall_confidence, ev_confidence,
                truck_id, truck_alias, site_id, site_alias, status, raw_text
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
            commit_path = (
                "green" if event_commit_status == "COMMITTED" else
                "amber" if event_commit_status == "FLAGGED" else
                "red"
            )

            db.insert_event(conn, {
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
            })

            db.log_audit(conn, "INSERT", "events", event_id,
                         new_value={"commit_status": event_commit_status})

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
            orig_truck_id = ev.get("_orig_truck_id")  # LLM-proposed ID that wasn't in DB
            orig_site_id = ev.get("_orig_site_id")
            for q_type, q_args in questions:
                if q_type == "UNKNOWN_TRUCK":
                    if orig_truck_id:
                        # LLM suggested a specific ID — ask operator to confirm or redirect
                        hitl.create_unrecognized_truck_question(
                            conn, msg_id, event_id, orig_truck_id, truck_alias,
                            ev_reasoning, raw_text, self.simulation_run_id, **wa_ctx
                        )
                    else:
                        hitl.create_unknown_truck_question(
                            conn, msg_id, event_id, truck_alias, raw_text,
                            self.simulation_run_id, **wa_ctx,
                            reasoning=ev_reasoning,
                        )
                elif q_type == "UNKNOWN_SITE":
                    if orig_site_id:
                        hitl.create_unrecognized_site_question(
                            conn, msg_id, event_id, orig_site_id, site_alias,
                            ev_reasoning, raw_text, self.simulation_run_id, **wa_ctx
                        )
                    else:
                        hitl.create_unknown_site_question(
                            conn, msg_id, event_id, site_alias, raw_text,
                            self.simulation_run_id, **wa_ctx,
                            reasoning=ev_reasoning,
                        )
                elif q_type == "LOW_CONFIDENCE":
                    parsed_summary = f"Truck {truck_alias} did {status} at {site_alias or site_id}"
                    hitl.create_low_confidence_question(
                        conn, msg_id, event_id, overall_confidence, parsed_summary,
                        raw_text, self.simulation_run_id, **wa_ctx,
                        reasoning=ev_reasoning,
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
        """
        questions = []
        thresholds = CONFIDENCE_THRESHOLDS

        # Rule: unknown truck → always HOLD
        if truck_id is None:
            questions.append(("UNKNOWN_TRUCK", {}))
            return "HELD", questions

        # Rule: site_id=null AND status requires site → HOLD + UNKNOWN_SITE only
        # Return immediately — low confidence is a consequence of the unknown site,
        # not an independent issue. Don't pile on a second HITL question.
        if site_id is None and status in SITE_REQUIRED_STATUSES:
            questions.append(("UNKNOWN_SITE", {}))
            return "HELD", questions

        # Rule: overall_confidence < HOLD threshold → HOLD + LOW_CONFIDENCE
        if overall_confidence < thresholds["HOLD"]:
            questions.append(("LOW_CONFIDENCE", {}))
            return "HELD", questions

        # Rule: confidence in COMMIT_FLAG range
        if overall_confidence < thresholds["AUTO_COMMIT"]:
            # commit_rec says FLAG
            if commit_rec == "COMMIT_FLAG":
                return "FLAGGED", questions
            if commit_rec == "HOLD":
                questions.append(("LOW_CONFIDENCE", {}))
                return "HELD", questions
            # COMMIT but mid-confidence → FLAG
            return "FLAGGED", questions

        # High confidence (>= AUTO_COMMIT)
        if site_id is None and status in SITE_REQUIRED_STATUSES:
            return "FLAGGED", questions

        return "COMMITTED", questions

    def _handle_tally(self, conn, level3_result, msg_id, summary):
        """
        Store tally for audit/reconciliation only.
        Tallies are human cross-checks and must NOT alter fleet state (no events written,
        no committed counter incremented). They are stored with status=RECEIVED.
        """
        tally_data = level3_result.get("tally") or {}
        tally_id = str(uuid4())
        db.insert_tally(conn, {
            "tally_id": tally_id,
            "msg_id": msg_id,
            "timestamp_iso": level3_result.get("raw_message", {}).get("timestamp_iso"),
            "tally_data": tally_data,
            "commit_status": "RECEIVED",
            "simulation_run_id": self.simulation_run_id,
        })
        summary["tally_received"] = summary.get("tally_received", 0) + 1

    def _handle_correction(self, conn, level3_result, msg_id, raw_text, summary):
        """
        Find the most recent events that this correction refers to and mark them updated.
        Creates a HITL question only if the message is substantive enough to act on.
        """
        events = level3_result.get("events", []) or []
        notes = level3_result.get("notes", "")

        wa_ctx = dict(original_wa_message_id=self.wa_message_id, group_jid=self.group_jid)
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
                    conn, "UPDATE", "events", old_ev["event_id"],
                    old_value={"commit_status": "COMMITTED"},
                    new_value={"commit_status": "DELETED"},
                    triggered_by="pipeline",
                )
            summary["held"] += 1
