"""
Fleet NL query — two-step SQL chain.

Step 1: LLM generates SQL from the question + schema.
Step 2: Execute SQL against SQLite, LLM formats the result as plain English.

No agent framework / tool-calling needed — works reliably with open-source models.

Config (env vars):
  FLEET_LLM_BASE_URL  default http://localhost:8001/v1  (llama.cpp / vLLM / Ollama)
  FLEET_LLM_MODEL     default Qwen2.5-32B-Instruct
  FLEET_LLM_API_KEY   default EMPTY
"""
import logging
import re
import sqlite3
from typing import Optional

from fleet_pipeline.config import DB_PATH

log = logging.getLogger(__name__)

from fleet_pipeline.config import LLM_BASE_URL as _CFG_BASE_URL, LLM_API_KEY as _CFG_API_KEY, MODEL_NAME as _CFG_MODEL
import os as _os

# NL query can use a separate (larger) model if desired, falls back to pipeline model
LLM_BASE_URL = _os.getenv("FLEET_LLM_BASE_URL", _CFG_BASE_URL or "http://localhost:8001/v1")
LLM_MODEL    = _os.getenv("FLEET_LLM_MODEL",    _CFG_MODEL)
LLM_API_KEY  = _os.getenv("FLEET_LLM_API_KEY",  _CFG_API_KEY)

# ---------------------------------------------------------------------------
# Schema description (static — avoids a round-trip to inspect tables)
# ---------------------------------------------------------------------------

_SCHEMA = """
SQLite database for a real-time truck fleet tracking system.
Events are parsed from WhatsApp messages by an LLM pipeline.

TABLE: trucks
  truck_id      TEXT PK   (e.g. TA, TB, T_ARJ_NOVO)
  display_name  TEXT       (e.g. "Truck A", "Arjun Novo")
  aliases       TEXT       JSON array of alternate names
  is_active     INTEGER    1=active

TABLE: sites
  site_id       TEXT PK   (e.g. SOC, BG, KN4, DAIRY, TN, PL, KHET)
  display_name  TEXT       (e.g. "Bhandara Ground")
  site_type     TEXT       Loading | Unloading | Depot

TABLE: events
  event_id             TEXT PK
  truck_id             TEXT FK → trucks
  status               TEXT   ENTER|LS|LO|LEFT|US|UO
  site_id              TEXT FK → sites
  site_alias           TEXT   raw alias from message
  timestamp_effective  TEXT   ISO-8601 datetime (IST, UTC+5:30)
  confidence           REAL   0.0–1.0
  commit_status        TEXT   COMMITTED|FLAGGED|HELD|DELETED
  created_at           TEXT

  STATUS CODES:
    ENTER = truck arrived at site
    LS    = loading started
    LO    = loading complete (ready to depart)
    LEFT  = departed site, in transit
    US    = unloading started
    UO    = unloading complete

TABLE: hitl_queue
  question_id    TEXT PK
  question_type  TEXT   UNKNOWN_TRUCK|UNKNOWN_SITE|LOW_CONFIDENCE|CORRECTION_AMBIGUOUS
  question_text  TEXT
  status         TEXT   OPEN|ANSWERED|DISMISSED
  created_at     TEXT

TABLE: tallies
  tally_id      TEXT PK
  timestamp_iso TEXT
  tally_data    TEXT   JSON with load counts
  commit_status TEXT
""".strip()

_SQL_RULES = """
RULES FOR WRITING THE SQL QUERY:
1. Always filter: commit_status = 'COMMITTED' (unless question asks about held/flagged).

2. "Current status" / "where is truck X" / "where are all trucks" / "fleet state":
   Use the events table — find the most recent COMMITTED event per truck.
   ```sql
   SELECT t.display_name, e.status, e.site_id, e.timestamp_effective
   FROM events e
   JOIN trucks t ON t.truck_id = e.truck_id
   WHERE e.commit_status = 'COMMITTED'
     AND e.timestamp_effective = (
       SELECT MAX(e2.timestamp_effective) FROM events e2
       WHERE e2.truck_id = e.truck_id AND e2.commit_status = 'COMMITTED'
     )
   ORDER BY t.display_name;
   ```

3. Loading cycle time (LS → LO duration), average per truck then overall:
   For each LS, find the NEXT LO at the same (truck_id, site_id).
   ```sql
   SELECT t.display_name,
          COUNT(*) AS cycles,
          ROUND(AVG((strftime('%s', lo.timestamp_effective) - strftime('%s', ls.timestamp_effective)) / 60.0), 1) AS avg_min,
          ROUND(MIN((strftime('%s', lo.timestamp_effective) - strftime('%s', ls.timestamp_effective)) / 60.0), 1) AS min_min,
          ROUND(MAX((strftime('%s', lo.timestamp_effective) - strftime('%s', ls.timestamp_effective)) / 60.0), 1) AS max_min
   FROM events ls
   JOIN events lo ON lo.truck_id = ls.truck_id
                 AND lo.site_id  = ls.site_id
                 AND lo.status   = 'LO'
                 AND lo.commit_status = 'COMMITTED'
                 AND lo.timestamp_effective = (
                   SELECT MIN(x.timestamp_effective) FROM events x
                   WHERE x.truck_id = ls.truck_id AND x.site_id = ls.site_id
                     AND x.status = 'LO' AND x.commit_status = 'COMMITTED'
                     AND x.timestamp_effective > ls.timestamp_effective
                 )
   JOIN trucks t ON t.truck_id = ls.truck_id
   WHERE ls.status = 'LS' AND ls.commit_status = 'COMMITTED'
     AND (strftime('%s',lo.timestamp_effective)-strftime('%s',ls.timestamp_effective)) BETWEEN 60 AND 43200
   GROUP BY ls.truck_id, t.display_name
   ORDER BY avg_min DESC;
   ```
   For the overall average across all trucks, wrap the above as a subquery and AVG the avg_min values.

4. Date filter: timestamp_effective LIKE '2025-10-14%'
5. Use LIMIT 50 unless question asks for all rows.
6. Do NOT use trucks.is_active to determine current status or location.
""".strip()


