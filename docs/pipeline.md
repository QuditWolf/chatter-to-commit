# Pipeline — Parsing Logic

## Overview

Every message goes through four stages before reaching the database.

```
Raw text  →  Level 1  →  Level 2  →  Level 3 (LLM)  →  Committer  →  DB
```

---

## Level 1 — Message Extraction (`pipeline/level1.py`)

Parses a WhatsApp chat export or real-time Baileys message into a structured dict:

```python
{
  "msg_id":       "uuid",
  "timestamp_iso": "2026-03-24T17:00:00+05:30",
  "sender_id":    "+919876543210",
  "sender_name":  "Dinesh",
  "raw_text":     "D LS SOC",
  "is_edited":    False,
  "is_deleted":   False,
}
```

For real-time WA messages the `wa_message_id` (Baileys key ID) is used as `msg_id` so that HITL questions can reference the original WA message for reply routing.

---

## Level 2 — Rule-Based Enrichment (`pipeline/level2.py`)

Performs fuzzy vocabulary matching to produce candidate truck and site aliases. This stage does **not** make final decisions — it builds a candidate set that is injected into the Level 3 prompt context.

- Strips common prefixes/noise words
- Normalises case
- Computes edit-distance matches against all known truck and site aliases from the registry
- Attaches `truck_candidates` and `site_candidates` to the message dict

---

## Level 3 — LLM Inference (`pipeline/level3.py`)

Constructs a structured prompt from:
1. The LLM system prompt (`prompts/level3_prompt_template.txt`) containing all parsing rules
2. Current `TRUCK_REGISTRY` and `SITE_REGISTRY` (loaded fresh each call)
3. `L3_CONTEXT` — last 12 committed events (rolling window), used for cycle-aware inference
4. `operator_clarification` — if this is a HITL re-process, the human's answer is injected here

The LLM must output **only** valid JSON matching the schema. Any non-JSON output is rejected and the message is held.

### Output schema

```json
{
  "msg_type": "STATUS_UPDATE",
  "events": [
    {
      "truck_alias": "D",
      "truck_id": "TD",
      "status": "LS",
      "site_alias": "SOC",
      "site_id": "SOC",
      "confidence": 0.95,
      "reasoning": "explicit D LS SOC",
      "inferred": false,
      "timestamp_effective": "2026-03-24T17:00:00+05:30"
    }
  ],
  "overall_confidence": 0.95,
  "commit_recommendation": "COMMIT"
}
```

`msg_type` values: `STATUS_UPDATE` · `CORRECTION` · `TALLY_UPDATE` · `NOISE` · `QUERY` · `OPS_NOTE` · `SHIFT_SIGNAL`

---

## LLM Parsing Rules

All rules live in `prompts/level3_prompt_template.txt`. Key rules:

### RULE 1 — ENTER is always ENTER
`"enter"` always means `status=ENTER` regardless of site type. Do not convert to LS or US based on the site registry.

### RULE 2 — Cycle-aware site inference

The most important rule. The LLM must NOT assume a truck returns to the same site between cycles.

| Situation | Action |
|-----------|--------|
| Status is LS/LO/LEFT/US/UO **and** truck's last status is ENTER/LS/US (open cycle) at site X | Infer site = X, confidence ≤ 0.90, COMMIT |
| Status is ENTER (new cycle starting) | Do NOT inherit previous cycle's site. site_id = null, confidence ≤ 0.55, HOLD |
| Status is LS/US **and** last cycle is closed (last = LEFT/LO/UO) | Cannot infer. site_id = null, confidence ≤ 0.55, HOLD |
| Same sender sent explicit site within last 2 hours (fallback) | Low-confidence hint only, confidence ≤ 0.65, COMMIT_FLAG |

**Why:** After completing a loading cycle (LEFT), a truck may go to a completely different loading site for the next cycle. Inheriting the old site would commit wrong data silently.

### RULE 3 — Unknown site
If a site string appears but is not in `SITE_REGISTRY` → `site_id=null`, `site_alias=<unknown string>`, confidence ≤ 0.55, HOLD. Never guess.

### RULE 4 — Unknown truck
If a truck name does not match any alias → `truck_id=null`, confidence ≤ 0.45, HOLD. Never guess.

### RULE 5 — Corrections
`"sorry D not B"` → `msg_type=CORRECTION`. If the target event is identifiable, include it; otherwise `events=[]`, HOLD → HITL.

### RULE 7b — LS/US implies ENTER
If a truck reports LS or US but has no open ENTER at that site in L3_CONTEXT → infer ENTER happened first. Inferred ENTER gets the same confidence as the LS/US event (not upgraded).

If `site_id=null` (unknown site), do NOT emit inferred ENTER — site is unknown.

### RULE 8 — Inferred intermediate steps
If messages skip steps (e.g. goes from LS to LEFT with no LO), emit the missing steps as inferred events with appropriate confidence:

| Pattern | Action |
|---------|--------|
| Last=LS at X, message=LEFT X | Emit inferred LO at X (0.88) |
| Last=LS at X, message=LEFT (no site) | Emit inferred LO at X, LEFT at X (0.85) |
| Last=ENTER(loading), message=LEFT loading | Emit LS + LO (0.45, LOW — truck may have left empty) |
| Last=US at X, message=LEFT X | Emit inferred UO at X (0.88) |
| Last=LO(loading), message=ENTER(unloading) | Emit inferred LEFT(loading) (0.88) |

