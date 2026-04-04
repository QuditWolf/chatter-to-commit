# Fleet Tracker — Message Types, Flows & Features

## Overview

This document catalogs every message type the pipeline can handle, the flow each takes through the system, and all user-facing features.

---

## Message Type Classification (Level 2)

Every incoming WhatsApp message is classified into one of these types by Level 2:

| Type | Description | Example |
|------|-------------|---------|
| `STATUS_LIKE` | Contains truck alias + status keyword | `D LS kn4` |
| `TALLY_LIKE` | Contains tally/count data | `Trolleys Loaded @KN4 = 5` |
| `QUERY_LIKE` | Contains question mark | `Where is A?` |
| `NOISE_LIKE` | Acknowledgement, greeting, empty | `ok`, `yes`, `👍` |
| `CORRECTION_LIKE` | Edited message or correction pattern | `D is not massey <edited>` |
| `SHIFT_SIGNAL` | Shift control keywords | `s1`, `shift start`, `pehli pali` |
| `SUMMARY_REQUEST` | Asking for count or standalone "loading over" | `Total count?`, `Loading over` |
| `DELETED` | WhatsApp deleted message | *(system message)* |

---

## Complete Message Flow

```
WhatsApp Group
    ↓
[WA Listener] Baileys receives message
    ├─ Extracts: wa_message_id, sender_phone, sender_name, raw_text
    ├─ Detects: quoted_wa_message_id (reply threading)
    ├─ Skips: msg.key.fromMe (own bot messages — anti-loop)
    └─ POST → /api/ingest/wa-message (202 instantly)
         ↓
[Ingest Route] /api/ingest/wa-message
    ├─ Stores in wa_messages table
    ├─ HITL check: does quoted_wa_message_id match open HITL question?
    │   ├─ YES → route to _handle_wa_hitl_answer (skip pipeline)
    │   └─ NO  → continue to pipeline
    ├─ Broadcast: message_received (WebSocket)
    └─ Background task: _process_and_broadcast
         ↓
[Pipeline Service] process_raw_text()
    ├─ Level 1: Parse raw text → level1_msg dict
    ├─ Save raw message to DB (pre-save, never lost)
    ├─ Level 2: Enrich with heuristic features
    │   ├─ Detect: trucks, sites, status keywords
    │   ├─ Classify: message type
    │   ├─ Context: time deltas, sender history
    │   ├─ Reply context: look up original message events (if quoted)
    │   └─ Summary detection: standalone "loading over", count requests
    ├─ Level 3: LLM inference
    │   ├─ Build prompt: rules + examples + context + reply context
    │   ├─ Call LLM (Qwen2.5-32B via llama.cpp)
    │   ├─ Parse JSON output
    │   ├─ Sanitize: injection detection, field validation, confidence clamping
    │   └─ Archive: raw LLM output → llm_outputs table
    ├─ Committer: Apply rules, write to DB
    │   ├─ Normalize events (string-null, timestamp key, alias→id)
    │   ├─ Infer ENTER if needed
    │   ├─ Infer site from sender/shift history
    │   ├─ Apply reply confidence cap (0.82)
    │   ├─ Detect corrections from reply
    │   ├─ Determine commit status (COMMITTED/FLAGGED/HELD)
    │   ├─ Write events to DB
    │   └─ Create HITL questions if needed
    ├─ WA Notifier: Send HITL clarifications to group
    ├─ WA Notifier: Post summary if SUMMARY_REQUEST
    ├─ Broadcast: commit_created (WebSocket)
    └─ Audit: Log full pipeline trace
         ↓
[Frontend] WebSocket receives broadcast
    ├─ message_received → show pending in message map
    ├─ commit_created → update fleet table, KPIs, message map
    ├─ fleet_state_updated → refresh fleet state
    └─ hitl_created → update HITL queue badge
```

---

## Message Patterns the Pipeline Handles

### 1. Single Truck Status
```
Input:  "D LS kn4"
Output: TD LS @KN4, confidence 0.95, COMMITTED
```

