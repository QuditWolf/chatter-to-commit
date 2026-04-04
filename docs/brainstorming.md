# Brainstorming & Design Decisions

## Reply-to-Context for WhatsApp Messages (2026-04-05)

### Problem
Operators use WhatsApp reply threading to add status updates or corrections to existing truck messages. The system was ignoring this metadata.

### Real Examples from Production Data (chat.txt / level2.json)

**Reply patterns (inferred from message sequences):**
```
"D enter kn4" → reply "LS" → means D LS @KN4
"B,C enter soc" → reply "LO" → means B LO @SOC + C LO @SOC
"D enter kn4" → reply "LEFT" → means D LEFT @KN4
```

**Correction patterns found in data:**
```
"Sorry C" — "sorry, it was C not what I said"
"It was D not Massy" — explicit truck correction
"D is not massey, no label on massey yet" — truck identity correction
"Correction *SoC" — site correction (Telegram-style)
"Not in BG or Dairy" — site negation
"B is not loading." — status negation
```

**Summary/count requests found in data:**
```
"What was the total count of last evening?"
"Count yesterday evening?"
"Tamanna pls give the total count"
"Please report the total count"
"Total count of this shift please"
"Send total count" / "Send total count pls"
"Back End team pls provide the total count"
"Backend team pls report the total count."
"Total count ?"
"How many trolleys in this shift are available?"
```

**Standalone "Loading over" / "Unloading over"** — appears hundreds of times. This is a shift-level announcement, NOT a truck status. When standalone (not part of "D LO"), it means all loading is over for that shift → should trigger summary.

### Design Decisions

1. **Reply confidence**: 0.82 cap — committed but flagged for review (amber highlight)
   - User approved: "all reply based messages can be committed but slightly reduce the confidence so that they get committed but get flagged for review"

2. **Correction detection**: When reply contains "sorry", "not", "actually", "correction", "it was", "my bad", "oops", "wrong", "mistake"
   - User approved: "A, yes sorry and C not D implies a correction event"

3. **Summary triggers**: Standalone "loading over" / "unloading over" (no truck letter before it), or messages with count/summary keywords
   - User approved: "when it is a standalone message not part of truk. standalone and not a reply means that all loading is over for that shift. yes."

4. **Anti-loop protection**: WA listener already has `if (msg.key.fromMe) continue;` — bot's messages are never re-ingested
   - User concern: "ensure that there is not a loop where we read our own message and keep sending replies"
   - Already handled by existing code

5. **Summary format**: Same as frontend's copyable summary, emoji-free
   - User approved: "emoji free but same format as copyable summary"

6. **Correction via reply**: New event with `corrects_event_id` pointing to original
   - User approved: "yes a new event. correction when it is like A LS not B or something like that"

### Implementation Flow

```
WA Message (reply to msg X)
  → Ingest: stores quoted_wa_message_id in raw_messages
  → Level 2: _get_reply_context() looks up msg X's events
  → Level 3: prompt includes reply context section
  → LLM: uses context to resolve missing fields
  → Committer: caps confidence at 0.82, sets inferred=true
  → Result: committed but FLAGGED (amber in UI)
```

### Example Flows

| Original | Reply | Result |
|----------|-------|--------|
| `D enter kn4` | `LS` | D LS @KN4, conf 0.82, FLAGGED |
| `D enter kn4` | `D LS` | D LS @KN4, conf 0.85, COMMITTED |
| `B,C enter soc` | `LO` | B LO @SOC + C LO @SOC, conf 0.82, FLAGGED |
| `D enter kn4` | `Sorry C` | Correction: D→C, conf 0.80, COMMITTED |
| `Loading over` | (standalone) | Summary posted to WA group |
| `Total count?` | (any) | Summary posted to WA group |

---

## Auto-post Shift Summary (2026-04-05)

### Problem
Operators manually ask for counts/summaries. The system should auto-post when asked or when shift ends.

### Design Decisions

1. **Triggers**:
   - Standalone "loading over" / "unloading over" (no truck letter)
   - Messages with summary/count keywords
   - Shift end (auto or manual)

2. **Anti-loop**: WA listener skips `msg.key.fromMe` — bot's own messages never re-ingested

3. **Format**: Same as frontend copyable summary, emoji-free

4. **Endpoints**:
   - Added `/send-message` to WA listener (plain message, no quote)
   - `wa_notifier.send_summary_to_group()` generates and posts

---

## Admin Edit/Delete (2026-04-05)

### Problem
No way to edit or delete trucks/sites from the admin UI.

### Implementation
- DELETE endpoints in `registry.py` with FK constraint checks
- Edit/delete buttons in frontend tables
- Edit: prompt dialog for display name
- Delete: confirmation dialog, blocks if records reference the entity

---

## LLM Quality Fixes (2026-04-05)

### Issues Found and Fixed
1. **Stale KV cache**: Fixed by `--no-cache-prompt` flag
2. **Status field missing**: Prompt improvement + VALID_STATUSES guard
3. **site_alias without site_id**: Fixed in committer normalization
4. **Wrong timestamp key**: Fixed in committer normalization
5. **Non-canonical msg_type**: Fixed in parse_llm_output normalization
6. **Short commit key**: Fixed in parse_llm_output normalization
7. **Flat JSON for multi-truck**: Fixed in _extract_partial_events recovery

### Test Results
- 10 test messages through real LLM
- All known issues resolved
- Average LLM time: 9.5s (vs documented 60-150s)

---

## Docker Deployment Fixes (2026-04-05)

### Issues Found and Fixed
1. **docker-compose.yml defaults**: Updated to match Qwen2.5-32B config
2. **.env file**: Added missing variables
3. **.env.example**: Updated model name, max tokens, improved comments
4. **Makefile**: Fixed fragile `reset-wa-session` target
