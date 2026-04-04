# Fleet Tracker — Qwen Context

> This file mirrors CLAUDE.md and is maintained by Qwen. It includes all decisions, progress, and brainstorming notes from Qwen's work on this project.

## What This Project Is

A **real-time operational tracking system** for a paddy-harvesting tractor-trolley fleet. Operators send terse, typo-ridden WhatsApp messages like `"D LS SOC"` or `"A,B enter KN4"` to a group. This system intercepts every message, parses it into structured fleet events using a 3-stage LLM pipeline, and maintains live fleet state on a dashboard.

- **Vehicles** are called "trucks" in code, "trolleys" in the UI. Don't conflate.
- **Production group JID**: `120363425655528115@g.us`
- **LLM**: Qwen2.5-32B-Instruct-Q4_K_M (GGUF, llama.cpp) on `10.0.0.4:8001`. ~9.5s per message.
- **POC**: Validated on 6,316 historical messages before production hardening.

---

## Architecture Overview

```
WhatsApp Group
    ↓
Node.js Baileys Listener (wa_listener/index.js, port 3001)
    ↓ POST /api/ingest/wa-message (202 instantly)
FastAPI (fleet_pipeline/api/main.py, port 8000)
    ↓ Background task
Pipeline: Level1 → Level2 → Level3 (LLM) → Committer → SQLite
    ↓ WebSocket broadcast
Frontend SPA (fleet_pipeline/frontend/index.html)
```

HITL loop: LLM uncertainty → bot quotes original WA message → operator replies → reply detected via `bot_wa_message_id` → event resolved.

---

## Key Files

| File | Role |
|------|------|
| `fleet_pipeline/api/main.py` | FastAPI app, WebSocket manager, auth middleware |
| `fleet_pipeline/api/pipeline_service.py` | Orchestrator: L1→L2→L3→Commit |
| `fleet_pipeline/pipeline/level1.py` | Timestamp extraction, raw message parsing |
| `fleet_pipeline/pipeline/level2.py` | Fuzzy truck/site matching, message classification, **reply context enrichment**, **summary detection** |
| `fleet_pipeline/pipeline/level3.py` | LLM prompt building + output parsing, **reply context injection** |
| `fleet_pipeline/pipeline/committer.py` | Post-LLM rules, cycle inference, HITL, DB writes, **reply confidence cap**, **correction detection** |
| `fleet_pipeline/pipeline/hitl_queue.py` | HITL question factory |
| `fleet_pipeline/pipeline/wa_notifier.py` | Bot clarifications via /send-reply, **summary posting via /send-message** |
| `fleet_pipeline/prompts/level3_prompt_template.txt` | LLM prompt — 15 rules (added R15 reply context), 14+ examples |
| `fleet_pipeline/db/schema.sql` | 13-table SQLite schema (+ `quoted_wa_message_id` column) |
| `fleet_pipeline/db/database.py` | CRUD helpers (~800 lines) |
| `fleet_pipeline/api/routes/ingest.py` | WA ingestion + HITL routing, **passes quoted_wa_message_id** |
| `fleet_pipeline/api/routes/shifts.py` | Shift control, **posts summary on end** |
| `fleet_pipeline/api/routes/registry.py` | Registry CRUD + **DELETE endpoints** |
| `fleet_pipeline/frontend/index.html` | Single-file SPA (~110 KB vanilla JS), **edit/delete buttons in admin** |
| `fleet_pipeline/wa_listener/index.js` | Baileys WS + /send-reply + **/send-message** |
| `fleet_pipeline/config.py` | Config loader (.env) |

---

## Truck Cycle & Status Values

```
ENTER → LS (Loading Started) → LO (Loading Over) → LEFT
ENTER → US (Unloading Started) → UO (Unloading Over) → LEFT
```

- ENTER→LEFT alone: confidence 0.45 (FLAGGED, not auto-committed — truck may have left empty)
- All other transitions at normal confidence thresholds

---

## Confidence Thresholds

| Value | Outcome |
|-------|---------|
| ≥ 0.85 | COMMITTED (auto-saved, no review) |
| 0.60–0.85 | FLAGGED (saved, amber in UI, review recommended) |
| < 0.60 | HELD (requires HITL resolution) |

**Reply-based events**: capped at 0.82 → committed but FLAGGED for review (amber highlight).

---

## Critical Engineering Decisions

### 1. `msg_id = wa_message_id`
Baileys key IDs used directly as `msg_id` — not generated UUIDs. Prevents duplicate rows when HITL re-processes the same original message. Enables 1:1 mapping for HITL reply routing.

### 2. `process_raw_text()` is synchronous
It blocks on the LLM HTTP call. **Must use `run_in_executor()`** in all async contexts. Do NOT call it directly in async functions. This pattern is in `ingest.py` — don't break it.

