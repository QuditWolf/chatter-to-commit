"""
Analytics routes — aggregated stats for the dashboard.
GET /analytics/summary    — KPIs, status breakdown, site activity, confidence dist
GET /analytics/timeline   — events per hour for the most recent simulation run
GET /analytics/fleet-map  — current truck positions for map overlay
"""

import json
from collections import defaultdict
from fastapi import APIRouter, Query

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db.database import db_conn

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
def analytics_summary():
    with db_conn(DB_PATH) as conn:
        # KPIs
        totals = dict(
            conn.execute(
                "SELECT commit_status, COUNT(*) as n FROM events GROUP BY commit_status"
            ).fetchall()
            or []
        )
        total_events = sum(totals.values())
        committed = totals.get("COMMITTED", 0)
        flagged = totals.get("FLAGGED", 0)
        held = totals.get("HELD", 0)

        trucks_active = conn.execute(
            "SELECT COUNT(DISTINCT truck_id) FROM events WHERE commit_status='COMMITTED' AND truck_id IS NOT NULL"
        ).fetchone()[0]

        hitl_open = conn.execute(
            "SELECT COUNT(*) FROM hitl_queue WHERE status='OPEN'"
        ).fetchone()[0]

        # Status breakdown
        status_rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM events WHERE commit_status='COMMITTED' GROUP BY status"
        ).fetchall()
        status_breakdown = {r[0]: r[1] for r in status_rows}

        # Site activity
        site_rows = conn.execute(
            """SELECT s.display_name, COUNT(*) as n
               FROM events e LEFT JOIN sites s ON e.site_id=s.site_id
               WHERE e.commit_status='COMMITTED' AND e.site_id IS NOT NULL
               GROUP BY e.site_id ORDER BY n DESC LIMIT 10"""
        ).fetchall()
        site_activity = {r[0]: r[1] for r in site_rows}

        # Confidence buckets
        conf_rows = conn.execute(
            "SELECT confidence FROM events WHERE confidence IS NOT NULL"
        ).fetchall()
        buckets = {"<0.6": 0, "0.6-0.75": 0, "0.75-0.85": 0, "≥0.85": 0}
        for (c,) in conf_rows:
            if c < 0.6:
                buckets["<0.6"] += 1
            elif c < 0.75:
                buckets["0.6-0.75"] += 1
            elif c < 0.85:
                buckets["0.75-0.85"] += 1
            else:
                buckets["≥0.85"] += 1

        # HITL question type breakdown
        hitl_rows = conn.execute(
            "SELECT question_type, COUNT(*) as n FROM hitl_queue GROUP BY question_type"
        ).fetchall()
        hitl_types = {r[0]: r[1] for r in hitl_rows}

        # Tallies count
        tally_count = conn.execute("SELECT COUNT(*) FROM tallies").fetchone()[0]

    commit_rate = round(committed / total_events, 3) if total_events else 0
    return {
        "kpis": {
            "total_events": total_events,
            "committed": committed,
            "flagged": flagged,
            "held": held,
            "trucks_active": trucks_active,
            "hitl_open": hitl_open,
            "commit_rate": commit_rate,
            "tally_count": tally_count,
        },
        "status_breakdown": status_breakdown,
        "site_activity": site_activity,
        "confidence_buckets": buckets,
        "hitl_types": hitl_types,
    }


