# Fleet Log Pipeline

An LLM-powered pipeline that parses WhatsApp-style truck operation logs into structured events, commits them to a database, and surfaces a fleet dashboard with a human-in-the-loop review interface.

---

## What It Does

1. **Ingests** raw WhatsApp `.txt` chat exports
2. **Parses** each message (Level 1) into structured fields
3. **Enriches** messages (Level 2) with heuristic pre-processing — truck/site detection, message type classification, time deltas
4. **Infers** structured truck events (Level 3) via LLM (Qwen2.5-7B-Instruct-AWQ)
5. **Commits** events to SQLite with confidence-based routing: auto-commit, flag for review, or hold
6. **Queues** ambiguous results for human review (HITL)
7. **Serves** a web dashboard — fleet state, HITL queue, and natural language queries

---

## Directory Structure

```
fleet_pipeline/
├── config.py                   # Paths, model name, confidence thresholds
├── data/
│   └── fleet.db                # SQLite database (auto-created on first run)
├── db/
│   ├── schema.sql              # Full DB schema (7 tables)
│   ├── database.py             # Connection helpers + all insert/query functions
│   └── seed_data.py            # Seed trucks and sites registries
├── pipeline/
│   ├── level1.py               # WhatsApp .txt parser
│   ├── level2.py               # Rule-based enricher
│   ├── level3.py               # LLM call wrapper (vLLM or mock)
│   ├── registries.py           # Load truck/site registries from DB
│   ├── committer.py            # Commit rules: writes events to DB
│   ├── hitl_queue.py           # Create and manage HITL questions
│   └── validator.py            # Schema validation for LLM output
├── prompts/
│   └── level3_prompt_template.txt   # 12 few-shot examples
├── api/
│   ├── main.py                 # FastAPI app entry point
│   ├── query_handler.py        # Natural language query → DB answer
│   └── routes/
│       ├── fleet.py            # GET /fleet/state, /fleet/truck/:id
│       ├── hitl.py             # GET/POST /hitl/queue, /hitl/answer
│       └── simulation.py       # POST /simulate/run, GET /simulate/status
├── frontend/
│   ├── index.html              # 3-tab dark UI
│   ├── fleet_view.js           # Fleet table + summary cards
│   └── hitl_panel.js           # HITL queue: view, answer, dismiss
└── simulation/
    ├── run_simulation.py       # CLI: replay a .txt file through all levels
    └── evaluate.py             # Compare output vs manual ground truth
```

---

## Setup

### Requirements

```bash
pip install fastapi uvicorn pytz
# For real LLM inference:
pip install vllm
```

### First Run

```bash
# From /home/onkar/.jackt/truck/

# 1. Seed the database with truck and site registries
python3 -m fleet_pipeline.db.seed_data

# 2. Run a mock simulation (no GPU required)
python3 -m fleet_pipeline.simulation.run_simulation \
    --input raw_data_level1_2/chat.txt \
    --mock

# 3. Start the API + frontend
uvicorn fleet_pipeline.api.main:app --reload --port 8000
# Open http://localhost:8000
```

---

## Running Simulations

```bash
# Full historical replay (mock LLM):
python3 -m fleet_pipeline.simulation.run_simulation \
    --input raw_data_level1_2/chat.txt \
    --mock

# Single shift only:
python3 -m fleet_pipeline.simulation.run_simulation \
    --input raw_data_level1_2/chat.txt \
    --mock \
    --shift 20231015_shift_1

# Verbose per-message output:
python3 -m fleet_pipeline.simulation.run_simulation \
    --input raw_data_level1_2/chat.txt \
    --mock --verbose

# Real LLM (requires vLLM + Qwen2.5-7B-Instruct-AWQ):
python3 -m fleet_pipeline.simulation.run_simulation \
    --input raw_data_level1_2/chat.txt
```

Each run is tagged with a unique `run_id` so multiple runs don't overwrite each other.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/fleet/state` | Current status of all trucks (latest event per truck) |
| GET | `/fleet/truck/{id}` | Recent events for one truck |
| GET | `/fleet/events` | Recent committed events feed |
| GET | `/hitl/queue` | Open human-review questions |
| POST | `/hitl/answer` | Submit an answer to a question |
| POST | `/hitl/dismiss/{id}` | Dismiss a question |
| POST | `/simulate/run` | Start a simulation in the background |
| GET | `/simulate/status/{run_id}` | Get simulation stats |
| GET | `/simulate/list` | List all simulation runs |
| POST | `/query` | Natural language query (e.g. "Where is truck B?") |
| GET | `/docs` | Auto-generated OpenAPI docs |

---

## Commit Rules

The `Committer` applies these rules to every LLM output:

| Condition | Result |
|-----------|--------|
| `overall_confidence >= 0.85` | `COMMITTED` silently |
| `0.60 <= confidence < 0.85` | `FLAGGED` + HITL question |
| `confidence < 0.60` | `HELD` + HITL question |
| `truck_id = null` | Always `HELD` + `UNKNOWN_TRUCK` question |
| `site_id = null` and status requires site | `FLAGGED` + `UNKNOWN_SITE` question |
| `msg_type = CORRECTION` | Retroactively marks previous event `DELETED` |
| `is_deleted = True` | `DELETED_MESSAGE` HITL question created |

---

## HITL Answer Formats

When answering questions via the UI or API (`POST /hitl/answer`):

**UNKNOWN_TRUCK**
- Existing truck: `TB` (truck_id)
- New truck: `new:TX:Truck X:alias1,alias2`

**UNKNOWN_SITE**
- Existing site: `BG`
- New site: `new:SITEID:Display Name:loading:alias1,alias2`

**LOW_CONFIDENCE / CORRECTION_AMBIGUOUS**
- Free text confirmation or correction

---

## Database Tables

| Table | Purpose |
|-------|---------|
| `trucks` | Canonical truck registry with aliases |
| `sites` | Canonical site registry with aliases |
| `raw_messages` | Level 1 parsed messages |
| `events` | Committed truck status events |
| `tallies` | Tally/summary snapshots |
| `hitl_queue` | Human review questions |
| `audit_log` | All DB mutations logged |
| `simulation_runs` | Simulation run metadata + stats |

---

## Known Issues / Next Steps

- **Real LLM run**: Qwen2.5-7B still wraps output in markdown fences — `strip_markdown_fences()` in `level3.py` handles this
- **Confidence calibration**: Currently LLM self-reported only; tune thresholds after labelling historical data with `simulation/evaluate.py`
- **Live feed**: Pipeline currently runs in batch simulation mode; live ingestion (polling a shared folder or webhook) not yet implemented
- **Per-truck timeline**: Frontend fleet view shows latest state only; shift timeline view not yet built