### 3. Sequential LLM calls (Semaphore)
Later messages need context from earlier ones. A `asyncio.Semaphore(1)` inside `_process_and_broadcast` serializes all LLM calls. This is intentional.

### 4. WebSocket broadcast timeout
`ws_manager.broadcast()` uses `asyncio.wait_for(ws.send_text(), timeout=2.0)`. This is required — without it, a stale WebSocket connection blocks all broadcasts (was causing 16s delays on `/wa-message`).

### 5. Cycle-aware site inference
Site inheritance is high-confidence (≤0.90) **only when the truck has an open cycle** at a known site. If the last event was LEFT/LO/UO (cycle closed), site_id=null → HITL question. This prevents silent commit errors on new cycles.

### 6. Single HITL per event
UNKNOWN_SITE suppresses LOW_CONFIDENCE — one bot message per ambiguous event, not two.

### 7. Pre-save raw message
Raw message is saved to DB **before** the LLM call. If LLM or committer fails, the message is never silently lost.

### 8. HITL via WhatsApp reply threading
Operator answers by replying to the bot's quoted message in WA — zero UI context switching. `bot_wa_message_id` in `hitl_queue` → O(1) reply detection.

### 9. L3 context window
`get_l3_context()` loads the last 20 COMMITTED/FLAGGED events before each LLM call. Loaded in `pipeline_service.py` before building the L3 prompt.

### 10. Site inference from sender history
When a truck's site is unknown, the committer checks for the most recent COMMITTED/FLAGGED event from the **same sender_id in the same shift** and inherits that site. Confidence is capped at 0.72 → always FLAGGED. Does NOT inherit across shifts. Implemented in `Committer._infer_site_from_sender_shift()`.

### 11. VALID_STATUSES guard
`committer.py` defines `VALID_STATUSES = {"ENTER","LS","LO","LEFT","US","UO","UNKNOWN"}`. Events with any other status are dropped before DB insert. Guards against LLM hallucinating non-existent status codes (e.g. `"B"`, `"truck"`, `"LOAD"`).

### 12. LLM output normalization in level3.py
The parser (`parse_llm_output`) normalizes before returning:
- **msg_type aliases**: `truck`/`truck_update`/`status` → `STATUS_UPDATE`; `tally`/`tally_update` → `TALLY_UPDATE`
- **commit key**: `"commit"` → `"commit_recommendation"` (32B often uses the short form; committer defaults to HOLD if key missing)
- **Flat-JSON recovery**: if the model emits truck/status fields at top level (no `events` array, common for multi-truck with duplicate keys), `_extract_partial_events()` walks `truck_id` occurrences and reconstructs the events list
- **Partial recovery**: character-by-character walk of truncated/corrupted `events` arrays

### 13. Auto-end shift (inactivity)
Background asyncio task in `lifespan` (main.py) checks every 5 minutes. If active shift has no messages for `AUTO_END_GAP` (3 hours), shift is ended and `shift_changed` is broadcast. **Now also posts summary to WA group before ending.**

### 14. Tallies are audit records, not fleet state
`TALLY_UPDATE` messages are stored in the `tallies` table with `commit_status=RECEIVED`. They do **not** write to `events`, do not increment `committed` counter, and do not affect fleet state at all. Tallies are human cross-checks against the events the system committed; they must never overwrite or duplicate that state.
- Summary key: `tally_received` (not `committed`)
- LLM prompt instructs `commit_recommendation=HOLD` for tallies
- Use the `tallies` table for future reconciliation/audit features

### 15. Committer event normalization
Before any validation, the committer normalises raw LLM event dicts to fix common model output defects:
1. String `"null"`/`"None"` → JSON `null` for `truck_id` and `site_id`
2. `"timestamp"` key renamed to `"timestamp_effective"` (model frequently uses wrong key)
3. `site_alias` → `site_id` via registry alias map (model often populates only `site_alias`)
4. `truck_alias` → `truck_id` via registry alias map (fallback when `truck_id` absent)

This runs before the existing alias-validation loop so downstream logic always sees clean FK values.

### 16. llama-server `--no-cache-prompt` is mandatory
Without this flag, llama-server reuses its KV cache across requests that share the same prompt prefix. Since every pipeline request uses the same rules+examples prefix, the server returns the *previous* request's response for the *current* input — producing completely wrong events. Always start llama-server with `--no-cache-prompt`.