### 2. Multi-Truck Same Action
```
Input:  "A,B,C enter kn4"
Output: TA ENTER @KN4 + TB ENTER @KN4 + TC ENTER @KN4, all COMMITTED
```

### 3. Multi-Verb Single Truck
```
Input:  "A enter LS kn4"
Output: TA ENTER @KN4 + TA LS @KN4, both COMMITTED
```

### 4. Missing Site (Open Cycle)
```
Context: B has open cycle at SOC (last event = ENTER @SOC)
Input:   "B LS"
Output:  TB LS @SOC (site inferred), confidence 0.90, COMMITTED
```

### 5. Missing Site (Closed Cycle)
```
Context: B's last event was LEFT @SOC (cycle closed)
Input:   "B LS"
Output:  TB LS, site=null, confidence 0.55, HELD + HITL question
```

### 6. Unknown Truck
```
Input:  "XYZ left kn4"
Output: truck_id=null, confidence 0.45, HELD + UNKNOWN_TRUCK HITL
```

### 7. Unknown Site
```
Input:  "D LS newplace"
Output: TD LS, site_id=null, site_alias="newplace", confidence 0.55, HELD + UNKNOWN_SITE HITL
```

### 8. Tally Message
```
Input:  "Trolleys Loaded @KN4 = 5\nTrolleys Left @KN4 = 5"
Output: Stored in tallies table as RECEIVED, no fleet state change
```

### 9. Noise
```
Input:  "ok", "yes", "👍", "Loading Over" (standalone, no truck)
Output: msg_type=NOISE, no events, no fleet state change
```

### 10. Shift Signal
```
Input:  "s1", "shift start", "pehli pali"
Output: Shift detected/started, no fleet events
```

### 11. Reply-to-Context (NEW)
```
Original: "D enter kn4"
Reply:    "LS"
Output:   TD LS @KN4 (truck+site from original), confidence 0.82, FLAGGED
```

### 12. Multi-Truck Reply
```
Original: "B,C enter soc"
Reply:    "LO"
Output:   TB LO @SOC + TC LO @SOC, confidence 0.82, FLAGGED
```

### 13. Correction via Reply
```
Original: "D enter kn4"
Reply:    "Sorry C"
Output:   Correction event: truck D→C, confidence 0.80, COMMITTED
```

### 14. Summary Request
```
Input:  "Loading over" (standalone, no truck)
Output: Shift summary generated and posted to WA group
```

### 15. Count Request
```
Input:  "Total count?", "Send total count pls"
Output: Shift summary generated and posted to WA group
```

### 16. LLM Unavailable
```
Input:  Any message when LLM is down
Output: Event with confidence 0.0, reasoning "LLM unavailable", HELD
        Can be reprocessed via /api/ingest/reprocess-held
```

### 17. HITL Answer via WhatsApp Reply
```
Bot sends: "❓ Unknown truck — 'XYZ' not in registry. Reply with truck code..."
Operator replies to bot message: "TB"
Output:  HITL answered, event reprocessed with truck_id=TB
```

### 18. Vehicle Number Trucks
```
Input:  "UP26AB7192 Entered SOC"
Output: T_UP26 ENTER @SOC, confidence 0.95, COMMITTED
```

### 19. Named Trucks
```
Input:  "ArjunWhite LS KN4"
Output: T_ARJ_WHITE LS @KN4, confidence 0.95, COMMITTED
```

### 20. Typo Correction
```
Input:  "D lefy kn4"
Output: TD LEFT @KN4 (typo detected), confidence 0.90, COMMITTED
```

---

## Features

### Core Pipeline
| Feature | Description |
|---------|-------------|
| 3-Stage LLM Pipeline | Level1 (parse) → Level2 (enrich) → Level3 (LLM) → Committer |
| Confidence Scoring | 3 tiers: COMMITTED (≥0.85), FLAGGED (0.60-0.85), HELD (<0.60) |
| Site Inference | From open cycle, sender/shift history, reply context |
| ENTER Inference | Auto-inserts ENTER before LS/US when no prior ENTER exists |
| Multi-Truck Parsing | Comma-separated trucks → one event per truck |
| Multi-Verb Parsing | Multiple statuses → one event per status |
| Typo Tolerance | Fuzzy matching for status keywords and aliases |
| LLM Output Sanitization | Injection detection, field validation, confidence clamping |
| LLM Output Archive | All raw LLM outputs stored for forensic recovery |

