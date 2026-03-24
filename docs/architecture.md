# Architecture

## System Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  WhatsApp Group                                                      │
│                                                                      │
│  Operator ──▶ "D LS SOC"          Bot ◀── "❓ Unknown site 'XY'…"  │
│               "H LS XY"                                             │
│               "SOC"  ◀── operator reply to bot                      │
└───────────────┬──────────────────────────────────┬──────────────────┘
                │ WebSocket (Baileys)               │ POST /send-reply
                ▼                                   ▲
┌───────────────────────────┐          ┌────────────────────────────┐
│  wa_listener  (Node.js)   │          │  fleet_pipeline  (Python)  │
│                           │          │                            │
│  • Baileys WebSocket      │──POST──▶ │  FastAPI / uvicorn         │
│  • Extracts quoted msg ID │          │  port 8000                 │
│  • POST /api/ingest/      │◀──HTTP── │                            │
│    wa-message             │          │  /send-reply endpoint      │
│  • /send-reply server     │          │  at port 3001              │
│    port 3001              │          │                            │
└───────────────────────────┘          └────────────┬───────────────┘
                                                    │ WebSocket /ws
                                       ┌────────────▼───────────────┐
                                       │  nginx  (HOST_PORT)        │
                                       │                            │
                                       │  GET /          → UI       │
                                       │  GET /api/...   → REST     │
                                       │  GET /ws        → WS proxy │
                                       └────────────────────────────┘
                                                    │
                                       ┌────────────▼───────────────┐
                                       │  Browser (dashboard)       │
                                       │  Receives pushed events:   │
                                       │  commit_created            │
                                       │  fleet_state_updated       │
                                       │  hitl_created              │
                                       └────────────────────────────┘
```

---

## Process Responsibilities

### `wa_listener` (Node.js)

- Maintains a persistent Baileys WebSocket connection to WhatsApp Web
- Forwards every group message to `POST /api/ingest/wa-message` on the Python API
- Extracts `contextInfo.stanzaId` (quoted message ID) so Python can detect WA replies
- Skips its own outbound messages (`fromMe: true`)
- Exposes `POST /send-reply` on port 3001 so Python can send bot replies back to WA
- Exposes `GET /health` for Docker healthcheck
- Saves auth session to `WA_SESSION_DIR` (Docker volume); QR only needed once

### `fleet_pipeline` (Python / FastAPI)

- Runs migrations on startup
- Warms up LLM KV cache on first boot
- Handles WA messages: checks for HITL reply routing before the normal pipeline
- Runs Level1 → Level2 → Level3 → Commit pipeline in a thread executor (non-blocking)
- Broadcasts events to all connected dashboard browsers via WebSocket
- Sends HITL clarification messages back to WA via `wa_notifier → POST /send-reply`

### `nginx`

- Terminates HTTP at `HOST_PORT`
- Proxies `/ws` with `Upgrade: websocket` and 24-hour read timeout
- Proxies everything else with 300-second read timeout (LLM calls take 15–120s)

---

## Request Lifecycle — Normal Message

```
1. Operator sends "D LS SOC" in WA group

2. wa_listener receives message via Baileys:
   wa_message_id = "3EB0ABC..."
   quoted_wa_message_id = null   (not a reply)

3. wa_listener POSTs to Python:
   POST /api/ingest/wa-message
   { wa_message_id, sender_phone, group_jid, raw_text, received_at }

4. Python returns 202 immediately
   Background task started

5. Python broadcasts to dashboard:
   WS event: message_received { raw_text, sender, timestamp }

6. Background task:
   a. Level 1 — extracts timestamp, sender_id, raw_text
   b. Level 2 — fuzzy-matches "D" → truck TD, "SOC" → site SOC
   c. Level 3 — sends prompt to LLM, receives JSON:
      { status: "LS", truck_id: "TD", site_id: "SOC", confidence: 0.95 }
   d. Committer:
      - Checks if ENTER needed before LS (open cycle check)
      - Inserts ENTER (inferred) if needed
      - Inserts LS event as COMMITTED
      - Stores wa_message_id on HITL questions (if any)

7. Python broadcasts:
   WS event: commit_created { committed: 1, flagged: 0, held: 0 }
   WS event: fleet_state_updated {}

8. Dashboard re-fetches KPIs and updates in real time
```

---

## Request Lifecycle — HITL Loop

```
1. Operator sends "H LS XY"  (site 'XY' not in registry)

