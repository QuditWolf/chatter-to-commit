# Work Diary — Fleet Tracker

## 2026-04-05 Session — Complete Log

### Phase 1: LLM Quality Regression Test
- Ran 10 test messages through real LLM (Qwen2.5-32B on 10.0.0.4:8001)
- All known issues from CLAUDE.md verified as resolved
- Average LLM time: 9.5s (vs documented 60-150s)
- KV cache staleness: FIXED (--no-cache-prompt working)
- Status field missing: FIXED (0/13 events)
- site_alias without site_id: FIXED (committer normalization)

### Phase 2: Frontend E2E Test
- Created test_frontend.js with Playwright
- 40/40 tests passed across all tabs
- Screenshots captured for dashboard, operator, commits, admin

### Phase 3: Docker Deployment Audit & Fixes
- docker-compose.yml defaults updated for Qwen2.5-32B
- .env populated with all required variables
- .env.example updated
- Makefile reset-wa-session fixed
- Committed and pushed all fixes

### Phase 4: Registry Population
- Extracted trucks and sites from level2.json (real chat data)
- Added 17 trucks via API (A-V single letters + UP26 + UP80)
- Added 9 sites via API (KN4, SOC, BG, DAIRY, LG, SKP, PL, TN, BINAI)

### Phase 5: Reply-to-Context Feature (NEW)
**User request**: Operators reply to WA messages to add verbs or corrections.
**Files changed:**
- schema.sql: added `quoted_wa_message_id` to raw_messages
- migrate.py: migration for new column
- database.py: insert_raw_message() accepts quoted_wa_message_id
- ingest.py: passes quoted_wa_message_id through pipeline
- pipeline_service.py: accepts quoted_wa_message_id parameter
- level2.py: _get_reply_context() looks up original message events
- level3.py: injects reply context into prompt via l3_context_summary
- committer.py: caps reply confidence at 0.82, _detect_correction_from_reply()
- wa_notifier.py: send_summary_to_group(), _post_send_message()
- wa_listener/index.js: /send-message endpoint
- main.py: auto-end shift posts summary
- shifts.py: manual shift end posts summary
- level3_prompt_template.txt: R15 reply context rule

### Phase 6: Auto-post Shift Summary (NEW)
**User request**: Post summary when asked or when shift ends.
- Level 2 detects SUMMARY_REQUEST (standalone "loading over", count keywords)
- Pipeline service calls wa_notifier.send_summary_to_group()
- Anti-loop: WA listener skips msg.key.fromMe

### Phase 7: Admin Edit/Delete
- DELETE endpoints in registry.py with FK checks
- Edit/delete buttons in frontend tables

### Phase 8: Comprehensive Sequential Test (12 messages, real LLM)
- 44/44 checks passed, 0 failed
- All message types verified: single, multi-truck, multi-verb, tally, noise, unknown, vehicle number, site inference, KV cache
- LLM output archive: 12 records, all raw outputs stored
- Sanitizer: all clean, no injection detected

