# Fleet Operations Spec

## Truck/Trolley Cycle

Each truck follows this standard cycle. **Any step may be skipped** — the pipeline must infer what happened:

```
Depot
  ↓
ENTER (loading site)      — truck arrives at loading site
  ↓
LS   (loading started)    — loading begins
  ↓
LO   (loading over)       — loading complete
  ↓
LEFT (loading site)       — truck departs loading site
  ↓
ENTER (unloading site)    — truck arrives at unloading site
  ↓
US   (unloading started)  — unloading begins   [often omitted]
  ↓
UO   (unloading over)     — unloading complete [often omitted]
  ↓
LEFT (unloading site)     — truck departs unloading site
  ↓
→ back to loading site (ENTER loading) or back to Depot
```

**Site types:**
- `loading` — quarry / mine / pit (KN4, SOC, TN, PL, KHET)
- `unloading` — delivery point (DAIRY, BG)
- `depot` — parking (BG also serves as depot)

## Standard Message Format

```
<truck> <status> <site>
```
Examples:
- `P enter dairy`
- `R UO bg`
- `D LS Kn4`
- `A, B enter KN4`   (multi-truck)
- `B LO`             (site omitted — infer from context)

## State Inference Rules

When a message arrives, the LLM must:

1. **Check L3_CONTEXT** for each truck's last known status + site.

2. **Infer skipped steps** if the new status is not the natural next step:
   - Last=LS, New=LEFT → infer LO happened (generate both LO and LEFT as separate events; LO inferred, LEFT explicit)
   - Last=ENTER(loading), New=LO → infer LS happened
   - Last=LO, New=ENTER(unloading) → infer LEFT(loading) happened
   - Last=US, New=LEFT → infer UO happened
   - etc.

3. **One message → potentially multiple commits**:
   - Always emit an event for what the message explicitly says
   - Emit additional inferred events for skipped steps
   - All inferred events: `inferred=true`, `confidence ≤ 0.75`, `commit_recommendation="COMMIT_FLAG"`

4. **Manual approval queue** (HITL):
   - Inferred intermediate events should trigger HITL questions for operator to approve
   - The operator can COMMIT or DISMISS each inferred event

## Commit Statuses

| commit_recommendation | commit_status | Meaning |
|---|---|---|
| COMMIT | COMMITTED | High confidence, auto-committed |
| COMMIT_FLAG | FLAGGED | Inferred/uncertain — needs review |
| HOLD | HELD | Unknown truck/site or LLM offline — needs manual map |

## Example: Inferred Multi-Commit

Message: `B left dairy`
L3_CONTEXT: Last event for B = `LS` at `DAIRY`

Expected output (2 events):
1. `{truck_id: TB, status: LO, site_id: DAIRY, inferred: true, confidence: 0.72, commit_rec: COMMIT_FLAG}`
2. `{truck_id: TB, status: LEFT, site_id: DAIRY, inferred: false, confidence: 0.95, commit_rec: COMMIT}`

## Notes on "left" vs "LEFT"

- `left` in a message = LEFT status (truck departed the site)
- Context: if truck last entered a loading site and now says LEFT → it also implies LO happened
- Always use site_type to determine if the LEFT is from a loading or unloading site