### 17. Reply-to-Context (NEW — 2026-04-05)
Operators reply to messages in WhatsApp to add verbs or correct values. The system now:
- Stores `quoted_wa_message_id` in `raw_messages` table
- Level 2 looks up the original message and its resolved events via `_get_reply_context()`
- Level 3 injects reply context into the LLM prompt (via `l3_context_summary.reply_context`)
- Committer caps reply-based confidence at 0.82 (committed but flagged for review)
- Correction detection: replies with "sorry", "not", "actually", "it was" trigger `_detect_correction_from_reply()`
- Anti-loop: WA listener already skips `msg.key.fromMe` — bot's own messages never re-ingested

### 18. Auto-post Shift Summary (NEW — 2026-04-05)
When someone sends "Loading over", "summary?", "total count?", or when a shift ends (auto or manual), the system generates and posts a shift summary to the WhatsApp group:
- Level 2 detects `SUMMARY_REQUEST` message type (standalone "loading over", "unloading over", or count/summary keywords)
- Pipeline service calls `wa_notifier.send_summary_to_group()`
- WA listener has new `/send-message` endpoint (plain message, no quote)
- Also posted on manual shift end (`shifts.py`) and auto-end (`main.py`)
- Summary format: same as frontend copyable summary, emoji-free

---

## LLM Configuration

**Current model**: `Qwen2.5-32B-Instruct-Q4_K_M.gguf` (switched from GLM-4.7-Flash on 2026-04-03)

**llama-server command** (Qwen2.5-32B, validated 2026-04-04):
```bash
CUDA_VISIBLE_DEVICES=0,1 ./llama.cpp/llama-server \
  -m models/qwen32b/Qwen2.5-32B-Instruct-Q4_K_M.gguf \
  --ctx-size 16384 \
  -ngl 999 \
  -ts 1,1 \
  -b 512 \
  -ub 512 \
  --temp 0.1 \
  -n 1200 \
  --no-cache-prompt \
  --port 8001
```

**Key flags**:
- `-b 512 -ub 512` — batch/microbatch size. Running `-b 16` makes prefill ~30× slower
- `-n 1200` — must match `FLEET_LLM_MAX_TOKENS`; if server `-n < client max_tokens` output is truncated
- `--no-cache-prompt` — **MANDATORY** — see decision #16 above
- `--temp 0.1` on server is overridden by client `FLEET_LLM_TEMPERATURE=0.1`; either is fine

**Key env vars**:
- `FLEET_LLM_MAX_TOKENS=1200` — enough for Qwen2.5; higher causes hallucination loops
- `FLEET_LLM_TIMEOUT=120` — Qwen2.5 is faster than GLM
- `FLEET_LLM_TEMPERATURE=0.1` — slight randomness; `0` for fully deterministic
- `FLEET_LLM_MOCK=true` — enables rule-based mock mode (no LLM, for dev/test)

---

## Deployment

Docker Compose (3 services: `api`, `wa`, `nginx`):
```bash
cp .env.example .env   # fill in: HOST_PORT, FLEET_LLM_BASE_URL, WA_GROUP_JID, FLEET_AUTH_PASSWORD
make up                # build + start
make qr                # show QR code (first run, Baileys auth)
```

Nginx proxies `/ws` with WebSocket upgrade and a 24h read timeout (for persistent dashboard connections).

---

## What NOT to Change

Do not refactor these unless a bug is found:
- **Level1/2/3 logic** — parsing chain is stable and validated on production data
- **Confidence scoring** — thresholds tuned against historical messages
- **Simulation mode** — used for regression testing
- **Registry data model** — trucks/sites schema is relied upon by multiple pipeline stages
- **The `msg_id = wa_message_id` pattern** — changing this breaks HITL deduplication

---

## Project Timeline

| Date | Milestone |
|------|-----------|
| 2026-03-24 | Initial commit — core pipeline, WA listener, SQLite schema |
| 2026-03-24 | Full truck cycle spec in L3 prompt, 10 inference rules |
| 2026-03-23 | Production hardening: HITL via WA replies, shifts, corrections, WebSocket, frontend rebuild |
| 2026-03-31 | Docker, better README, LLM error handling, dashboard status fixes, processing animation |
| 2026-04-03 | Switched to Qwen2.5-32B; site inference from sender/shift; auto-end shift; VALID_STATUSES guard; LLM output normalization + partial/flat-JSON recovery |
| 2026-04-04 | Prompt rewrite (14 rules, 14+ examples, PROHIBITIONS block); committer normalization; stale KV-cache bug found + fixed (`--no-cache-prompt`); tallies changed to audit-only |
| 2026-04-05 | Reply-to-context feature; auto-post shift summary; admin edit/delete for trucks/sites; LLM quality regression test (all issues resolved); Docker deployment config fixed |

---

## Unresolved Problems & Known LLM Quality Issues (as of 2026-04-05)

### Structural issues (code-level, fixable)

