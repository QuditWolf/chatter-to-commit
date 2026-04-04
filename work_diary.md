# Work Diary — Fleet Tracker

## 2026-04-05 Session

### Tasks Completed

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
