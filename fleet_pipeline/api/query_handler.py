"""
Natural language query handler.
Answers fleet questions from DB state using pattern matching + SQL aggregations.

Supported query patterns:
- "where is truck B" / "status of Arjun Novo"
- "fleet summary" / "how many trucks"
- "which trucks are loading / at SOC"
- "average time between LS and LO" (optionally filtered by date range)
- "how long does loading take for truck A"
"""
import re
from datetime import datetime, timezone
from typing import Optional

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db.database import db_conn, get_fleet_state, get_recent_events_for_truck
from fleet_pipeline.pipeline.registries import load_truck_registry, load_site_registry, resolve_truck_id, resolve_site_id

STATUS_LABELS = {
    "ENTER": "entered site",
    "LS":    "loading started",
    "LO":    "loading complete",
    "LEFT":  "departed site",
    "US":    "unloading started",
    "UO":    "unloading complete",
}

# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt_ts(ts: str) -> str:
    if not ts:
        return "unknown time"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%-d %b, %H:%M")
    except Exception:
        return str(ts)[:16]


def _fmt_minutes(seconds: float) -> str:
    m = int(seconds / 60)
    s = int(seconds % 60)
    if m == 0:
        return f"{s}s"
    return f"{m}m {s}s" if s else f"{m}m"


def _find_truck_id(raw: str, db_path: str) -> Optional[str]:
    """Try to resolve a raw string to a truck_id."""
    reg = load_truck_registry(db_path)
    tid = resolve_truck_id(raw, reg)
    if tid:
        return tid
    # try as bare truck_id
    if raw.upper() in reg:
        return raw.upper()
    return None


def _find_truck_from_question(q: str, db_path: str):
    """Extract truck alias from question and return (truck_id, display_alias)."""
    # "arjun novo", "arjun white", "truck b", etc.
    named = re.search(r"arjun\s+\w+", q, re.I)
    if named:
        alias = named.group(0).strip()
        tid = _find_truck_id(alias, db_path)
        return tid, alias

    # "truck X" — only match explicit "truck A/B/..." pattern, not bare letters
    m = re.search(r"\btruck\s+([a-o])\b", q, re.I)
    if m:
        letter = m.group(1).upper()
        tid = _find_truck_id(letter, db_path)
        return tid or ("T" + letter), letter

    # registration / named truck id
    m2 = re.search(r"\b(t[a-z_]+\w*)\b", q, re.I)
    if m2:
        raw = m2.group(1)
        tid = _find_truck_id(raw, db_path)
        return tid, raw

    return None, None


def _parse_date_range(q: str):
    """Extract optional date hints like 'last 7 days', '2025-10-14', 'October'."""
    m = re.search(r"last\s+(\d+)\s+days?", q, re.I)
    if m:
        n = int(m.group(1))
        return f"-{n} days", None
    m2 = re.search(r"(\d{4}-\d{2}-\d{2})", q)
    if m2:
        return m2.group(1), m2.group(1)
    return None, None


# ── query handlers ────────────────────────────────────────────────────────────

def _answer_truck_status(q: str, db_path: str) -> dict:
    truck_id, alias = _find_truck_from_question(q, db_path)
    if not truck_id:
        return {"answer": "Could not identify a truck in your question. Try 'status of truck B' or 'where is Arjun Novo'.", "data": None}

    with db_conn(db_path) as conn:
        events = get_recent_events_for_truck(conn, truck_id, limit=5)

    if not events:
        return {"answer": f"No committed events found for '{alias}' (ID: {truck_id}).", "data": None}

    ev = events[0]
    status_desc = STATUS_LABELS.get(ev["status"], ev["status"])
    site_name   = ev.get("site_name") or ev.get("site_id") or "unknown site"
    ts_str      = _fmt_ts(ev["timestamp_effective"])
    conf        = ev.get("confidence")
    conf_str    = f" (confidence {int(conf*100)}%)" if conf else ""

    lines = [f"{alias} ({truck_id}): {status_desc} at {site_name} as of {ts_str}{conf_str}."]

    if len(events) > 1:
        lines.append(f"\nRecent history ({len(events)} events):")
        for e in events:
            lines.append(f"  {_fmt_ts(e['timestamp_effective'])}  {e['status']}  {e.get('site_id','?')}")

    return {"answer": "\n".join(lines), "data": ev}


def _answer_fleet_summary(db_path: str) -> dict:
    with db_conn(db_path) as conn:
        state = get_fleet_state(conn)

    if not state:
        return {"answer": "No fleet data available yet.", "data": None}

    status_groups: dict = {}
    for ev in state:
        s = ev.get("status", "UNKNOWN")
        status_groups.setdefault(s, []).append(ev.get("truck_name") or ev.get("truck_id"))

    lines = [f"{len(state)} trucks tracked:"]
    for status, trucks in sorted(status_groups.items()):
        label = STATUS_LABELS.get(status, status)
        lines.append(f"  {status} ({label}): {', '.join(trucks)}")

    return {"answer": "\n".join(lines), "data": state}