### Reply-to-Context (NEW)
| Feature | Description |
|---------|-------------|
| Reply Tracing | Looks up original message and its resolved events |
| Context Injection | Reply context added to LLM prompt |
| Confidence Cap | Reply-based events capped at 0.82 (committed but flagged) |
| Correction Detection | Keywords trigger correction logic |

### Auto-post Summary (NEW)
| Feature | Description |
|---------|-------------|
| Summary Detection | Standalone "loading over", count/summary keywords |
| WA Group Posting | Summary posted to WhatsApp group |
| Shift End Posting | Summary posted on manual and auto shift end |
| Anti-loop Protection | Bot messages never re-ingested |

### HITL (Human-in-the-Loop)
| Feature | Description |
|---------|-------------|
| WhatsApp Reply Threading | Operator replies to bot's quoted message |
| Question Types | UNKNOWN_TRUCK, UNKNOWN_SITE, LOW_CONFIDENCE, CORRECTION_AMBIGUOUS |
| Bot Formatting | Concise, actionable messages with reply options |
| Natural Language | Operator can reply in plain English |

### Dashboard
| Feature | Description |
|---------|-------------|
| Real-time Fleet State | WebSocket broadcast, live updates |
| KPI Tiles | In loading, unloading, transit, loaded today |
| Site Analytics | Per-site load/unload counts |
| Shift Summary | Copyable text summary |
| Message Map | Raw message → parsed events mapping |
| Commit Log | Full commit history with corrections |
| System Health | LLM, WA, DB status indicators |
| NL Query | Natural language questions about fleet state |

### Admin
| Feature | Description |
|---------|-------------|
| Truck Registry | View, add, edit, delete trucks |
| Site Registry | View, add, edit, delete sites |
| Shift Config | Configure shift times and WA keywords |
| Edit/Delete | Full CRUD for registry entries |

### Data Integrity
| Feature | Description |
|---------|-------------|
| Pre-save Raw Messages | Raw text saved before LLM call — never lost |
| LLM Output Archive | All raw outputs stored in llm_outputs table |
| Audit Log | All DB mutations logged |
| Sanitization | Injection detection, field validation |
| Forensic Recovery | Raw LLM output retrievable for re-parsing |
| Correction History | Append-only, never overwrites events |

---

## Anti-Loop Protection

The system has multiple layers of protection against infinite reply loops:

1. **WA Listener**: `if (msg.key.fromMe) continue;` — bot's own messages are never processed
2. **HITL Reply Routing**: Only replies matching open HITL questions are routed as answers
3. **Summary Posting**: Uses `/send-message` (plain message), not a reply — no quote ID to trigger loops
4. **Message Deduplication**: `msg_id = wa_message_id` prevents duplicate processing

---

## Error Handling

| Error | Behavior |
|-------|----------|
| LLM unavailable | Event saved with confidence 0.0, HELD, reprocessable |
| LLM timeout | Same as above |
| LLM returns invalid JSON | Partial recovery attempted, fallback to ERROR |
| Sanitizer blocks output | Treated as LLM error, HELD |
| DB write fails | Transaction rolled back, error logged |
| WA notification fails | Logged, processing continues |
| Summary posting fails | Logged, shift end continues |

---

## Data Retention

All data is retained and retrievable:

| Table | Retention | Recovery Path |
|-------|-----------|---------------|
| raw_messages | Permanent | Direct query by msg_id |
| events | Permanent | Direct query, corrections preserved |
| llm_outputs | Permanent | Raw LLM text + parsed JSON + sanitization issues |
| audit_log | Permanent | Full mutation history |
| corrections | Permanent | Append-only correction chain |
| hitl_queue | Permanent | Full Q&A history |
| tallies | Permanent | Audit trail for human counts |
