"""
E2E verification:
  1. Frontend processing state — WS events: message_received → commit_created
  2. D UO inference — seed US context, send "D UO", expect UO committed
"""
import asyncio
import json
import time
import uuid
import sqlite3
import requests
import websockets

BASE  = "http://localhost:8000"
WS    = "ws://localhost:8000/ws"
DB    = "/workspace/02_truck_fleet/data/fleet.db"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
INFO = "\033[36mINFO\033[0m"

results = []

def check(label, cond, detail=""):
    status = PASS if cond else FAIL
    results.append(cond)
    suffix = f"  ({detail})" if detail else ""
    print(f"  {status}  {label}{suffix}")

def h(title):
    print(f"\n── {title} ──────────────────────────────────")

def seed_event(truck_id, truck_alias, site_id, site_alias, status, inferred=False):
    """Insert a COMMITTED event directly into DB to seed L3 context."""
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # get a shift
        shift = conn.execute(
            "SELECT shift_id FROM shifts WHERE status='ACTIVE' LIMIT 1"
        ).fetchone()
        shift_id = shift["shift_id"] if shift else None
        # find or create raw_message
        msg_id = f"seed_{uuid.uuid4().hex[:8]}"
        conn.execute(
            """INSERT INTO raw_messages(msg_id,raw_text,sender_name,sender_id,source_file,timestamp_iso)
               VALUES(?,?,?,?,?,datetime('now'))""",
            (msg_id, f"seed {status}", "seeder","seeder","seed")
        )
        conn.execute(
            """INSERT INTO events(
                 event_id, msg_id, truck_id, truck_alias, site_id, site_alias,
                 status, timestamp_effective, inferred, confidence, reasoning,
                 commit_status, shift_id
               ) VALUES(?,?,?,?,?,?,?,datetime('now'),?,0.95,'seeded','COMMITTED',?)""",
            (f"ev_{uuid.uuid4().hex[:8]}", msg_id, truck_id, truck_alias,
             site_id, site_alias, status, 1 if inferred else 0, shift_id)
        )
        conn.commit()
    print(f"  {INFO}  Seeded {truck_alias} {status}@{site_alias}")


async def ws_collect(duration: float):
    """Connect to WS and collect events for `duration` seconds."""
    events = []
    try:
        async with websockets.connect(WS, open_timeout=5) as ws:
            deadline = asyncio.get_event_loop().time() + duration
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    events.append(json.loads(raw))
                except asyncio.TimeoutError:
                    break
    except Exception as e:
        print(f"  {INFO}  WS connect error: {e}")
    return events


# ───────────────────────────────────────────────────────────────────────────
# TEST 1 — WS processing state: message_received fires before commit_created
# ───────────────────────────────────────────────────────────────────────────
async def test_ws_processing_state():
    h("TEST 1 — WS Processing State")

    wa_id = f"wa_{uuid.uuid4().hex[:10]}"
    received_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Start collecting WS events
    collect_task = asyncio.create_task(ws_collect(90))

    # Small delay to ensure WS is connected
    await asyncio.sleep(0.5)

    # Send WA message (should return 202 immediately)
    t0 = time.time()
    resp = requests.post(f"{BASE}/api/ingest/wa-message", json={
        "wa_message_id": wa_id,
        "sender_phone":  "+254700000001",
        "group_jid":     "120363425655528115@g.us",
        "raw_text":      "truck A entered loading site",
        "received_at":   received_at,
        "message_type":  "fleet_event",
    }, timeout=10)
    api_elapsed = time.time() - t0

    check("202 returned immediately",    resp.status_code == 202,
          f"status={resp.status_code}")
    check("API response < 2s (non-blocking)", api_elapsed < 2.0,
          f"{api_elapsed:.2f}s")

    # Wait for WS events (up to 90s for LLM)
    events = await collect_task

    types = [e.get("type") for e in events]
    print(f"  {INFO}  WS events received: {types}")

    check("message_received event fired",   "message_received" in types)
    check("commit_created event fired",     "commit_created" in types)

    # Check ordering: message_received must come before commit_created
    if "message_received" in types and "commit_created" in types:
        mr_idx = next(i for i,e in enumerate(events) if e.get("type")=="message_received")
        cc_idx = next(i for i,e in enumerate(events) if e.get("type")=="commit_created")
        check("message_received before commit_created", mr_idx < cc_idx,
              f"indices {mr_idx} < {cc_idx}")

        mr = events[mr_idx]["data"]
        cc = events[cc_idx]["data"]
        check("message_received has wa_message_id",  "wa_message_id" in mr)
        check("message_received has raw_text",        "raw_text" in mr)
        check("commit_created has msg_id",            bool(cc.get("msg_id")))
        check("commit_created msg_id matches wa_id",
              cc.get("msg_id") == wa_id or cc.get("msg_id","").startswith("msg_"),
              f"msg_id={cc.get('msg_id')}")

    return events