def _answer_trucks_at_status_or_site(q: str, db_path: str) -> dict:
    # "which trucks are loading" / "trucks at SOC"
    site_reg = load_site_registry(db_path)
    site_id  = None
    for sid, aliases in site_reg.items():
        for a in aliases:
            if a.lower() in q:
                site_id = sid
                break

    status_filter = None
    if re.search(r"\bload", q, re.I):
        status_filter = ("LS", "LO")
    elif re.search(r"\bunload", q, re.I):
        status_filter = ("US", "UO")
    elif re.search(r"\bidle|standing|waiting", q, re.I):
        status_filter = ("ENTER",)
    elif re.search(r"\bin transit|left|depart", q, re.I):
        status_filter = ("LEFT",)

    with db_conn(db_path) as conn:
        state = get_fleet_state(conn)

    matches = []
    for ev in state:
        if site_id and ev.get("site_id") != site_id:
            continue
        if status_filter and ev.get("status") not in status_filter:
            continue
        matches.append(ev)

    if not matches:
        desc = f"at {site_id}" if site_id else ("currently loading" if status_filter == ("LS","LO") else "matching your filter")
        return {"answer": f"No trucks {desc} right now.", "data": []}

    lines = [f"{len(matches)} truck(s) found:"]
    for ev in matches:
        name   = ev.get("truck_name") or ev.get("truck_id")
        status = STATUS_LABELS.get(ev.get("status",""), ev.get("status",""))
        site   = ev.get("site_name") or ev.get("site_id") or "?"
        lines.append(f"  {name}: {status} at {site}")

    return {"answer": "\n".join(lines), "data": matches}


def _answer_cycle_time(q: str, db_path: str) -> dict:
    """Average time between LS and LO (or US/UO) for a truck or all trucks."""
    # Determine which cycle
    if re.search(r"unload", q, re.I):
        start_ev, end_ev = "US", "UO"
        cycle_name = "unloading cycle (US → UO)"
    else:
        start_ev, end_ev = "LS", "LO"
        cycle_name = "loading cycle (LS → LO)"

    # Optional truck filter
    truck_id, alias = _find_truck_from_question(q, db_path)

    # Optional date filter
    date_filter, date_exact = _parse_date_range(q)

    with db_conn(db_path) as conn:
        where = "WHERE commit_status='COMMITTED' AND status IN (?,?)"
        params = [start_ev, end_ev]
        if truck_id:
            where += " AND truck_id=?"
            params.append(truck_id)
        if date_exact:
            where += " AND timestamp_effective LIKE ?"
            params.append(date_exact + "%")
        elif date_filter:
            where += " AND timestamp_effective >= datetime('now', ?)"
            params.append(date_filter)

        rows = conn.execute(
            f"SELECT truck_id, site_id, status, timestamp_effective FROM events {where} ORDER BY truck_id, timestamp_effective",
            params,
        ).fetchall()

    if not rows:
        return {"answer": f"No {cycle_name} data found for your query.", "data": None}

    # Pair LS→LO per truck per site
    durations = []
    pending = {}  # (truck_id, site_id) -> start_ts
    for row in rows:
        tid, sid, status, ts_str = row
        key = (tid, sid)
        if status == start_ev:
            pending[key] = ts_str
        elif status == end_ev and key in pending:
            try:
                t_start = datetime.fromisoformat(pending[key])
                t_end   = datetime.fromisoformat(ts_str)
                secs    = (t_end - t_start).total_seconds()
                if 0 < secs < 86400:  # sanity: within a day
                    durations.append((tid, secs))
            except Exception:
                pass
            del pending[key]

    if not durations:
        return {"answer": f"Could not compute {cycle_name} durations — no complete LS→LO pairs found in data.", "data": None}

    avg_secs = sum(d for _, d in durations) / len(durations)
    min_secs = min(d for _, d in durations)
    max_secs = max(d for _, d in durations)

    scope = f"truck {alias}" if truck_id else "all trucks"
    return {
        "answer": (
            f"{cycle_name.capitalize()} durations for {scope} "
            f"({len(durations)} complete cycles):\n"
            f"  Average : {_fmt_minutes(avg_secs)}\n"
            f"  Min     : {_fmt_minutes(min_secs)}\n"
            f"  Max     : {_fmt_minutes(max_secs)}"
        ),
        "data": {"cycles": len(durations), "avg_seconds": avg_secs, "min": min_secs, "max": max_secs},
    }


# ── router ────────────────────────────────────────────────────────────────────

def answer_query(question: str, db_path: str = DB_PATH) -> dict:
    q = question.lower().strip()

    # Cycle / duration queries
    if re.search(r"average|avg|how long|cycle time|duration|time between", q):
        return _answer_cycle_time(q, db_path)

    # Truck-specific status
    if re.search(r"where is|status of|where('s| is)|what('s| is).*(doing|status|location)", q):
        return _answer_truck_status(q, db_path)

    # Which trucks at a site or in a status
    if re.search(r"which trucks?|trucks? (at|in|that are|currently)|how many trucks?", q):
        result = _answer_trucks_at_status_or_site(q, db_path)
        if result["data"] is not None:
            return result
        # Fall through to summary if no filter matched
        return _answer_fleet_summary(db_path)

    # Fleet summary
    if re.search(r"fleet|summary|overview|all trucks", q):
        return _answer_fleet_summary(db_path)

    # Single-word or short truck mention — try status lookup
    truck_id, alias = _find_truck_from_question(q, db_path)
    if truck_id:
        return _answer_truck_status(q, db_path)

    return {
        "answer": (
            "I can answer questions such as:\n"
            "  - 'Where is truck B?'\n"
            "  - 'Status of Arjun Novo'\n"
            "  - 'Which trucks are loading?'\n"
            "  - 'Fleet summary'\n"
            "  - 'Average loading time for truck A'\n"
            "  - 'Average time between LS and LO last 7 days'"
        ),
        "data": None,
    }
