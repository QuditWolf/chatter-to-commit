# RESUME PROMPT — Fleet Tracker Project

Paste this into a new session to resume work:

---

I'm working on the Fleet Tracker project at `/workspace/02_truck_fleet`. Here's the current state:

## What's Done
- All features implemented and pushed to main branch:
  - Reply-to-context for WhatsApp messages (confidence cap 0.82, correction detection)
  - Auto-post shift summary to WA group (on "loading over", "total count?", shift end)
  - Admin edit/delete for trucks and sites
  - LLM injection sanitizer (sanitizer.py)
  - LLM output archive (llm_outputs table)
  - Full code audit with 7 bugs fixed
- All tests passing:
  - LLM regression: 10/10 messages, 44/44 checks
  - Frontend E2E: 40/40 browser tests
  - Comprehensive sequential: 12/12 messages through real LLM
- Registry populated: 17 trucks, 9 sites
- Docker deployment config fixed and ready

## Current Issue
The E2E sequential test (`test_e2e.py`) timed out when running against the deployed server at `http://10.0.0.4:8081`. Need to:
1. Verify the server is actually running the latest code (check if docker was rebuilt)
2. Run the E2E test with proper timeout handling
3. Verify dashboard statistics match expected values
4. Test HITL flow, reply context, and summary posting end-to-end

## Key Files
- `fleet_pipeline/api/pipeline_service.py` — Pipeline orchestrator
- `fleet_pipeline/pipeline/level2.py` — Message classification + reply context
- `fleet_pipeline/pipeline/level3.py` — LLM prompt + sanitizer integration
- `fleet_pipeline/pipeline/committer.py` — Commit rules + reply confidence cap
- `fleet_pipeline/pipeline/sanitizer.py` — LLM output sanitization
- `fleet_pipeline/pipeline/wa_notifier.py` — WA notifications + summary posting
- `fleet_pipeline/wa_listener/index.js` — WA listener + /send-message endpoint
- `test_e2e.py` — Sequential E2E test (needs to be run)

## Server Info
- API: `http://10.0.0.4:8081` (nginx proxy to api:8000)
- LLM: `http://10.0.0.4:8001/v1` (llama.cpp, Qwen2.5-32B)
- DB: `/workspace/02_truck_fleet/fleet_pipeline/data/fleet.db`
- Docker: `make up`, `make qr`, `make down`

## Next Steps
1. Run `test_e2e.py` against the deployed server (sequential, 33 messages)
2. Verify all dashboard stats match
3. Test browser automation for all tabs
4. Report latency per message and total wall time
5. Identify any remaining issues

## Context Files
- `QWEN.md` — Full project context with all decisions
- `docs/brainstorming.md` — Design discussions
- `docs/flows_and_features.md` — All message types and flows
- `work_diary.md` — Complete work log

---
