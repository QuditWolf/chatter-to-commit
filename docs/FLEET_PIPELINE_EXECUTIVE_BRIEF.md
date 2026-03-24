# Fleet Tracker — Pipeline & Parsing Overview
**For: Stakeholders / Executive Review**

---

## What the System Does

The Fleet Tracker converts informal WhatsApp messages from field drivers into structured, queryable fleet events. A single message like "B LS dairy" is automatically parsed, validated, stored, and reflected on the live dashboard — with no manual data entry.

---

## The Standard Truck Cycle

Each truck/trolley follows this operational cycle. Messages may arrive for any step, and steps may be skipped:

```
Depot
  ↓  ENTER — arrives at loading site
  ↓  LS    — loading starts
  ↓  LO    — loading complete
  ↓  LEFT  — departs loading site
  ↓  ENTER — arrives at unloading site (Dairy / BG)
  ↓  US    — unloading starts         [sometimes omitted]
  ↓  UO    — unloading complete       [sometimes omitted]
  ↓  LEFT  — departs unloading site
  ↓  → next cycle (loading) or Depot
```

**Site types in use:**
| Site | Type | Meaning |
|------|------|---------|
| KN4, SOC, TN, PL, KHET | Loading | Quarry / pit / mine |
| DAIRY, BG | Unloading / Depot | Delivery / parking |

---

## Message Parsing — Three Stages

### Stage 1 — Level 1: Raw Ingest
Every WhatsApp message is timestamped and stored exactly as received. Nothing is discarded.

### Stage 2 — Level 2: Enrichment (deterministic, <1ms)
The system scans the raw text for:
- **Truck aliases**: "B" → Truck B (TB), "Arjun Novo 4841" → T_ARJ
- **Status keywords**: "LS", "loading started", "loading over", "entered", "left"…
- **Site references**: "dairy", "KN4", "Bhandara Ground" (BG)
- **Candidate message type**: STATUS_LIKE, TALLY_LIKE, NOISE_LIKE

This is pure pattern matching — instant, no AI needed.

### Stage 3 — Level 3: LLM Inference (AI, 15–60 seconds)
The enriched message is sent to a large language model with:
- The full truck registry (all known trucks and their aliases)
- The full site registry (all sites and their types)
- The last 20 committed fleet events (context window)
- A structured prompt with parsing rules and examples

The model returns a JSON object with one or more events, confidence scores, and a commit recommendation.

---

## Deterministic vs. Non-Deterministic Processing

| Aspect | Deterministic (L1+L2) | AI-Based (L3) |
|--------|----------------------|---------------|
| Speed | <1ms | 15–60 seconds |
| Truck lookup | Exact alias matching | Fuzzy + contextual |
| Status extraction | Keyword list | NLU + context |
| Site inference | Exact alias | Contextual + history |
| Multi-step inference | Not applicable | Yes (see below) |
| Reliability | 100% predictable | Probabilistic |
| LLM offline? | Stage runs fine | Falls back to HELD |

---

## Confidence & Commit Tiers

Every extracted event receives a confidence score (0.0–1.0):

| Score | Status | What happens |
|-------|--------|-------------|
| ≥ 0.85 | **COMMITTED** ✅ | Auto-saved. Appears on dashboard immediately. |
| 0.60–0.84 | **FLAGGED** ⚠️ | Saved but marked for review. HITL question created. |
| < 0.60 | **HELD** 🔴 | Requires manual classification before committing. |
| Unknown truck/site | **HELD** 🔴 | HITL question to identify the entity. |

---

## Inference Engine — Filling in the Gaps

A key feature is **state inference**: when a message implies something happened that wasn't explicitly stated, the system generates inferred events alongside the explicit one.

### High-Confidence Inference (auto-committed)
| Context | New message | Inference | Confidence |
|---------|------------|-----------|-----------|
| B was at LS (SOC) | "B LO" | Site = SOC | 0.88 |
| B was at LS (dairy) | "B left dairy" | LO happened before LEFT | 0.88 |
| B was at LO (KN4) | "B enter dairy" | LEFT(KN4) happened | 0.88 |

**Example:** Driver texts "B LO" without mentioning the site. The system sees B was last at LS at SOC → infers site=SOC with confidence 0.88 → auto-commits.

### Low-Confidence Inference (flagged for review)
| Context | New message | Inference | Confidence | Why low? |
|---------|------------|-----------|-----------|----------|
| D entered KN4 | "D left KN4" | LS + LO happened | 0.45 | Truck may have left empty |
| D entered dairy | "D left dairy" | US + UO happened | 0.45 | Unloading not confirmed |

These are **always flagged** for human review — the system notes that loading or unloading may not have occurred.

### Rule: Same sender, same session = same site
If the same driver sends multiple status messages within 2 hours and stops mentioning the site, the system infers the site from their last known location. Confidence: 0.85.

---

## HITL (Human-in-the-Loop) Queue

When the system is uncertain, it creates a question in the operator panel:

| Question type | Example | Operator action |
|---------------|---------|----------------|
| UNKNOWN_TRUCK | "Who is 'Raju 4841'?" | Map to existing truck or create new |
| UNKNOWN_SITE | "What is 'chowk'?" | Map to existing site or create new |
| LOW_CONFIDENCE | "B LO — is this correct?" | Confirm or correct |
| AMBIGUOUS_CORRECTION | "sorry not B" — but B what? | Manual clarification |

Answers to HITL questions enrich the registry permanently — the same alias is recognised next time.

---

## LLM Offline Resilience

If the language model is offline or returns an error:
- The message is **not lost** — it is saved as **HELD** with the raw text intact
- It appears in the Commit Log with a "not mapped" warning
- The operator can manually classify it (truck, status, site) via the UI
- When the LLM comes back online, previously unprocessed messages can be re-queued

---

## Shift Management

The system automatically tracks work shifts:

| Method | Description |
|--------|-------------|
| **Auto-detect** | 1-hour gap in messages → new shift starts |
| **WA signal** | "s1", "shift start", "shift end" in messages → immediate shift change |
| **Operator** | Start / End / Resume buttons in the operator dashboard |

Shifts are named `YYYY-MM-DD_NN` (e.g., `2026-03-23_02` = second shift of March 23).

---

## Data Flow Summary

```
WhatsApp message
      ↓
Node.js Listener (instant)
      ↓  HTTP POST
Python API (FastAPI)
      ↓  L1: store raw
      ↓  L2: enrich (deterministic, <1ms)
      ↓  L3: LLM inference (15–60s)
      ↓  Committer: save events + HITL
      ↓  WebSocket broadcast
Live Dashboard (instant update)
```

---

## Frontend Panels

| Panel | Purpose |
|-------|---------|
| **Dashboard** | Live fleet map, KPIs, active shift, service status |
| **Commit Log** | All messages with their extracted events; manual map for HELD items |
| **HITL Queue** | Pending questions for operator resolution |
| **Operator** | Shift controls, manual message injection |
| **Admin** | Truck/site registry management |

---

## System Status Monitoring

The dashboard shows live status of:
- **LLM** — AI backend (green/red dot)
- **WA** — WhatsApp connection (green/red dot)
- **DB** — SQLite database (green/red dot)

Incidents (downtime, reconnects) are logged in the System Health panel.
