"""
Level 1 — WhatsApp chat parser.
Parses raw WhatsApp .txt export into structured dicts, one per message.

Usage (importable):
    from fleet_pipeline.pipeline.level1 import parse_chat_file, save_messages

CLI:
    python -m fleet_pipeline.pipeline.level1 chat.txt export_level1.jsonl
"""
import re
import uuid
import json
import sys
from datetime import datetime
from typing import List, Optional

import pytz

WHATSAPP_TIMESTAMP_RE = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2}),\s*(\d{1,2}:\d{2}\s*[APMapm\.]*)\s*-\s*(.*)$"
)

PHONE_RE = re.compile(r"\+?\d[\d ]+\d")


def parse_timestamp(raw_ts: str):
    raw_ts = raw_ts.replace("\u202f", " ")  # normalize narrow NBSP
    dt = datetime.strptime(raw_ts, "%m/%d/%y, %I:%M %p")
    local = pytz.timezone("Asia/Kolkata").localize(dt)
    utc = local.astimezone(pytz.utc)
    return local.isoformat(), utc.isoformat()


def clean_sender_name(full_sender: str):
    s = full_sender.strip()
    s = re.sub(r"\b(Btech|Mech|Engg|ML|PT|DG|ME|CE|CSE|ECE)\b", "", s, flags=re.I)
    s = re.sub(r"\+1\b", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def extract_sender(sender_part: str):
    sender_part = sender_part.rstrip(":")
    m = PHONE_RE.search(sender_part)
    sender_id = None
    if m:
        sender_id = m.group(0).replace(" ", "")
    sender_name = clean_sender_name(sender_part)
    return sender_name, sender_id


def detect_deleted(text: str) -> bool:
    return "This message was deleted" in text or "<Media omitted>" in text


def detect_edited(text: str) -> bool:
    return "This message was edited" in text


def detect_media(text: str) -> Optional[str]:
    if "<Media omitted>" in text:
        return "media"
    if text.startswith("IMG-"):
        return "image"
    if text.startswith("VID-"):
        return "video"
    if text.startswith("AUD-"):
        return "audio"
    return None


def parse_chat_file(path: str, source_file_name: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    messages = []
    buffer = []
    line_no = 0

    def flush_message(buf):
        if not buf:
            return None
        first_line = buf[0]
        m = WHATSAPP_TIMESTAMP_RE.match(first_line)
        if not m:
            return None

        raw_date, raw_time, rest = m.groups()
        raw_timestamp = f"{raw_date}, {raw_time}"

        if ":" in rest:
            sender_part, msg_text_first = rest.split(":", 1)
        else:
            sender_part, msg_text_first = rest, ""

        sender_name, sender_id = extract_sender(sender_part)

        raw_text = msg_text_first
        if len(buf) > 1:
            raw_text += "\n" + "\n".join(buf[1:])
        raw_text = raw_text.strip("\n")

        iso_ts, _ = parse_timestamp(raw_timestamp)

        return {
            "msg_id": str(uuid.uuid4()),
            "source_file": source_file_name,
            "line_no": None,
            "raw_timestamp": raw_timestamp,
            "timestamp_iso": iso_ts,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "raw_text": raw_text,
            "is_edited": detect_edited(raw_text),
            "is_deleted": detect_deleted(raw_text),
            "media_type": detect_media(raw_text),
            "media_info": None,
        }

    for idx, line in enumerate(lines, start=1):
        line_no = idx
        if WHATSAPP_TIMESTAMP_RE.match(line):
            if buffer:
                msg = flush_message(buffer)
                if msg:
                    msg["line_no"] = line_no - len(buffer)
                    messages.append(msg)
            buffer = [line]
        else:
            if buffer:
                buffer.append(line)

    if buffer:
        msg = flush_message(buffer)
        if msg:
            msg["line_no"] = line_no - len(buffer) + 1
            messages.append(msg)

    return messages


def save_messages(messages: List[dict], output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python -m fleet_pipeline.pipeline.level1 <chat.txt> <output.jsonl>")
        sys.exit(1)
    msgs = parse_chat_file(sys.argv[1], sys.argv[1])
    save_messages(msgs, sys.argv[2])
    print(f"Saved {len(msgs)} messages to {sys.argv[2]}")
