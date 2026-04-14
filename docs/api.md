# API Reference

Base URL: `http://localhost:HOST_PORT` (through nginx) or `http://localhost:8000` (direct)

---

## WebSocket

### `GET /ws`

Connect for real-time push events. The server sends JSON frames:

```json
{ "type": "event_type", "data": { ... } }
```

| `type` | Trigger | `data` fields |
|--------|---------|---------------|
| `message_received` | WA message arrives | `wa_message_id`, `raw_text`, `sender`, `timestamp_iso`, `source` |
| `commit_created` | Pipeline finishes | `msg_id`, `committed`, `flagged`, `held`, `source` |
| `fleet_state_updated` | Any state change | `source` |
| `hitl_created` | HITL question(s) created | `source` |
| `hitl_answered_wa` | WA reply routed as answer | `question_id`, `answer` |
| `commit_error` | Pipeline exception | `raw_text`, `error` |
| `reprocess_complete` | Bulk reprocess done | `count` |

---

## Ingestion

### `POST /api/ingest/wa-message`
Called by the Node.js WA listener. Queues message for background processing.

**Request body:**
```json
{
  "wa_message_id": "3EB0ABC123",
  "sender_phone": "+919876543210",
  "group_jid": "120363XXXXXX@g.us",
  "raw_text": "D LS SOC",
  "received_at": "2026-03-24T17:00:00+05:30",
  "message_type": "fleet_event",
  "quoted_wa_message_id": null
}
```

`quoted_wa_message_id` — if set and matches a HITL question's `bot_wa_message_id`, the message is routed as a HITL answer instead of through the normal pipeline.

**Response (202):**
```json
{ "queued": true, "wa_message_id": "3EB0ABC123" }
```
or if routed as HITL answer:
```json
{ "queued": true, "routed_as": "hitl_answer", "question_id": "uuid" }
```

---

### `POST /api/ingest/manual`
Operator panel text injection.

**Request body:**
```json
{
  "text": "H LS SOC",
  "sender_name": "operator",
  "sender_id": "operator",
  "timestamp_iso": "2026-03-24T17:00:00+05:30"
}
```

**Response (202):** `{ "queued": true, "temp_id": "uuid" }`

---

### `POST /api/ingest/reprocess-held`
Re-runs all HELD messages that failed due to LLM being offline. Call after LLM comes back up.

**Response:** `{ "reprocessed": 12, "message": "Reprocessing 12 held messages in background" }`

---

### `GET /api/ingest/status`
Returns current LLM mode.

**Response:**
```json
{ "mode": "openai_compat", "endpoint": "http://localhost:8001/v1", "model": "Qwen/..." }
```
`mode` values: `mock` · `openai_compat` · `vllm_inprocess`

---

## Fleet State

### `GET /api/fleet/kpis`
Current fleet KPIs.

**Response:**
```json
{
  "in_loading":   { "count": 3, "trucks": ["TA", "TB", "TC"] },
  "in_unloading": { "count": 1, "trucks": ["TD"] },
  "in_transit":   { "count": 2, "trucks": ["TE", "TF"] },
  "loaded_today": 14,
  "shift_id": "uuid"
}
```

---

### `GET /api/fleet/state`
Full current state per truck.

---

### `GET /api/fleet/site-analytics`
Per-site truck counts and current occupants.

---

## Messages

### `GET /api/messages`
Message-to-commit map (operator panel) with pagination.

**Query params:**
| Param | Default | Description |
|-------|---------|-------------|
| `page` | `1` | Page number |
| `limit` | `20` | Results per page |
| `status` | `all` | Filter: `all`, `committed`, `held`, `flagged`, `corrected` |
| `search` | — | Full-text search across message text, truck/site IDs |
| `hide_noise` | `false` | Hide NOISE-classified messages |
| `shift_id` | — | Filter to messages with events in this shift |

**Response items include:** `quoted_raw_text` — the text of the message this is replying to (null for non-replies).

