"""
Level 2 — Rule-based enricher.
Reads Level 1 messages and adds heuristic pre-processing:
- rough_trucks, rough_sites, rough_status_keywords detection
- candidate_msg_type classification (includes DELETED fix)
- language tagging
- time-delta features and context windows

Fixes applied vs original:
1. classify_message_type() now checks is_deleted FIRST → returns "DELETED"
2. is_edited check uses level1_msg.get("is_edited") OR regex, not only regex

Usage:
    python -m fleet_pipeline.pipeline.level2 input.jsonl output.jsonl
"""

import re
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

ISO_DT_PARSER = datetime.fromisoformat


@dataclass
class EnricherConfig:
    truck_vocab: Set[str] = field(default_factory=set)
    site_vocab: Set[str] = field(default_factory=set)
    sender_window_minutes: int = 60
    truck_window_minutes: int = 180
    prev_limit: int = 5


class Enricher:
    TRUCK_PATTERNS = [
        re.compile(r"\bNH\s?\d{3,4}\b", re.I),
        re.compile(r"\bKubota\b", re.I),
        re.compile(r"\bArjun\b", re.I),
        re.compile(r"\b([A-G])\b", re.I),
        re.compile(r"\b([A-Z]{1,3}\-?\d{2,5})\b", re.I),
    ]

    STATUS_KEYWORDS = [
        "enter",
        "ls",
        "lo",
        "left",
        "us",
        "uo",
        "loading started",
        "loading over",
        "loading",
        "unloaded",
        "unloading",
    ]

    NOISE_TOKENS = [
        "respond",
        "please respond",
        "where is",
        "ok",
        "thanks",
        "thank you",
        "pls",
        "warmup",
        "system warmup message",
    ]

    OPS_TOKENS = [
        "waiting",
        "ready",
        "kept ready",
        "kept",
        "arrived",
        "available",
        "standing by",
    ]

    TALLY_PATTERNS = [
        re.compile(r"\bTrolleys\b", re.I),
        re.compile(r"={2,}"),
        re.compile(r"\bTrolleys Loaded\b", re.I),
        re.compile(r"\bTrolleys Left\b", re.I),
        re.compile(r"=\s*\d+"),
    ]

    DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
    QUESTION_RE = re.compile(r"\?")
    EDITED_RE = re.compile(r"edited", re.I)

    # Shift signal patterns — matched BEFORE any fleet-event parsing.
    # These messages are control signals, not fleet events.
    SHIFT_SIGNAL_PATTERNS = [
        re.compile(r"\bshift\s+(start|end|over|started|ended|begin|begins)\b", re.I),
        re.compile(r"\bshift\s+\d+\b", re.I),  # "shift 1", "shift 2"
        re.compile(r"\bs[123]\b", re.I),  # "s1", "s2", "s3"
        re.compile(r"\b(pehli|doosri|teesri)\s+pali\b", re.I),  # Hindi shift names
    ]

    # Summary request patterns — standalone messages asking for count/summary
    # or announcing that loading/unloading is over for the shift.
    SUMMARY_REQUEST_PATTERNS = [
        re.compile(r"\btotal\s+count\b", re.I),
        re.compile(r"\b(summary|sumary|summery)\b", re.I),
        re.compile(r"\bsend\s+(total|count|summary)\b", re.I),
        re.compile(r"\bhow\s+many\s+trolleys?\b", re.I),
        re.compile(r"\b(back\s*end|backend)\s+team\b", re.I),
        re.compile(
            r"\bpls?\s+(report|give|send)\s+(the\s+)?(total\s+)?(count|summary)\b", re.I
        ),
        re.compile(
            r"\bcount\s+(yesterday|today|this\s+shift|please|pls|so\s+far)\b", re.I
        ),
        re.compile(r"\btotal\s+count\s*\??$", re.I),
        # Standalone "loading over" / "unloading over" (no truck letter before it)
        re.compile(r"^\s*(loading|unloading)\s+over\s*$", re.I),
        re.compile(
            r"^\s*(loading|unloading)\s+over\s+at\s+", re.I
        ),  # "Loading over at KN4"
    ]

    # Correction keywords — when a reply contains these, it's likely a correction
    CORRECTION_KEYWORDS = [
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

    def __init__(self, config: EnricherConfig):
        self.config = config
        self._truck_vocab_lc = {x.lower() for x in config.truck_vocab}
        self._site_vocab_lc = {x.lower() for x in config.site_vocab}
        self.discovered_trucks: Set[str] = set()
        self.discovered_sites: Set[str] = set()
        self._history: List[Dict[str, Any]] = []

    def enrich_stream(
        self, level1_messages: Iterable[Dict[str, Any]]
    ) -> Iterable[Dict[str, Any]]:
        for msg in level1_messages:
            enriched = self.enrich_message(msg)
            self._history.append(msg)
            yield enriched

    def enrich_message(self, level1_msg: Dict[str, Any]) -> Dict[str, Any]:
        ts = self._parse_iso_ts(level1_msg["timestamp_iso"])
        raw_text = level1_msg.get("raw_text", "") or ""
        sender_id = level1_msg.get("sender_id")

        rough_trucks = self.detect_trucks(raw_text)
        rough_sites = self.detect_sites(raw_text)
        rough_status_keywords = self.detect_status_keywords(raw_text)
        # FIX: pass full level1_msg so deleted/edited flags are checked
        candidate_msg_type = self.classify_message_type(
            level1_msg, raw_text, rough_status_keywords
        )
        lang = self.detect_language(raw_text)

        prev_sender_message_ids = self.prev_messages_from_sender(sender_id, ts)
        prev_truck_message_ids = self.prev_messages_for_trucks(rough_trucks, ts)

        cursor = {
            "time_since_last_sender_msg": self.time_since_last_sender(sender_id, ts),
            "time_since_last_truck_event": self.time_since_last_truck_event(ts),
            "inactivity_window": self.inactivity_window(ts),
        }

        # Reply context enrichment
        reply_context = None
        quoted_id = level1_msg.get("quoted_wa_message_id")
        if quoted_id:
            reply_context = self._get_reply_context(quoted_id)

        return {
            "msg_id": level1_msg["msg_id"],
            "raw": level1_msg,
            "rough_trucks": sorted(set(rough_trucks)),
            "rough_sites": sorted(set(rough_sites)),
            "rough_status_keywords": sorted(set(rough_status_keywords)),
            "candidate_msg_type": candidate_msg_type,
            "lang": lang,
            "prev_sender_message_ids": prev_sender_message_ids,
            "prev_truck_message_ids": prev_truck_message_ids,
            "cursor": cursor,
            "reply_context": reply_context,
        }

    def _get_reply_context(self, quoted_wa_message_id: str) -> Optional[Dict[str, Any]]:
        """Look up the original message and its resolved events by quoted WA message ID.

        Returns dict with:
          - reply_to_msg_id: the quoted WA message ID
          - reply_to_raw_text: original message text
          - reply_to_trucks: list of truck IDs from original events
          - reply_to_sites: list of site IDs from original events
          - reply_to_statuses: list of statuses from original events
        """
        try:
            import sqlite3
            from fleet_pipeline.config import DB_PATH

            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                # Get original raw message
                orig = conn.execute(
                    "SELECT msg_id, raw_text FROM raw_messages WHERE msg_id = ?",
                    (quoted_wa_message_id,),
                ).fetchone()
                if not orig:
                    return None

                # Get events linked to the original message
                events = conn.execute(
                    """SELECT truck_id, truck_alias, status, site_id, site_alias
                       FROM events
                       WHERE msg_id = ? AND commit_status IN ('COMMITTED', 'FLAGGED')
                       ORDER BY rowid ASC""",
                    (orig["msg_id"],),
                ).fetchall()

                trucks = []
                sites = []
                statuses = []
                for ev in events:
                    if ev["truck_id"]:
                        trucks.append(ev["truck_id"])
                    if ev["truck_alias"]:
                        trucks.append(ev["truck_alias"])
                    if ev["site_id"]:
                        sites.append(ev["site_id"])
                    if ev["site_alias"]:
                        sites.append(ev["site_alias"])
                    if ev["status"]:
                        statuses.append(ev["status"])

                return {
                    "reply_to_msg_id": quoted_wa_message_id,
                    "reply_to_raw_text": orig["raw_text"] or "",
                    "reply_to_trucks": sorted(set(trucks)),
                    "reply_to_sites": sorted(set(sites)),
                    "reply_to_statuses": statuses,
                }
        except Exception:
            return None

    def detect_trucks(self, text: str) -> List[str]:
        found: Set[str] = set()

        for token in self.config.truck_vocab:
            if not token:
                continue
            if re.search(r"\b" + re.escape(token) + r"\b", text, re.I):
                found.add(token)

        for pat in self.TRUCK_PATTERNS:
            for m in pat.finditer(text):
                val = m.group(0).strip()
                if val:
                    found.add(val)

        for m in re.finditer(r"\b([A-Z])\b", text):
            found.add(m.group(1).upper())

        canonical: Set[str] = set()
        for f in found:
            if re.fullmatch(r"[A-Za-z]$", f):
                canonical.add(f.upper())
            else:
                canonical.add(f.strip())

        for item in canonical:
            if item.lower() not in self._truck_vocab_lc:
                self.discovered_trucks.add(item)

        return sorted(canonical)

    def detect_sites(self, text: str) -> List[str]:
        found: Set[str] = set()
        for token in self.config.site_vocab:
            if re.search(r"\b" + re.escape(token) + r"\b", text, re.I):
                found.add(token)

        for m in re.finditer(r"\b([A-Za-z]{2,4})\b", text):
            tok = m.group(1)
            if tok.lower() in self._site_vocab_lc:
                continue
            if tok.upper() == tok and len(tok) <= 4:
                found.add(tok)
                if tok.lower() not in self._site_vocab_lc:
                    self.discovered_sites.add(tok)

        return sorted(found)

    def detect_status_keywords(self, text: str) -> List[str]:
        found: Set[str] = set()
        txt_lc = text.lower()
        for kw in self.STATUS_KEYWORDS:
            if kw.lower() in txt_lc:
                found.add(kw.lower())
        for m in re.finditer(r"\b(ls|lo|us|uo|enter|left)\b", text, re.I):
            found.add(m.group(1).lower())
        return sorted(found)

    def is_shift_signal(self, text: str) -> bool:
        """Return True if text is a shift control message, not a fleet event."""
        for pat in self.SHIFT_SIGNAL_PATTERNS:
            if pat.search(text or ""):
                return True
        return False

    def classify_message_type(
        self, level1_msg: Dict[str, Any], text: str, status_keywords: List[str]
    ) -> str:
        txt = text or ""
        txt_lc = txt.lower()

        # FIX 1: deleted messages get their own type
        if level1_msg.get("is_deleted"):
            return "DELETED"

        # Pre-filter: shift signals must never reach fleet-event parsing
        if self.is_shift_signal(txt):
            return "SHIFT_SIGNAL"

        # Summary request detection — standalone "loading over", "unloading over",
        # or messages asking for count/summary. Must NOT contain a truck letter
        # followed by a status keyword (those are fleet events, not summary requests).
        has_truck_status = bool(status_keywords) and bool(self.detect_trucks(txt))
        if not has_truck_status:
            for pat in self.SUMMARY_REQUEST_PATTERNS:
                if pat.search(txt):
                    return "SUMMARY_REQUEST"

        # 1. Query
        if self.QUESTION_RE.search(txt):
            return "QUERY_LIKE"

        # 2. Status
        if status_keywords:
            return "STATUS_LIKE"

        # 3. Tally
        if "\n" in txt and len(txt.splitlines()) >= 2:
            lines = [l.strip() for l in txt.splitlines() if l.strip()]
            numeric_lines = sum(1 for l in lines if re.search(r"\d", l))
            if numeric_lines >= 2:
                return "TALLY_LIKE"
        for pat in self.TALLY_PATTERNS:
            if pat.search(txt):
                return "TALLY_LIKE"

        # FIX 2: check is_edited flag AND text marker
        if level1_msg.get("is_edited") or self.EDITED_RE.search(txt):
            return "CORRECTION_LIKE"

        # 5. Noise
        for tok in self.NOISE_TOKENS:
            if tok in txt_lc:
                return "NOISE_LIKE"

        # 6. OPS_NOTE
        for tok in self.OPS_TOKENS:
            if tok in txt_lc:
                return "OPS_NOTE_LIKE"

        return "NOISE_LIKE"

    def detect_language(self, text: str) -> str:
        if not text.strip():
            return "en"
        has_deva = bool(self.DEVANAGARI_RE.search(text))
        has_latin = bool(re.search(r"[A-Za-z]", text))
        if has_deva and not has_latin:
            return "hi"
        if has_latin and not has_deva:
            return "en"
        return "mixed"

    def _parse_iso_ts(self, iso_ts: str) -> datetime:
        dt = ISO_DT_PARSER(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def inactivity_window(self, current_ts: datetime) -> Optional[int]:
        if not self._history:
            return None
        last_ts = self._parse_iso_ts(self._history[-1]["timestamp_iso"])
        return int((current_ts - last_ts).total_seconds())

    def time_since_last_sender(
        self, sender_id: Optional[str], current_ts: datetime
    ) -> Optional[int]:
        if not sender_id:
            return None
        for prev in reversed(self._history):
            if prev.get("sender_id") == sender_id:
                prev_ts = self._parse_iso_ts(prev["timestamp_iso"])
                return int((current_ts - prev_ts).total_seconds())
        return None

    def time_since_last_truck_event(self, current_ts: datetime) -> Optional[int]:
        for prev in reversed(self._history):
            if self.detect_trucks(prev.get("raw_text", "") or ""):
                prev_ts = self._parse_iso_ts(prev["timestamp_iso"])
                return int((current_ts - prev_ts).total_seconds())
        return None

    def prev_messages_from_sender(
        self, sender_id: Optional[str], current_ts: datetime
    ) -> List[str]:
        if not sender_id:
            return []
        window = timedelta(minutes=self.config.sender_window_minutes)
        out: List[str] = []
        for prev in reversed(self._history):
            if prev.get("sender_id") != sender_id:
                continue
            prev_ts = self._parse_iso_ts(prev["timestamp_iso"])
            if current_ts - prev_ts <= window:
                out.append(prev["msg_id"])
                if len(out) >= self.config.prev_limit:
                    break
        return out

    def prev_messages_for_trucks(
        self, trucks: List[str], current_ts: datetime
    ) -> List[str]:
        if not trucks:
            return []
        truck_window = timedelta(minutes=self.config.truck_window_minutes)
        trucks_lc = {t.lower() for t in trucks}
        out: List[str] = []
        for prev in reversed(self._history):
            prev_ts = self._parse_iso_ts(prev["timestamp_iso"])
            if current_ts - prev_ts > truck_window:
                continue
            prev_trucks_lc = {
                p.lower() for p in self.detect_trucks(prev.get("raw_text", "") or "")
            }
            if prev_trucks_lc & trucks_lc:
                out.append(prev["msg_id"])
                if len(out) >= self.config.prev_limit:
                    break
        return out

    def vocabulary_report(self) -> Dict[str, Any]:
        return {
            "initial_trucks": sorted(self.config.truck_vocab),
            "discovered_trucks": sorted(self.discovered_trucks),
            "final_trucks": sorted(self.config.truck_vocab | self.discovered_trucks),
            "initial_sites": sorted(self.config.site_vocab),
            "discovered_sites": sorted(self.discovered_sites),
            "final_sites": sorted(self.config.site_vocab | self.discovered_sites),
        }


def read_level1_file(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    text_stripped = text.lstrip()
    if text_stripped.startswith("["):
        msgs = json.loads(text)
    else:
        msgs = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                msgs.append(json.loads(line))
    return sorted(msgs, key=lambda m: m["timestamp_iso"])


def save_level2_file(level2_msgs: Iterable[Dict[str, Any]], out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        for m in level2_msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage: python -m fleet_pipeline.pipeline.level2 input.jsonl output.jsonl"
        )
        sys.exit(1)

    # Build vocab from seed data truck/site aliases
    from fleet_pipeline.pipeline.registries import build_vocab_from_seed

    truck_vocab, site_vocab = build_vocab_from_seed()

    config = EnricherConfig(
        truck_vocab=truck_vocab,
        site_vocab=site_vocab,
        sender_window_minutes=60,
        truck_window_minutes=180,
        prev_limit=5,
    )
    level1_msgs = read_level1_file(sys.argv[1])
    enricher = Enricher(config=config)
    enriched = []
    for msg in level1_msgs:
        enriched.append(enricher.enrich_message(msg))
        enricher._history.append(msg)
    save_level2_file(enriched, sys.argv[2])
    print(json.dumps(enricher.vocabulary_report(), indent=2, ensure_ascii=False))