2–6. Same as above through Level 3.
   LLM: { status: "LS", truck_id: "TH", site_id: null, confidence: 0.55 }
   Committer: site_id=null → HELD, creates UNKNOWN_SITE question

7. Committer stores on hitl_queue:
   original_wa_message_id = "3EB0ABC..."
   group_jid = "120363XXXXXX@g.us"

8. pipeline_service calls wa_notifier:
   Looks up newly created HITL questions for this msg_id
   Calls POST http://wa:3001/send-reply:
   {
     group_jid: "120363XXXXXX@g.us",
     text: "❓ Unknown site — 'XY' not recognised.\n\nOriginal: \"H LS XY\"\n\nReply with:\n• Site code e.g. SOC\n...",
     quote_id: "3EB0ABC..."   ← quotes the original "H LS XY" message
   }

9. wa_listener sends the bot message, returns bot_message_id = "3EB0DEF..."
   Python stores bot_wa_message_id on the hitl_queue row

10. Operator sees the bot reply quoting their message in WA.
    They reply to the bot message: "SOC"

11. wa_listener detects:
    quoted_wa_message_id = "3EB0DEF..."   ← bot's message ID

    POSTs to Python:
    POST /api/ingest/wa-message
    { raw_text: "SOC", quoted_wa_message_id: "3EB0DEF...", ... }

12. Python ingest checks:
    SELECT * FROM hitl_queue WHERE bot_wa_message_id = '3EB0DEF...' AND status='OPEN'
    → Found: question_type = UNKNOWN_SITE

    Routes to HITL answer handler (skips normal pipeline)

13. HITL answer logic:
    "SOC" is a known site code → direct DB update
    UPDATE events SET site_id='SOC', commit_status='COMMITTED'
    UPDATE hitl_queue SET status='ANSWERED'

14. Bot sends ack in WA (replying to its own question):
    "✅ Received — ok"

15. Python broadcasts:
    WS event: fleet_state_updated {}
    Dashboard updates
```

---

## Database Schema (summary)

```
trucks          Canonical truck registry (truck_id, aliases JSON, is_active)
sites           Canonical site registry (site_id, aliases JSON, site_type, is_active)
raw_messages    One row per ingested message (msg_id, sender, timestamp, raw_text)
events          Parsed truck events (truck_id, status, site_id, confidence, commit_status)
hitl_queue      Pending human review items (question_type, context, bot_wa_message_id)
wa_messages     Raw WA ingestion log (wa_message_id, sender_phone, group_jid)
shifts          Detected shift records (shift_number, started_at, ended_at)
shift_config    Admin-editable shift times (start_time, wa_keyword)
tallies         Operator-sent count summaries (KN4=11 BG=9 DAIRY=4)
corrections     Append-only correction audit trail
audit_log       All DB mutations with old/new values
simulation_runs Batch replay run tracking
```

Full DDL: [`fleet_pipeline/db/schema.sql`](../fleet_pipeline/db/schema.sql)

---

## WebSocket Events

The server pushes these events to all connected dashboard clients:

| Event type | When | Payload fields |
|------------|------|----------------|
| `message_received` | Immediately on WA message arrival | `wa_message_id`, `raw_text`, `sender`, `timestamp_iso`, `source` |
| `commit_created` | After pipeline finishes | `msg_id`, `committed`, `flagged`, `held`, `source` |
| `fleet_state_updated` | After any state change | `source` |
| `hitl_created` | After HITL questions created | `source` |
| `hitl_answered_wa` | After WA reply routes as answer | `question_id`, `answer` |
| `commit_error` | On pipeline exception | `raw_text`, `error` |
| `reprocess_complete` | After bulk reprocess | `count` |

---

## Confidence Thresholds

| Threshold | Value | Meaning |
|-----------|-------|---------|
| `AUTO_COMMIT` | 0.85 | ≥ 0.85 → COMMITTED silently |
| `COMMIT_FLAG` | 0.60 | 0.60–0.85 → FLAGGED (amber, visible but not blocking) |
| `HOLD` | 0.60 | < 0.60 → HELD + HITL question created |

Special rules override thresholds:
- `truck_id = null` → always HELD (UNKNOWN_TRUCK)
- `site_id = null` AND status requires site → always HELD (UNKNOWN_SITE)
- These override LOW_CONFIDENCE — only one HITL question per event