### Phase 9: Full Code Audit
**Bugs found and fixed:**
1. **CRITICAL**: WA_GROUP_JID not in config.py → ImportError on shift end
2. **CRITICAL**: delete_site() crashes on tallies.site_id (column doesn't exist)
3. **MODERATE**: schema.sql missing 5 migration columns on events table
4. **MODERATE**: schema.sql missing shift_name on shifts table
5. **LOW**: Dead unreachable code in level2.py _get_reply_context()
6. **LOW**: Reprocess endpoints bypass LLM semaphore
7. **LOW**: MODEL_NAME not imported in pipeline_service.py

### Phase 10: LLM Injection Sanitizer
- Created sanitizer.py with injection detection, field validation, confidence clamping
- llm_outputs table for forensic recovery
- Pipeline service archives every LLM call
- docs/flows_and_features.md created with all message types and flows

### Phase 11: Documentation
- QWEN.md created and continuously updated
- docs/brainstorming.md created
- work_diary.md created
- docs/flows_and_features.md created

### Commits Made
1. `16390d9` — fix: docker deployment config, LLM quality fixes
2. `318303d` — feat: reply-to-context, auto-post shift summary, admin edit/delete
3. `3a203ad` — feat: LLM injection sanitizer, audit hardening, flows doc
4. `71cc132` — fix: audit bugs — WA_GROUP_JID, delete_site crash, schema drift, dead code, reprocess semaphore
5. `790b697` — fix: import MODEL_NAME in pipeline_service
6. `7ffd25f` — chore: add test artifacts to gitignore

### Current State
- All code committed and pushed to main
- Server deployment ready (docker compose down -v + git pull + make up)
- .env.copy created for server deployment
- All known bugs fixed
- All tests passing

#### 1. LLM Quality Regression Test
- Created `test_llm_regression.py` to verify all known issues from CLAUDE.md
- Ran 10 test messages through real LLM (Qwen2.5-32B)
- **Result**: All known issues resolved after `--no-cache-prompt` fix
- Average LLM time: 9.5s (much faster than documented 60-150s)

#### 2. Frontend E2E Test
- Created `test_frontend.js` using Playwright
- 40 tests across all dashboard features
- **Result**: 40/40 passed, 0 failed
- Screenshots captured for all tabs

#### 3. Docker Deployment Audit & Fixes
- **docker-compose.yml**: Updated defaults to match Qwen2.5-32B (`FLEET_MODEL`, `FLEET_LLM_MAX_TOKENS=1200`)
- **.env**: Added missing vars (`HOST_PORT`, `FLEET_LLM_API_KEY`, `FLEET_LLM_MOCK`, `FLEET_AUTH_PASSWORD`, `LOG_LEVEL`)
- **.env.example**: Updated model name, max tokens, improved comments
- **Makefile**: Fixed fragile `reset-wa-session` target
- **Committed and pushed** all fixes

#### 4. Registry Population
- Added 17 trucks via API (15 single-letter A-V + UP26 + UP80)
- Added 9 sites via API (KN4, SOC, BG, DAIRY, LG, SKP, PL, TN, BINAI)
- Extracted from real chat data (level2.json)

#### 5. Reply-to-Context Feature (NEW)
**Files changed:**
- `fleet_pipeline/db/schema.sql` — Added `quoted_wa_message_id TEXT` to `raw_messages`
- `fleet_pipeline/db/migrate.py` — Migration for new column
- `fleet_pipeline/db/database.py` — Updated `insert_raw_message()` to accept `quoted_wa_message_id`
- `fleet_pipeline/api/routes/ingest.py` — Pass `quoted_wa_message_id` through pipeline
- `fleet_pipeline/api/pipeline_service.py` — Accept `quoted_wa_message_id` parameter, handle summary requests
- `fleet_pipeline/pipeline/level2.py` — Added `_get_reply_context()`, `SUMMARY_REQUEST_PATTERNS`, `CORRECTION_KEYWORDS`
- `fleet_pipeline/pipeline/level3.py` — Inject reply context into prompt via `l3_context_summary`
- `fleet_pipeline/pipeline/committer.py` — Reply confidence cap (0.82), `_detect_correction_from_reply()`
- `fleet_pipeline/pipeline/wa_notifier.py` — Added `send_summary_to_group()`, `_post_send_message()`
- `fleet_pipeline/wa_listener/index.js` — Added `/send-message` endpoint
- `fleet_pipeline/api/main.py` — Auto-end shift now posts summary
- `fleet_pipeline/api/routes/shifts.py` — Manual shift end now posts summary
- `fleet_pipeline/prompts/level3_prompt_template.txt` — Added R15 (reply context rule)

**How it works:**
1. WA listener already extracts `quoted_wa_message_id` from Baileys (`contextInfo.stanzaId`)
2. Ingest route now stores it in `raw_messages` table
3. Level 2 looks up original message and its resolved events
4. Level 3 injects reply context into LLM prompt
5. Committer caps reply-based confidence at 0.82 (committed but flagged)
6. Correction detection for replies with keywords like "sorry", "not", "actually"

#### 6. Auto-post Shift Summary (NEW)
**How it works:**
1. Level 2 detects `SUMMARY_REQUEST` message type
2. Pipeline service calls `wa_notifier.send_summary_to_group()`
3. Summary generated from DB (same format as frontend)
4. Posted to WA group via `/send-message` endpoint
5. Also posted on shift end (auto and manual)

**Anti-loop protection:**
- WA listener already has `if (msg.key.fromMe) continue;`
- Bot's messages have `fromMe=true`, so never re-ingested

#### 7. Admin Edit/Delete for Trucks/Sites
**Files changed:**
- `fleet_pipeline/api/routes/registry.py` — Added DELETE endpoints with FK checks
- `fleet_pipeline/frontend/index.html` — Added edit/delete buttons to registry tables

#### 8. Documentation
- Created `QWEN.md` — comprehensive project context for Qwen
- Created `docs/brainstorming.md` — detailed design discussions
- Created `work_diary.md` — this file

### Files Created
- `test_llm_regression.py` — LLM quality test script
- `test_frontend.js` — Frontend E2E test (Playwright)
- `frontend_test_report.json` — Test results
- `screenshot_dashboard.png` — Dashboard screenshot
- `screenshot_operator.png` — Operator tab screenshot
- `screenshot_commits.png` — Commits tab screenshot
- `screenshot_admin.png` — Admin tab screenshot
- `.env.copy` — Server deployment env template
- `QWEN.md` — Project context for Qwen
- `docs/brainstorming.md` — Design discussions
- `work_diary.md` — This file

### Files Modified
- `fleet_pipeline/db/schema.sql` — Added `quoted_wa_message_id` column
- `fleet_pipeline/db/migrate.py` — Migration for new column
- `fleet_pipeline/db/database.py` — Updated `insert_raw_message()`
- `fleet_pipeline/api/routes/ingest.py` — Pass `quoted_wa_message_id` through
- `fleet_pipeline/api/pipeline_service.py` — Summary handling, reply context
- `fleet_pipeline/api/routes/registry.py` — DELETE endpoints
- `fleet_pipeline/api/routes/shifts.py` — Summary on shift end
- `fleet_pipeline/api/main.py` — Summary on auto-end shift
- `fleet_pipeline/pipeline/level2.py` — Reply context, summary detection
- `fleet_pipeline/pipeline/level3.py` — Reply context in prompt
- `fleet_pipeline/pipeline/committer.py` — Reply confidence cap, correction detection
- `fleet_pipeline/pipeline/wa_notifier.py` — Summary posting
- `fleet_pipeline/wa_listener/index.js` — `/send-message` endpoint
- `fleet_pipeline/prompts/level3_prompt_template.txt` — R15 reply context rule
- `fleet_pipeline/frontend/index.html` — Edit/delete buttons in admin
- `docker-compose.yml` — Updated defaults
- `.env.example` — Updated for Qwen2.5-32B
- `Makefile` — Fixed `reset-wa-session`
- `.gitignore` — Added `.env.copy` exclusion
- `CLAUDE.md` — (unchanged, copied to QWEN.md)

### Next Steps
- Deploy to server (push, pull, rebuild)
- Test with real WhatsApp messages
- Monitor reply context accuracy
- Monitor summary posting