@router.get("/timeline")
def analytics_timeline():
    """Events committed per hour (for today or most recent data)."""
    with db_conn(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT substr(timestamp_effective, 1, 13) as hour_key, COUNT(*) as n
               FROM events
               WHERE commit_status='COMMITTED' AND timestamp_effective IS NOT NULL
               GROUP BY hour_key
               ORDER BY hour_key"""
        ).fetchall()
    return {"timeline": [{"hour": r[0], "count": r[1]} for r in rows]}


@router.get("/historical")
def analytics_historical():
    """All-time historical analytics: calendar counts, hourly patterns, per-truck/site stats."""
    with db_conn(DB_PATH) as conn:
        # Daily event counts
        daily = conn.execute("""
            SELECT substr(timestamp_effective,1,10) as d, COUNT(*) as n
            FROM events WHERE commit_status='COMMITTED' AND timestamp_effective IS NOT NULL
              AND length(timestamp_effective) >= 10
            GROUP BY d ORDER BY d
        """).fetchall()

        # Hourly × day-of-week heatmap
        hourly_dow = conn.execute("""
            SELECT strftime('%w', substr(timestamp_effective,1,10)) as dow,
                   CAST(substr(timestamp_effective,12,2) AS INTEGER) as hr,
                   COUNT(*) as n
            FROM events WHERE commit_status='COMMITTED' AND timestamp_effective IS NOT NULL
              AND length(timestamp_effective) >= 13
            GROUP BY dow, hr ORDER BY dow, hr
        """).fetchall()

        # Monthly totals
        monthly = conn.execute("""
            SELECT substr(timestamp_effective,1,7) as m, COUNT(*) as n
            FROM events WHERE commit_status='COMMITTED' AND timestamp_effective IS NOT NULL
            GROUP BY m ORDER BY m
        """).fetchall()

        # Loading cycles per truck (LS → LO pairing)
        truck_cycles = conn.execute("""
            SELECT t.display_name, t.truck_id,
                   COUNT(*) as cycles,
                   ROUND(AVG((strftime('%s',lo.timestamp_effective)-strftime('%s',ls.timestamp_effective))/60.0),1) as avg_min,
                   ROUND(MIN((strftime('%s',lo.timestamp_effective)-strftime('%s',ls.timestamp_effective))/60.0),1) as min_min,
                   ROUND(MAX((strftime('%s',lo.timestamp_effective)-strftime('%s',ls.timestamp_effective))/60.0),1) as max_min
            FROM events ls
            JOIN events lo ON lo.truck_id=ls.truck_id AND lo.site_id=ls.site_id
                          AND lo.status='LO' AND lo.commit_status='COMMITTED'
                          AND lo.timestamp_effective=(
                            SELECT MIN(x.timestamp_effective) FROM events x
                            WHERE x.truck_id=ls.truck_id AND x.site_id=ls.site_id
                              AND x.status='LO' AND x.commit_status='COMMITTED'
                              AND x.timestamp_effective>ls.timestamp_effective
                          )
            JOIN trucks t ON t.truck_id=ls.truck_id
            WHERE ls.status='LS' AND ls.commit_status='COMMITTED'
              AND (strftime('%s',lo.timestamp_effective)-strftime('%s',ls.timestamp_effective)) BETWEEN 60 AND 43200
            GROUP BY ls.truck_id ORDER BY cycles DESC
        """).fetchall()

        # Per-site stats
        site_stats = conn.execute("""
            SELECT s.display_name, s.site_type,
                   COUNT(*) as total_events,
                   COUNT(DISTINCT DATE(e.timestamp_effective)) as active_days,
                   SUM(CASE WHEN e.status='LS' THEN 1 ELSE 0 END) as load_starts,
                   COUNT(DISTINCT e.truck_id) as unique_trucks
            FROM events e JOIN sites s ON e.site_id=s.site_id
            WHERE e.commit_status='COMMITTED'
            GROUP BY e.site_id ORDER BY total_events DESC
        """).fetchall()

        # Correction / noise breakdown
        msg_stats = conn.execute("""
            SELECT
              COUNT(*) as total,
              SUM(CASE WHEN is_edited=1 THEN 1 ELSE 0 END) as edited,
              SUM(CASE WHEN is_deleted=1 THEN 1 ELSE 0 END) as deleted
            FROM raw_messages
        """).fetchone()

    total_cycles = sum(r[2] for r in truck_cycles)
    avg_cycle = (
        round(sum(r[2] * (r[3] or 0) for r in truck_cycles) / total_cycles, 1)
        if total_cycles
        else 0
    )

    return {
        "kpis": {
            "active_days": len(daily),
            "total_events": sum(r[1] for r in daily),
            "total_cycles": total_cycles,
            "avg_cycle_min": avg_cycle,
        },
        "daily_counts": {r[0]: r[1] for r in daily},
        "hourly_dow": [{"dow": int(r[0]), "hr": r[1], "n": r[2]} for r in hourly_dow],
        "monthly": {r[0]: r[1] for r in monthly},
        "truck_cycles": [
            {
                "name": r[0],
                "id": r[1],
                "cycles": r[2],
                "avg_min": r[3],
                "min_min": r[4],
                "max_min": r[5],
            }
            for r in truck_cycles
        ],
        "site_stats": [
            {
                "name": r[0],
                "type": r[1],
                "total_events": r[2],
                "active_days": r[3],
                "load_starts": r[4],
                "unique_trucks": r[5],
            }
            for r in site_stats
        ],
        "message_stats": {
            "total": msg_stats[0] if msg_stats else 0,
            "edited": msg_stats[1] if msg_stats else 0,
            "deleted": msg_stats[2] if msg_stats else 0,
        },
    }


@router.get("/site-state")
def site_state():
    """
    Per-site operational state + totals for the CURRENT SHIFT.

    State: current position of each truck at each site (based on latest committed event in current shift):
      - entered     : status=ENTER (arrived, not yet loading/unloading)
      - in_progress : status=LS or US (operation started, not over)
      - op_over     : status=LO or UO (over but not yet LEFT)

    Totals:
      - total_lo    : LO events committed at this site in current shift
      - total_left  : LEFT events committed at this site in current shift
    """
    with db_conn(DB_PATH) as conn:
        # Active shift
        shift_row = conn.execute(
            "SELECT shift_id FROM shifts WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        shift_id = shift_row["shift_id"] if shift_row else None

        shift_filter = "AND e.shift_id = ?" if shift_id else ""
        bind = [shift_id] if shift_id else []

        # Current fleet state (latest committed event per truck in current shift)
        state_rows = conn.execute(
            f"""SELECT e.truck_id, COALESCE(t.display_name, e.truck_alias, e.truck_id) as truck_name,
                      e.status, e.site_id, COALESCE(s.display_name, e.site_id) as site_name, s.site_type
               FROM events e
               LEFT JOIN trucks t ON e.truck_id = t.truck_id
               LEFT JOIN sites  s ON e.site_id  = s.site_id
               WHERE e.commit_status = 'COMMITTED'
                 AND e.truck_id IS NOT NULL
                 AND e.site_id  IS NOT NULL
                 {shift_filter}
                 AND e.rowid IN (
                   SELECT MAX(rowid) FROM events
                   WHERE commit_status='COMMITTED' AND truck_id IS NOT NULL
                     AND site_id IS NOT NULL
                     {"AND shift_id = ?" if shift_id else ""}
                   GROUP BY truck_id
                 )""",
            bind + bind,
        ).fetchall()

        # Totals per site in current shift
        totals_rows = conn.execute(
            f"""SELECT site_id,
                      SUM(CASE WHEN status='LO'   THEN 1 ELSE 0 END) as total_lo,
                      SUM(CASE WHEN status='LEFT'  THEN 1 ELSE 0 END) as total_left,
                      SUM(CASE WHEN status='UO'   THEN 1 ELSE 0 END) as total_uo
               FROM events
               WHERE commit_status='COMMITTED' AND site_id IS NOT NULL
                 AND truck_id IS NOT NULL
                 {shift_filter.replace("e.", "")}
               GROUP BY site_id""",
            bind,
        ).fetchall()

        # Sites with events in current shift (only show sites relevant to this shift)
        all_sites = conn.execute(
            f"""SELECT DISTINCT s.site_id, COALESCE(s.display_name, s.site_id) as display_name, s.site_type
                FROM events e JOIN sites s ON e.site_id = s.site_id
                WHERE e.commit_status='COMMITTED' AND e.truck_id IS NOT NULL
                  {"AND e.shift_id = ?" if shift_id else ""}
                UNION
                SELECT s.site_id, COALESCE(s.display_name, s.site_id) as display_name, s.site_type
                FROM sites s
                WHERE s.is_active=1
                  AND (s.site_type = 'loading' OR s.site_type = 'unloading')
                ORDER BY display_name""",
            bind,
        ).fetchall()

    # Build totals index
    totals_by_site = {}
    for r in totals_rows:
        totals_by_site[r[0]] = {
            "total_lo": r[1] or 0,
            "total_left": r[2] or 0,
            "total_uo": r[3] or 0,
        }

    # Build state index: site_id → {entered, in_progress, op_over}
    state_by_site = defaultdict(
        lambda: {
            "entered": [],
            "in_progress": [],
            "op_over": [],
            "site_name": "",
            "site_type": "",
        }
    )
    for r in state_rows:
        truck_id, truck_name, status, site_id, site_name, site_type = r
        entry = state_by_site[site_id]
        entry["site_name"] = site_name
        entry["site_type"] = site_type or ""
        label = truck_name or truck_id
        if status == "ENTER":
            entry["entered"].append(label)
        elif status in ("LS", "US"):
            entry["in_progress"].append(label)
        elif status in ("LO", "UO"):
            entry["op_over"].append(label)

    # Merge all sites
    result = []
    seen = set()
    # First: sites that have state or totals data
    for site_id, state in state_by_site.items():
        seen.add(site_id)
        t = totals_by_site.get(site_id, {"total_lo": 0, "total_left": 0, "total_uo": 0})
        result.append(
            {
                "site_id": site_id,
                "site_name": state["site_name"] or site_id,
                "site_type": state["site_type"],
                "state": {
                    "entered": state["entered"],
                    "in_progress": state["in_progress"],
                    "op_over": state["op_over"],
                },
                "totals": t,
            }
        )
    # Then: sites from registry that had no current activity but have historical totals
    for r in all_sites:
        sid, sname, stype = r[0], r[1], r[2]
        if sid not in seen:
            t = totals_by_site.get(sid, {"total_lo": 0, "total_left": 0, "total_uo": 0})
            if t["total_lo"] > 0 or t["total_left"] > 0:
                result.append(
                    {
                        "site_id": sid,
                        "site_name": sname or sid,
                        "site_type": stype or "",
                        "state": {"entered": [], "in_progress": [], "op_over": []},
                        "totals": t,
                    }
                )

    # Sort: active sites first (have current state), then by name
    result.sort(
        key=lambda x: (
            0
            if (
                x["state"]["entered"]
                or x["state"]["in_progress"]
                or x["state"]["op_over"]
            )
            else 1,
            x["site_name"],
        )
    )
    return {"sites": result}


@router.get("/shift-summary")
def shift_summary(shift_id: str = Query("")):
    """
    Shift summary for the operator report card.

    Returns:
    - loaded_by_site:   {site_id: {name, count}} — LO events at loading sites this shift
    - reached_by_site:  {site_id: {name, count}} — ENTER events at unloading sites this shift
    - unloaded_by_site: {site_id: {name, count}} — UO events at unloading sites this shift
    - in_loading:       list of truck aliases currently LS at a loading site
    - in_unloading:     list of truck aliases currently US at an unloading site
    - truck_cycles:     [{alias, truck_id, loads, unloads}] — per-truck LO/UO counts this shift
    - shift_name:       current shift label
    - shift_id:         current shift_id (or None)
    - text:             preformatted copyable summary string
    """
    with db_conn(DB_PATH) as conn:
        # Resolve shift: explicit param → active shift fallback
        if shift_id:
            shift_row = conn.execute(
                "SELECT shift_id, shift_number, started_at, ended_at, shift_name FROM shifts WHERE shift_id=?",
                (shift_id,),
            ).fetchone()
        else:
            shift_row = conn.execute(
                "SELECT shift_id, shift_number, started_at, ended_at, shift_name FROM shifts WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        shift_id = shift_row["shift_id"] if shift_row else None
        shift_name = (
            (shift_row["shift_name"] or f"Shift {shift_row['shift_number']}")
            if shift_row
            else None
        )

        # If no shift found, return empty data
        if not shift_id:
            return {
                "loaded_by_site": {},
                "reached_by_site": {},
                "unloaded_by_site": {},
                "in_loading": [],
                "in_unloading": [],
                "truck_cycles": [],
                "shift_name": None,
                "shift_id": None,
                "total_loaded": 0,
                "text": "No active shift.",
            }

        shift_filter = "AND e.shift_id = ?"
        bind = [shift_id]

        # LO count per loading site
        lo_rows = conn.execute(
            f"""
            SELECT e.site_id, COALESCE(s.display_name, e.site_id) as site_name, COUNT(*) as n
            FROM events e LEFT JOIN sites s ON e.site_id=s.site_id
            WHERE e.commit_status IN ('COMMITTED','FLAGGED') AND e.status='LO'
              AND s.site_type='loading' {shift_filter}
            GROUP BY e.site_id ORDER BY n DESC
        """,
            bind,
        ).fetchall()

        # ENTER count per unloading site
        reach_rows = conn.execute(
            f"""
            SELECT e.site_id, COALESCE(s.display_name, e.site_id) as site_name, COUNT(*) as n
            FROM events e LEFT JOIN sites s ON e.site_id=s.site_id
            WHERE e.commit_status IN ('COMMITTED','FLAGGED') AND e.status='ENTER'
              AND s.site_type='unloading' {shift_filter}
            GROUP BY e.site_id ORDER BY n DESC
        """,
            bind,
        ).fetchall()

        # UO count per unloading site
        uo_rows = conn.execute(
            f"""
            SELECT e.site_id, COALESCE(s.display_name, e.site_id) as site_name, COUNT(*) as n
            FROM events e LEFT JOIN sites s ON e.site_id=s.site_id
            WHERE e.commit_status IN ('COMMITTED','FLAGGED') AND e.status='UO'
              AND s.site_type='unloading' {shift_filter}
            GROUP BY e.site_id ORDER BY n DESC
        """,
            bind,
        ).fetchall()

        # Trucks currently in loading (latest committed/flagged event per truck = LS, site_type=loading)
        in_loading_rows = conn.execute(
            f"""
            SELECT COALESCE(t.display_name, e.truck_alias, e.truck_id) as label,
                   e.truck_alias, e.truck_id, COALESCE(s.display_name, e.site_id) as site_name
            FROM events e
            LEFT JOIN trucks t ON e.truck_id=t.truck_id
            LEFT JOIN sites  s ON e.site_id=s.site_id
            WHERE e.commit_status IN ('COMMITTED','FLAGGED') AND e.status='LS'
              AND s.site_type='loading' AND e.truck_id IS NOT NULL
              {shift_filter}
              AND e.rowid IN (
                SELECT MAX(rowid) FROM events
                WHERE commit_status IN ('COMMITTED','FLAGGED') AND truck_id IS NOT NULL
                  {"AND shift_id = ?" if shift_id else ""}
                GROUP BY truck_id
              )
        """,
            bind + bind,
        ).fetchall()

        # Trucks currently in unloading (latest event = US)
        in_unloading_rows = conn.execute(
            f"""
            SELECT COALESCE(t.display_name, e.truck_alias, e.truck_id) as label,
                   e.truck_alias, e.truck_id, COALESCE(s.display_name, e.site_id) as site_name
            FROM events e
            LEFT JOIN trucks t ON e.truck_id=t.truck_id
            LEFT JOIN sites  s ON e.site_id=s.site_id
            WHERE e.commit_status IN ('COMMITTED','FLAGGED') AND e.status='US'
              AND s.site_type='unloading' AND e.truck_id IS NOT NULL
              {shift_filter}
              AND e.rowid IN (
                SELECT MAX(rowid) FROM events
                WHERE commit_status IN ('COMMITTED','FLAGGED') AND truck_id IS NOT NULL
                  {"AND shift_id = ?" if shift_id else ""}
                GROUP BY truck_id
              )
        """,
            bind + bind,
        ).fetchall()

        # Per-truck LO count (load cycles) this shift
        truck_lo = conn.execute(
            f"""
            SELECT e.truck_id,
                   COALESCE(e.truck_alias, e.truck_id) as alias,
                   COUNT(*) as loads
            FROM events e
            WHERE e.commit_status IN ('COMMITTED','FLAGGED') AND e.status='LO' {shift_filter}
            GROUP BY e.truck_id ORDER BY loads DESC, e.truck_id
        """,
            bind,
        ).fetchall()

        # Per-truck UO count
        truck_uo_map = {}
        for r in conn.execute(
            f"""
            SELECT e.truck_id, COUNT(*) as unloads
            FROM events e
            WHERE e.commit_status IN ('COMMITTED','FLAGGED') AND e.status='UO' {shift_filter}
            GROUP BY e.truck_id
        """,
            bind,
        ).fetchall():
            truck_uo_map[r["truck_id"]] = r["unloads"]

    # Build structured data
    loaded_by_site = {
        r["site_id"]: {"name": r["site_name"], "count": r["n"]} for r in lo_rows
    }
    reached_by_site = {
        r["site_id"]: {"name": r["site_name"], "count": r["n"]} for r in reach_rows
    }
    unloaded_by_site = {
        r["site_id"]: {"name": r["site_name"], "count": r["n"]} for r in uo_rows
    }

    truck_cycles = []
    for r in truck_lo:
        alias = r["alias"] or r["truck_id"]
        # Normalise alias: strip leading T for display (TA→A)
        import re

        short = (
            re.sub(r"^T([A-Z0-9]{1,2})$", r"\1", alias)
            if alias == alias.upper()
            else alias
        )
        truck_cycles.append(
            {
                "truck_id": r["truck_id"],
                "alias": short,
                "loads": r["loads"],
                "unloads": truck_uo_map.get(r["truck_id"], 0),
            }
        )

    in_loading_trucks = [dict(r) for r in in_loading_rows]
    in_unloading_trucks = [dict(r) for r in in_unloading_rows]

    total_loaded = sum(d["count"] for d in loaded_by_site.values())

    # Build copyable text
    lines = [f"── {shift_name} summary ──"]
    lines.append(f"Total Trolleys Loaded (all sites) = {total_loaded}")

    if loaded_by_site:
        for sid, d in loaded_by_site.items():
            lines.append(f"  Trolleys Loaded @{d['name']} = {d['count']}")
    else:
        lines.append("  Trolleys Loaded = 0")

    if reached_by_site:
        for sid, d in reached_by_site.items():
            lines.append(f"Trolleys Reached @{d['name']} = {d['count']}")
    else:
        lines.append("Trolleys Reached = 0")

    if unloaded_by_site:
        for sid, d in unloaded_by_site.items():
            lines.append(f"Trolleys UNLOADED @{d['name']} = {d['count']}")
    else:
        lines.append("Trolleys UNLOADED = 0")

    if in_loading_trucks:
        aliases = [r["truck_alias"] or r["truck_id"] for r in in_loading_trucks]
        # Normalise
        short_aliases = []
        for a in aliases:
            import re

            short_aliases.append(
                re.sub(r"^T([A-Z0-9]{1,2})$", r"\1", a) if a == a.upper() else a
            )
        lines.append(
            f"Trolleys in Loading = {len(in_loading_trucks)}  ({', '.join(short_aliases)})"
        )
    else:
        lines.append("Trolleys in Loading = 0")

    if in_unloading_trucks:
        aliases = [r["truck_alias"] or r["truck_id"] for r in in_unloading_trucks]
        import re

        short_aliases = [
            re.sub(r"^T([A-Z0-9]{1,2})$", r"\1", a) if a == a.upper() else a
            for a in aliases
        ]
        lines.append(
            f"Trolleys in Unloading = {len(in_unloading_trucks)}  ({', '.join(short_aliases)})"
        )

    if truck_cycles:
        lines.append("")
        lines.append("Load cycles per trolley:")
        cycle_parts = [f"{t['alias']} = {t['loads']}" for t in truck_cycles]
        lines.append("  " + "   ".join(cycle_parts))

    text = "\n".join(lines)

    return {
        "shift_id": shift_id,
        "shift_name": shift_name,
        "started_at": shift_row["started_at"] if shift_row else None,
        "ended_at": shift_row["ended_at"] if shift_row else None,
        "total_loaded": total_loaded,
        "loaded_by_site": loaded_by_site,
        "reached_by_site": reached_by_site,
        "unloaded_by_site": unloaded_by_site,
        "in_loading": in_loading_trucks,
        "in_unloading": in_unloading_trucks,
        "truck_cycles": truck_cycles,
        "text": text,
    }


def _group_loading_cycles(events: list) -> list:
    """
    Group a sorted list of truck events into loading cycles.

    A cycle is ENTER → LS → LO → LEFT (each optional).
    Rules:
    - A new cycle starts on ENTER or LS when the previous cycle was closed
      (i.e. last meaningful event was LO or LEFT).
    - Inferred events are flagged but still anchor cycle boundaries (they
      should NOT be displayed as visual markers on the gantt).
    - Returns a list of cycle dicts: {enter, ls, lo, left} each being
      an event dict (or None), plus cycle_number (1-based).
    """
    cycles = []
    cur = {"enter": None, "ls": None, "lo": None, "left": None}
    _OPEN_STATUSES = {"ENTER", "LS"}
    _CLOSE_STATUSES = {"LO", "LEFT"}

    def _flush():
        if any(v is not None for v in cur.values()):
            cycles.append({**cur})

    for ev in events:
        s = ev["status"]
        if s == "ENTER":
            # Start a new cycle if prev was closed or empty
            if cur["lo"] is not None or cur["left"] is not None:
                _flush()
                cur = {"enter": None, "ls": None, "lo": None, "left": None}
            cur["enter"] = ev
        elif s == "LS":
            # Start new cycle if prev was already closed
            if cur["lo"] is not None or cur["left"] is not None:
                _flush()
                cur = {"enter": None, "ls": None, "lo": None, "left": None}
            cur["ls"] = ev
        elif s == "LO":
            cur["lo"] = ev
        elif s == "LEFT":
            cur["left"] = ev
            _flush()
            cur = {"enter": None, "ls": None, "lo": None, "left": None}

    # flush trailing open cycle
    if any(v is not None for v in cur.values()):
        _flush()

    for i, c in enumerate(cycles):
        c["cycle_number"] = i + 1

    return cycles


@router.get("/gantt")
def gantt_data(shift_id: str = Query("")):
    """
    Gantt chart data for a shift.

    Returns per-truck loading cycles with ENTER/LS/LO/LEFT timestamps.
    Inferred events are included in cycle boundaries but marked inferred=true
    so the frontend can skip rendering them as visual markers.

    Also returns shift start/end for the time axis.
    """
    with db_conn(DB_PATH) as conn:
        # Resolve shift
        if shift_id:
            shift_row = conn.execute(
                "SELECT shift_id, started_at, ended_at, shift_name, shift_number FROM shifts WHERE shift_id=?",
                (shift_id,),
            ).fetchone()
        else:
            shift_row = conn.execute(
                "SELECT shift_id, started_at, ended_at, shift_name, shift_number FROM shifts WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
            ).fetchone()

        if not shift_row:
            return {
                "shift_id": None,
                "trucks": [],
                "shift_start": None,
                "shift_end": None,
            }

        sid = shift_row["shift_id"]
        shift_start = shift_row["started_at"]
        shift_end = shift_row["ended_at"]

        # All COMMITTED/FLAGGED loading-cycle events for this shift, ordered by time
        rows = conn.execute(
            """SELECT e.event_id, e.truck_id,
                      COALESCE(t.display_name, e.truck_alias, e.truck_id) as truck_name,
                      e.status, e.site_id,
                      COALESCE(s.display_name, e.site_id) as site_name,
                      e.timestamp_effective, e.inferred, e.confidence,
                      e.timestamp_approximate
               FROM events e
               LEFT JOIN trucks t ON t.truck_id = e.truck_id
               LEFT JOIN sites  s ON s.site_id  = e.site_id
               WHERE e.shift_id = ?
                 AND e.commit_status IN ('COMMITTED', 'FLAGGED')
                 AND e.status IN ('ENTER', 'LS', 'LO', 'LEFT')
                 AND e.truck_id IS NOT NULL
               ORDER BY e.truck_id, e.timestamp_effective""",
            (sid,),
        ).fetchall()

        # Group events by truck
        by_truck = defaultdict(list)
        truck_names = {}
        for r in rows:
            ev = dict(r)
            ev["inferred"] = bool(ev["inferred"])
            ev["timestamp_approximate"] = bool(ev.get("timestamp_approximate"))
            by_truck[ev["truck_id"]].append(ev)
            truck_names[ev["truck_id"]] = ev["truck_name"]

        # Average loading time per truck (LS→LO minutes, non-inferred only)
        avg_rows = conn.execute(
            """SELECT ls.truck_id,
                      ROUND(AVG((strftime('%s', lo.timestamp_effective)
                               - strftime('%s', ls.timestamp_effective)) / 60.0), 1) as avg_min,
                      COUNT(*) as cycles
               FROM events ls
               JOIN events lo
                 ON lo.truck_id = ls.truck_id
                AND lo.shift_id = ls.shift_id
                AND lo.status = 'LO'
                AND lo.commit_status IN ('COMMITTED', 'FLAGGED')
                AND lo.inferred = 0
                AND lo.timestamp_effective > ls.timestamp_effective
                AND lo.timestamp_effective = (
                      SELECT MIN(x.timestamp_effective) FROM events x
                      WHERE x.truck_id = ls.truck_id AND x.shift_id = ls.shift_id
                        AND x.status = 'LO' AND x.commit_status IN ('COMMITTED','FLAGGED')
                        AND x.inferred = 0
                        AND x.timestamp_effective > ls.timestamp_effective
                    )
               WHERE ls.shift_id = ?
                 AND ls.status = 'LS'
                 AND ls.commit_status IN ('COMMITTED', 'FLAGGED')
                 AND ls.inferred = 0
               GROUP BY ls.truck_id""",
            (sid,),
        ).fetchall()
        avg_by_truck = {
            r["truck_id"]: {"avg_min": r["avg_min"], "cycles": r["cycles"]}
            for r in avg_rows
        }

    trucks_out = []
    for truck_id, evs in sorted(
        by_truck.items(), key=lambda x: truck_names.get(x[0], x[0])
    ):
        cycles = _group_loading_cycles(evs)
        stats = avg_by_truck.get(truck_id, {"avg_min": None, "cycles": 0})
        trucks_out.append(
            {
                "truck_id": truck_id,
                "truck_name": truck_names.get(truck_id, truck_id),
                "cycles": cycles,
                "avg_min": stats["avg_min"],
                "total_loads": stats["cycles"],
            }
        )

    return {
        "shift_id": sid,
        "shift_name": shift_row["shift_name"] or f"Shift {shift_row['shift_number']}",
        "shift_start": shift_start,
        "shift_end": shift_end,
        "trucks": trucks_out,
    }


@router.get("/shifts")
def list_shifts():
    """All shifts ordered newest-first, for the shift selector."""
    with db_conn(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT s.shift_id, s.shift_number, s.shift_name, s.started_at, s.ended_at,
                      s.default_site_id, s.default_site_ids,
                      (SELECT COUNT(*) FROM events e WHERE e.shift_id=s.shift_id
                       AND e.commit_status IN ('COMMITTED','FLAGGED')) as event_count
               FROM shifts s
               WHERE (s.is_deleted IS NULL OR s.is_deleted = 0)
               ORDER BY s.started_at DESC"""
        ).fetchall()
        # Pre-fetch site display names for all referenced site_ids
        all_site_ids = set()
        for r in rows:
            if r["default_site_id"]:
                all_site_ids.add(r["default_site_id"])
            if r["default_site_ids"]:
                try:
                    for sid in json.loads(r["default_site_ids"]):
                        all_site_ids.add(sid)
                except Exception:
                    pass
        site_names: dict = {}
        if all_site_ids:
            for sid in all_site_ids:
                sr = conn.execute(
                    "SELECT display_name FROM sites WHERE site_id=?", (sid,)
                ).fetchone()
                site_names[sid] = sr["display_name"] if sr else sid

    def _resolve_site_names(row) -> list:
        ids: list = []
        if row["default_site_ids"]:
            try:
                ids = json.loads(row["default_site_ids"])
            except Exception:
                pass
        if not ids and row["default_site_id"]:
            ids = [row["default_site_id"]]
        return [site_names.get(sid, sid) for sid in ids]

    return {
        "shifts": [
            {
                "shift_id": r["shift_id"],
                "shift_number": r["shift_number"],
                "shift_name": r["shift_name"] or f"Shift {r['shift_number']}",
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "active": r["ended_at"] is None,
                "default_sites": _resolve_site_names(r),
                "event_count": r["event_count"],
            }
            for r in rows
        ]
    }


@router.get("/fleet-map")
def fleet_map():
    """Current truck positions + status for map overlay."""
    with db_conn(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT e.truck_id, e.truck_alias, e.status, e.site_id, e.confidence,
                      e.timestamp_effective, t.display_name
               FROM events e
               LEFT JOIN trucks t ON e.truck_id=t.truck_id
               WHERE e.commit_status='COMMITTED' AND e.truck_id IS NOT NULL
                 AND e.rowid IN (
                   SELECT MAX(rowid) FROM events
                   WHERE commit_status='COMMITTED' AND truck_id IS NOT NULL
                   GROUP BY truck_id
                 )"""
        ).fetchall()
    return {
        "trucks": [
            dict(
                zip(
                    [
                        "truck_id",
                        "truck_alias",
                        "status",
                        "site_id",
                        "confidence",
                        "timestamp_effective",
                        "display_name",
                    ],
                    r,
                )
            )
            for r in rows
        ]
    }