| Problem | Root cause | Status |
|---------|-----------|--------|
| `committed=2` for a single-event message | When DB has a prior open cycle, committer inserts inferred ENTER + new event | By design |
| Context pollution with 20-event window | LLM may re-output prior events from full DB context | Open |

### LLM quality issues (Qwen2.5-32B-Q4_K_M)

| Symptom | Detail | Fix |
|---------|--------|-----|
| **Stale KV cache responses** | Server returns previous request's output | **`--no-cache-prompt`** — FIXED |
| **`status` field missing** | Model occasionally omits status key | VALID_STATUSES guard; prompt improvement |
| **`site_alias` present, `site_id` absent** | Model outputs alias but not ID | Fixed in committer normalization |
| **`"timestamp"` instead of `"timestamp_effective"`** | Wrong key name | Fixed in committer normalization |
| **Non-canonical `msg_type`** | `"truck_update"` instead of `"STATUS_UPDATE"` | Fixed in `parse_llm_output` |
| **`"commit"` instead of `"commit_recommendation"`** | Short-form key | Fixed in normalization |
| **Flat JSON for multi-truck** | Duplicate top-level keys | Fixed in `_extract_partial_events` |
| **LEFT/LO without context → HELD** | Low confidence on fresh DB | Acceptable behaviour |

### What works reliably

- Noise phrases → NOISE
- Single-truck explicit status + site → COMMITTED
- Multi-truck ENTER/LS/LO → N× COMMITTED
- Multi-verb single truck → N× COMMITTED
- Tally messages → RECEIVED in tallies table, no fleet state change
- Unknown truck → STATUS_UPDATE, HELD
- Missing site with open cycle → COMMITTED (site inferred)
- **Reply context**: `"D enter kn4"` → reply `"LS"` → D LS @KN4, conf 0.82, FLAGGED
- **Summary requests**: "Loading over", "total count?" → posts summary to WA group

---

## LLM Quality Regression Test (2026-04-05)

**All known issues resolved.** Test results:
- Stale KV cache: ✅ FIXED
- Status field missing: ✅ FIXED (0/13 events)
- site_alias without site_id: ✅ FIXED
- Low confidence errors: ✅ EXPECTED (only unknown truck at 0.40)
- Average LLM time: 9.5s (much faster than documented 60-150s)

---

## Brainstorming & User Discussions

### Reply-to-Context Feature (2026-04-05)

**User request**: Operators reply to messages in WhatsApp to add verbs or correct values.

**Real examples from chat.txt**:
- `"D enter kn4"` → reply `"LS"` → D LS @KN4
- `"B,C enter soc"` → reply `"LO"` → B LO @SOC + C LO @SOC
- `"Sorry C"` replying to `"D enter kn4"` → correction: D→C
- `"It was D not Massy"` → correction: Massy→D

**Design decisions**:
1. Reply confidence capped at 0.82 — committed but flagged (amber highlight in UI)
2. Corrections detected via keywords: "sorry", "not", "actually", "it was", "my bad", "oops", "wrong", "mistake"
3. `"Sorry C"` replying to `"D enter kn4"` → creates CORRECTION event (D→C)
4. New event for time progression, correction for identity fixes
5. LLM prompt gets R15 rule explaining reply context usage

### Auto-post Shift Summary (2026-04-05)

**User request**: Post shift summary to WA group when asked or when shift ends.

**Real examples from chat.txt**:
- "What was the total count of last evening?"
- "Tamanna pls give the total count"
- "Please report the total count"
- "Total count of this shift please"
- "Send total count" / "Send total count pls"
- "Back End team pls provide the total count"
- "Loading over" (hundreds of occurrences — standalone = shift end)
- "Loading Over At KN4" (site-specific)

**Design decisions**:
1. Summary triggers: standalone "loading over"/"unloading over", or count/summary keywords
2. Must NOT contain a truck letter + status (those are fleet events, not summary requests)
3. Same format as frontend's copyable summary, emoji-free
4. Anti-loop: WA listener skips `msg.key.fromMe` — bot's own messages never re-ingested
5. Also posted on shift end (auto-end and manual)

### Admin Edit/Delete (2026-04-05)

**User request**: Add options to fully edit and delete sites and trucks in the admin portal.

**Implementation**:
- DELETE endpoints in `registry.py` (with FK constraint checks — blocks if records reference)
- Edit/delete buttons in frontend truck and site registry tables
- Edit uses prompt dialog for display name changes
- Delete shows confirmation dialog

---

## Useful Utilities

```bash
python reset_data.py              # Drop all tables, re-seed (clean slate)
python verify_pending.py          # List all HELD/FLAGGED events
python test_llm_regression.py     # LLM quality regression test (real LLM)
node test_frontend.js             # Frontend E2E test (Playwright)
```