```json
{
  "items": [
    {
      "msg_id": "uuid",
      "raw_text": "LO",
      "quoted_raw_text": "D LS KN4",
      "sender_name": "+91...",
      "timestamp_iso": "...",
      "events": [
        {
          "event_id": "uuid",
          "truck_id": "TD",
          "status": "LO",
          "site_id": "KN4",
          "confidence": 0.75,
          "commit_status": "FLAGGED",
          "reasoning": "truck from reply chain (D LS KN4)",
          "inferred": false
        }
      ]
    }
  ],
  "total": 842,
  "page": 1,
  "pages": 43
}
```

### `GET /api/commits-log`
Ordered event log with source messages.

**Query params:** `page`, `limit`, `truck_id`, `site_id`, `status`, `commit_status`, `search`, `shift_id`

Response items include `quoted_raw_text` (text of quoted/replied-to message, shown in UI as `↩ ...` prefix).

---

### `POST /api/commits`
Create a manual commit (no source message).

**Request body:**
```json
{
  "truck": "D",
  "status": "LS",
  "site": "SOC",
  "timestamp_iso": "2026-03-24T17:00:00+05:30",
  "note": "Manually entered — operator confirmed verbally"
}
```

---

### `PATCH /api/commits/{event_id}`
Edit an existing commit.

**Request body (all fields optional):**
```json
{
  "truck_id": "TD",
  "status": "LO",
  "site_id": "SOC",
  "timestamp_iso": "2026-03-24T17:30:00+05:30"
}
```

---

### `DELETE /api/commits/{event_id}`
Soft-delete (sets `commit_status = DELETED`).

---

## HITL Queue

### `GET /hitl/queue`
List open questions.

**Query params:** `limit` (default 50), `offset` (default 0)

**Response:**
```json
{
  "questions": [
    {
      "question_id": "uuid",
      "question_type": "UNKNOWN_SITE",
      "question_text": "Site 'XY' not recognised...",
      "context": { "site_alias": "XY", "raw_text": "H LS XY" },
      "status": "OPEN",
      "created_at": "..."
    }
  ],
  "count": 3,
  "type_counts": { "UNKNOWN_SITE": 2, "LOW_CONFIDENCE": 1 }
}
```

---

### `POST /hitl/answer`
Submit an answer.

**Request body:**
```json
{
  "question_id": "uuid",
  "answer": "SOC",
  "answered_by": "operator"
}
```

**Answer interpretation by type:**

| `question_type` | Answer format | Action |
|----------------|---------------|--------|
| `UNKNOWN_TRUCK` | Existing code: `TA` | Add alias, commit event |
| `UNKNOWN_TRUCK` | New truck: `new:TX:Display:alias` | Create truck, commit event |
| `UNKNOWN_TRUCK` | Free text | Re-process with clarification |
| `UNKNOWN_SITE` | Site code: `SOC` | Update event site_id, commit |
| `UNKNOWN_SITE` | Full message: `D LS SOC` | Re-process with clarification |
| `UNKNOWN_SITE` | New site: `new:ID:Name:loading:alias` | Create site, commit |
| `UNKNOWN_SITE` | Free text | Re-process with clarification |
| `LOW_CONFIDENCE` | `CONFIRM` | Force-commit the held event |
| `LOW_CONFIDENCE` | Anything else | Re-process with clarification |
| `CORRECTION_AMBIGUOUS` | Any text | Re-process with clarification |

**Response:**
```json
{ "status": "ok", "question_id": "uuid" }
```
or:
```json
{ "status": "reprocessing", "question_id": "uuid" }
```

---

### `POST /hitl/dismiss/{question_id}`
### `POST /hitl/{question_id}/skip`
Dismiss without answering.

---

### `POST /hitl/{question_id}/answer`
Answer by path param (same body as `POST /hitl/answer`).

---

## Registry

### `GET /api/registry/trucks`
List all trucks with aliases.