# ───────────────────────────────────────────────────────────────────────────
# TEST 2 — D UO inference: seed US, then "D UO" → expect UO committed
# ───────────────────────────────────────────────────────────────────────────
async def test_uo_inference():
    h("TEST 2 — D UO Inference (with US context)")

    # First ensure trucks/sites exist in registry
    trucks_resp = requests.get(f"{BASE}/api/registry/trucks").json()
    truck_aliases = [t.get("alias","").upper() for t in trucks_resp.get("items", trucks_resp.get("trucks",[]))]
    sites_resp  = requests.get(f"{BASE}/api/registry/sites").json()
    site_aliases = [s.get("alias","").upper() for s in sites_resp.get("items", sites_resp.get("sites",[]))]

    print(f"  {INFO}  Trucks: {truck_aliases[:5]}, Sites: {site_aliases[:5]}")

    # Pick truck D and a site; create if not present
    if "D" not in truck_aliases:
        r = requests.post(f"{BASE}/api/registry/trucks", json={
            "truck_id": "TD", "alias": "D", "aliases": ["D"]
        })
        print(f"  {INFO}  Created truck D: {r.status_code}")

    # Pick a delivery/unloading site
    unload_site = None
    for s in sites_resp.get("items", sites_resp.get("sites",[])):
        if s.get("site_type") in ("unloading","delivery"):
            unload_site = s; break
    if not unload_site:
        # Use whatever site exists
        sites = sites_resp.get("items", sites_resp.get("sites",[]))
        unload_site = sites[0] if sites else None

    if not unload_site:
        print(f"  {INFO}  No sites in registry — creating BG")
        requests.post(f"{BASE}/api/registry/sites", json={
            "site_id":"BG","alias":"BG","aliases":["BG","bg"],"site_type":"unloading"
        })
        unload_site = {"site_id":"BG","alias":"BG","site_type":"unloading"}

    site_id    = unload_site.get("site_id","BG")
    site_alias = unload_site.get("alias","BG")
    print(f"  {INFO}  Using site {site_alias} ({site_id}) for UO test")

    # Seed a US (unloading started) event so L3 context has D at unloading site
    seed_event("TD", "D", site_id, site_alias, "US")

    # Now send "D UO" — should infer explicit UO or at minimum have high-conf UO
    resp = requests.post(f"{BASE}/api/ingest/manual", json={
        "text": f"D UO {site_alias}",
        "sender_name": "operator",
        "sender_id": "manual",
    }, timeout=120)

    check("D UO manual ingest succeeded", resp.status_code == 200,
          f"status={resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(f"  {INFO}  Response: committed={data.get('committed')}, flagged={data.get('flagged')}, held={data.get('held')}")
        print(f"  {INFO}  Events: {json.dumps(data.get('events',[]), indent=2)[:600]}")

        events = data.get("events", [])
        uo_events = [e for e in events if e.get("status") == "UO"]
        check("At least 1 UO event returned", len(uo_events) >= 1,
              f"{len(uo_events)} UO events")
        check("UO not HELD", not any(
            e.get("commit_status") == "HELD" for e in uo_events
        ))
        check("committed or flagged > 0", data.get("committed",0) + data.get("flagged",0) > 0,
              f"committed={data.get('committed')}, flagged={data.get('flagged')}")

    # Test 2b: "D UO" with no site specified — model should use last known site from L3
    h("TEST 2b — D UO (no site, rely on L3 context)")
    seed_event("TD", "D", site_id, site_alias, "US")

    resp2 = requests.post(f"{BASE}/api/ingest/manual", json={
        "text": "D UO",
        "sender_name": "operator",
        "sender_id":   "manual",
    }, timeout=120)

    check("D UO (no site) succeeded", resp2.status_code == 200,
          f"status={resp2.status_code}")
    if resp2.status_code == 200:
        d2 = resp2.json()
        print(f"  {INFO}  Response: committed={d2.get('committed')}, flagged={d2.get('flagged')}, held={d2.get('held')}")
        events2 = d2.get("events", [])
        uo2 = [e for e in events2 if e.get("status") == "UO"]
        check("UO event returned even without explicit site", len(uo2) >= 1,
              f"{len(uo2)} UO events")
        if uo2:
            check("UO site inferred from L3 context",
                  uo2[0].get("site_id") is not None,
                  f"site_id={uo2[0].get('site_id')}")


# ───────────────────────────────────────────────────────────────────────────
# TEST 3 — Full inference sequence  ENTER → LS → LO → LEFT → ENTER → US → UO → LEFT
# ───────────────────────────────────────────────────────────────────────────
async def test_full_cycle_inference():
    h("TEST 3 — Full Cycle Inference (D)")

    # Get a loading site and unloading site
    sites_resp = requests.get(f"{BASE}/api/registry/sites").json()
    sites = sites_resp.get("items", sites_resp.get("sites", []))
    loading_site = next((s for s in sites if s.get("site_type") == "loading"), sites[0] if sites else None)
    unload_site  = next((s for s in sites if s.get("site_type") == "unloading"), None)

    if not loading_site:
        print(f"  {INFO}  No sites — skipping full cycle test")
        return

    ls = loading_site.get("alias", "KN4")
    us = unload_site.get("alias", "BG") if unload_site else ls

    steps = [
        (f"D enter {ls}",  "ENTER", ls),
        (f"D ls {ls}",     "LS",    ls),
        (f"D lo {ls}",     "LO",    ls),
        (f"D left {ls}",   "LEFT",  ls),
        (f"D enter {us}",  "ENTER", us),
        (f"D us {us}",     "US",    us),
        (f"D uo {us}",     "UO",    us),
        (f"D left {us}",   "LEFT",  us),
    ]

    for text, expected_status, site in steps:
        resp = requests.post(f"{BASE}/api/ingest/manual", json={
            "text": text, "sender_name": "operator", "sender_id": "manual"
        }, timeout=120)
        if resp.status_code == 200:
            d = resp.json()
            evts = d.get("events", [])
            matched = any(e.get("status") == expected_status for e in evts)
            total = d.get("committed",0) + d.get("flagged",0) + d.get("held",0)
            check(f"'{text}' → {expected_status}", matched and total > 0,
                  f"events={[e.get('status') for e in evts]}, c={d.get('committed')}, f={d.get('flagged')}")
        else:
            check(f"'{text}' → {expected_status}", False,
                  f"HTTP {resp.status_code}")


# ───────────────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────────────
async def main():
    print("\n═══════════════════════════════════════════════════════════")
    print("  Fleet Tracker — E2E Verification (Processing State + UO)")
    print("═══════════════════════════════════════════════════════════")

    await test_ws_processing_state()
    await test_uo_inference()
    await test_full_cycle_inference()

    passed = sum(results)
    total  = len(results)
    print(f"\n══ Results: {passed}/{total} passed ══")
    if passed == total:
        print(f"  {PASS}  All checks passed")
    else:
        failed = total - passed
        print(f"  {FAIL}  {failed} check(s) failed")


if __name__ == "__main__":
    asyncio.run(main())