### RULE 8b — Typo correction
Common misspellings auto-corrected before parsing:
- `lefy / lef / leftt` → LEFT
- `entter / entr / entre` → ENTER
- `loadind / loadin` → LS or LO by context
- `unlaod / unlod` → US or UO by context

### RULE 8c — Operator clarification override
When `L3_CONTEXT.operator_clarification` is set (HITL re-process), it is authoritative and overrides the raw message interpretation. Confidence = 0.95.

---

## Committer (`pipeline/committer.py`)

The committer applies deterministic rules on top of the LLM output.

### Inferred ENTER injection

Before committing each event, the committer checks whether the truck needs an ENTER injected:

```python
def _needs_inferred_enter(conn, truck_id, site_id):
    # Query the most recent committed event for this truck at this site
    # Returns True if last status was NOT ENTER, LS, or US
    # (i.e. the cycle is not yet open at this site)
```

If True and the LS/US event has a known `site_id`, the committer prepends a synthetic ENTER event:
- `inferred = True`
- `confidence = min(ls_event_confidence, 0.88)` — inherits the LS confidence, not hardcoded

This handles cases where operators skip the ENTER message but RULE 7b in the LLM also missed it.

### Commit status rules

Applied in priority order:

1. `truck_id = null` → **HELD** + UNKNOWN_TRUCK question (return immediately)
2. `site_id = null` AND status requires site → **HELD** + UNKNOWN_SITE question (return immediately, no LOW_CONFIDENCE stacking)
3. `overall_confidence < 0.60` → **HELD** + LOW_CONFIDENCE question
4. `overall_confidence < 0.85` → **FLAGGED**
5. Otherwise → **COMMITTED**

The key design decision: UNKNOWN_SITE/UNKNOWN_TRUCK suppress LOW_CONFIDENCE. The site/truck questions are more actionable and the operator only needs one bot message.

### shift_id assignment
The `ShiftDetector` determines which shift a message belongs to based on time-of-day rules and WA keyword signals (e.g. "s1", "shift start"). If no shift is detected, the event is **FLAGGED** (not COMMITTED) until a shift is assigned.

---

## HITL Types

### UNKNOWN_TRUCK
**Trigger:** `truck_id = null` after Level 3
**Bot message:** Quotes original message, lists recognised trucks, offers `new:TX:Name:alias` format
**Answer format:**
- Existing code: `TA` → adds alias, updates held event's `truck_id`, commits
- New truck: `new:TX:Display Name:alias` → creates truck, commits
- Free text: re-processes with `operator_clarification`

### UNKNOWN_SITE
**Trigger:** `site_id = null` AND status ∈ {ENTER, LS, LO, LEFT, US, UO}
**Bot message:** Quotes original, explains what was unrecognised
**Answer format:**
- Site code: `SOC` → updates event's `site_id`, commits
- Full corrected message: `D LS SOC` → re-processes with clarification
- New site: `new:SNAME:Display Name:loading:alias` → creates site, commits
- Free text: re-processes with clarification

### LOW_CONFIDENCE
**Trigger:** `overall_confidence < 0.60` (when site and truck are known)
**Bot message:** Shows parsed interpretation, asks CONFIRM or correction
**Answer format:**
- `CONFIRM` → force-commits the held event
- Anything else → re-processes with clarification

### CORRECTION_AMBIGUOUS
**Trigger:** Message classified as CORRECTION but target is unclear
**Bot message:** Asks operator to clarify what changed
**Answer:** Always re-processes with clarification

---

## HITL Re-process Flow

When an answer triggers re-processing:

1. Original held event is marked `commit_status = DELETED`
2. Original `raw_text` is re-sent through the full pipeline
3. `operator_clarification` is injected into `L3_CONTEXT` (RULE 8c)
4. LLM produces a corrected interpretation at high confidence (0.95)
5. New event is committed
6. Bot sends ack: `✅ Clarification received — re-processing message…`

This means the HITL loop is transparent — every commit has a `msg_id` linking back to the original raw message, and the reasoning field shows `operator_clarification: "..."`.

---

## Truck Cycle

The full expected cycle for a loading truck:

```
Depot
  │
  ▼ ENTER (loading site)    e.g. "D enter SOC"
  │
  ▼ LS (loading started)    e.g. "D LS"
  │
  ▼ LO (loading over)       e.g. "D LO"
  │
  ▼ LEFT (loading site)     e.g. "D left SOC"
  │
  ▼ ENTER (unloading site)  e.g. "D enter BG"
  │
  ▼ US (unloading started)  e.g. "D US"
  │
  ▼ UO (unloading over)     e.g. "D UO"
  │
  ▼ LEFT (unloading site)   e.g. "D left BG"
  │
  └─ back to Depot → repeat
```

Any step may be omitted from messages. The pipeline infers missing steps where confident enough.

---

## Shift Detection (`pipeline/shift_detector.py`)

Shifts are detected two ways:

1. **Time-based:** Compares message `timestamp_iso` against configured shift start times in `shift_config` table (default: Shift 1 = 06:00, Shift 2 = 13:00, Shift 3 = 17:00)

2. **WA keyword:** If the raw text matches a configured `wa_keyword` (e.g. `"s1"`, `"shift start"`), a new shift is created immediately regardless of time

Shift boundaries are stored in the `shifts` table. Events without a detected shift are **FLAGGED** for human review.