### `POST /api/registry/trucks`
Create truck. Body: `{ "truck_id": "TH", "display_name": "Truck H", "aliases": ["H", "h"] }`

### `PUT /api/registry/trucks/{truck_id}`
Update truck. Body: `{ "display_name": "...", "aliases": [...], "is_active": true }`

### `GET /api/registry/sites`
List all sites.

### `POST /api/registry/sites`
Create site. Body: `{ "site_id": "SOC", "display_name": "SOC", "site_type": "loading", "aliases": ["soc"] }`

### `PUT /api/registry/sites/{site_id}`
Update site. Body: `{ "display_name": "...", "site_type": "unloading", "aliases": [...] }`

### `GET /api/registry/shifts-config`
List shift configuration.

### `PUT /api/registry/shifts-config/{shift_number}`
Update shift. Body: `{ "start_time": "06:00", "expected_end": "09:00", "wa_keyword": "s1" }`

---

## Analytics

### `GET /analytics/shift-summary`
Shift summary for the dashboard report panel.

**Query params:** `shift_id` (optional — defaults to current active shift)

**Response:**
```json
{
  "shift_id": "uuid",
  "shift_name": "Shift 1",
  "started_at": "2026-04-14T06:00:00",
  "ended_at": null,
  "total_loaded": 14,
  "loaded_by_site":   { "KN4": { "name": "Kua No. 4", "count": 8 }, "SOC": { "name": "SOC", "count": 6 } },
  "reached_by_site":  { "BG":  { "name": "Bhandara Ground", "count": 12 } },
  "unloaded_by_site": { "BG":  { "name": "Bhandara Ground", "count": 10 } },
  "in_loading":   [{ "truck_id": "TA", "truck_alias": "A" }],
  "in_unloading": [],
  "truck_cycles": [{ "truck_id": "TA", "alias": "A", "loads": 5, "unloads": 4 }],
  "text": "── Shift 1 summary ──\nTotal Trolleys Loaded (all sites) = 14\n  Trolleys Loaded @Kua No. 4 = 8\n..."
}
```

### `GET /analytics/shifts`
All shifts ordered newest-first, for the dashboard shift selector.

**Response:**
```json
{
  "shifts": [
    { "shift_id": "uuid", "shift_number": 3, "shift_name": "Shift 3", "started_at": "...", "ended_at": null, "active": true },
    { "shift_id": "uuid", "shift_number": 2, "shift_name": "Shift 2", "started_at": "...", "ended_at": "...", "active": false }
  ]
}
```

### `GET /analytics/gantt`
Per-truck loading cycles for the Gantt chart, grouped into ENTER→LS→LO→LEFT sequences.

**Query params:** `shift_id` (optional — defaults to current active shift)

**Response:**
```json
{
  "shift_id": "uuid",
  "shift_name": "Shift 1",
  "shift_start": "2026-04-14T06:00:00",
  "shift_end": null,
  "trucks": [
    {
      "truck_id": "TA", "truck_name": "A",
      "avg_min": 42.5, "total_loads": 3,
      "cycles": [
        {
          "cycle_number": 1,
          "enter": { "event_id": "...", "status": "ENTER", "timestamp_effective": "...", "inferred": false, ... },
          "ls":    { "event_id": "...", "status": "LS",    "timestamp_effective": "...", "inferred": false, ... },
          "lo":    { "event_id": "...", "status": "LO",    "timestamp_effective": "...", "inferred": false, ... },
          "left":  null
        }
      ]
    }
  ]
}
```

Inferred events (pipeline auto-injected) have `inferred: true` and are used for cycle geometry but not rendered as visual markers in the UI.

---

## System

### `GET /health`
Liveness check. `{ "status": "ok", "ws_clients": 2 }`

### `GET /api/status`
Detailed status including LLM and WA listener health.

### `POST /api/dev/broadcast`
Dev-only: trigger a WS broadcast manually. Query param: `event_type`
