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
Message-to-commit map with pagination.

**Query params:**
| Param | Default | Description |
|-------|---------|-------------|
| `limit` | `50` | Results per page |
| `offset` | `0` | Pagination offset |
| `truck_id` | — | Filter by truck |
| `site_id` | — | Filter by site |
| `status` | — | Filter by event status |
| `hide_noise` | `false` | Hide NOISE-classified messages |

**Response:**
```json
{
  "items": [
    {
      "msg_id": "uuid",
      "raw_text": "D LS SOC",
      "sender_name": "+91...",
      "timestamp_iso": "...",
      "events": [
        {
          "event_id": "uuid",
          "truck_id": "TD",
          "status": "LS",
          "site_id": "SOC",
          "confidence": 0.95,
          "commit_status": "COMMITTED",
          "reasoning": "explicit D LS SOC",
          "inferred": false
        }
      ]
    }
  ],
  "total": 842,
  "offset": 0,
  "limit": 50
}
```

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
Current shift summary used in the dashboard report panel.

**Response:**
```json
{
  "shift_id": "uuid",
  "shift_name": "Shift 1",
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

---

## System

### `GET /health`
Liveness check. `{ "status": "ok", "ws_clients": 2 }`

### `GET /api/status`
Detailed status including LLM and WA listener health.

### `POST /api/dev/broadcast`
Dev-only: trigger a WS broadcast manually. Query param: `event_type`