# ---------------------------------------------------------------------------
# Lazy LLM singleton
# ---------------------------------------------------------------------------

_llm = None

def _get_llm():
    global _llm
    if _llm is None:
        from openai import OpenAI
        _llm = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=120)
    return _llm


def _chat(system: str, user: str, max_tokens: int = 512) -> str:
    client = _get_llm()
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=0,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# SQL extraction helper
# ---------------------------------------------------------------------------

def _extract_sql(text: str) -> Optional[str]:
    """Pull the first SQL SELECT statement out of the LLM response."""
    # Try markdown code block first
    m = re.search(r"```(?:sql)?\s*(SELECT[\s\S]+?)```", text, re.I)
    if m:
        return m.group(1).strip()
    # Bare SELECT
    m = re.search(r"(SELECT\s+[\s\S]+?);?\s*$", text, re.I | re.M)
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def agent_answer(question: str) -> dict:
    """
    Two-step chain: generate SQL → execute → format answer.
    Falls back to pattern-matching handler on any error.
    """
    try:
        # ── Step 1: Generate SQL ────────────────────────────────────────────
        sql_system = (
            "You are a SQLite expert for a truck fleet tracking database.\n"
            f"{_SCHEMA}\n\n{_SQL_RULES}\n\n"
            "Return ONLY the SQL query — no explanation, no markdown prose. "
            "Wrap it in a ```sql code block."
        )
        sql_user = f"Write a SQL query to answer: {question}"

        sql_raw = _chat(sql_system, sql_user, max_tokens=1024)
        sql = _extract_sql(sql_raw)

        if not sql:
            raise ValueError(f"LLM did not produce a SQL query. Got: {sql_raw[:200]}")

        log.info("Generated SQL: %s", sql)

        # ── Step 2: Execute SQL (run all statements, collect all results) ────
        # Model sometimes produces 2 queries (e.g. per-truck then overall avg)
        statements = [s.strip() for s in sql.split(";") if s.strip().upper().startswith("SELECT")]
        all_rows = []
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            for stmt in statements:
                try:
                    rows = conn.execute(stmt).fetchmany(50)
                    if rows:
                        all_rows.append(rows)
                except sqlite3.Error as e:
                    raise ValueError(f"SQL error: {e}\nSQL was: {stmt}")
        rows = all_rows[0] if all_rows else []

        def _fmt_rows(rs):
            if not rs:
                return "No rows returned."
            cols = rs[0].keys()
            lines = [" | ".join(cols)]
            lines += [" | ".join(str(r[c]) if r[c] is not None else "—" for c in cols) for r in rs]
            return "\n".join(lines)

        if not all_rows:
            results_text = "No rows returned."
        elif len(all_rows) == 1:
            results_text = _fmt_rows(all_rows[0])
        else:
            parts = []
            for i, rs in enumerate(all_rows, 1):
                parts.append(f"[Result set {i}]\n{_fmt_rows(rs)}")
            results_text = "\n\n".join(parts)

        # ── Step 3: Format answer ───────────────────────────────────────────
        fmt_system = (
            "You are a fleet operations analyst. "
            "Given raw SQL results, write a concise professional answer in plain English. "
            "Include relevant names, statuses, and times. Do not mention SQL."
        )
        fmt_user = (
            f"Question: {question}\n\n"
            f"SQL results:\n{results_text}\n\n"
            "Write a clear, concise answer:"
        )

        answer = _chat(fmt_system, fmt_user, max_tokens=2048)

        return {"answer": answer, "data": {"sql": sql, "row_count": len(rows)}, "source": "agent"}

    except Exception as e:
        log.warning("SQL chain failed (%s: %s). Falling back to pattern-matcher.", type(e).__name__, e)

    from fleet_pipeline.api.query_handler import answer_query
    result = answer_query(question)
    result["source"] = "fallback"
    return result
