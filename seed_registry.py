"""Seed trucks and sites into the remote server via API."""

import json
import urllib.request

BASE = "http://10.0.0.4:8081"

TRUCKS = [
    {"truck_id": "TA", "display_name": "Truck A", "aliases": ["A", "a"]},
    {"truck_id": "TB", "display_name": "Truck B", "aliases": ["B", "b"]},
    {"truck_id": "TC", "display_name": "Truck C", "aliases": ["C", "c"]},
    {"truck_id": "TD", "display_name": "Truck D", "aliases": ["D", "d"]},
    {"truck_id": "TE", "display_name": "Truck E", "aliases": ["E", "e"]},
    {"truck_id": "TF", "display_name": "Truck F", "aliases": ["F", "f"]},
    {"truck_id": "TG", "display_name": "Truck G", "aliases": ["G", "g"]},
    {"truck_id": "TH", "display_name": "Truck H", "aliases": ["H", "h"]},
    {"truck_id": "TI", "display_name": "Truck I", "aliases": ["I", "i"]},
    {"truck_id": "TJ", "display_name": "Truck J", "aliases": ["J", "j"]},
    {"truck_id": "TK", "display_name": "Truck K", "aliases": ["K", "k"]},
    {"truck_id": "TL", "display_name": "Truck L", "aliases": ["L", "l"]},
    {"truck_id": "TM", "display_name": "Truck M", "aliases": ["M", "m"]},
    {"truck_id": "TN", "display_name": "Truck N", "aliases": ["N", "n"]},
    {"truck_id": "TO", "display_name": "Truck O", "aliases": ["O", "o"]},
    {
        "truck_id": "T_ARJ_WHITE",
        "display_name": "Arjun White",
        "aliases": ["Arjun white", "ArjunWhite", "Arjun White", "arjun white"],
    },
    {
        "truck_id": "T_ARJ_NOVO",
        "display_name": "Arjun Novo",
        "aliases": [
            "Arjun novo",
            "Arjun Novo",
            "NOVO 655",
            "Novo 655",
            "Arjun novo 4841",
            "Arjun Novo 4841",
        ],
    },
    {
        "truck_id": "T_ARJ_ULTRA",
        "display_name": "Arjun Ultra",
        "aliases": ["Arjun ultra", "Arjun ultra 1"],
    },
    {
        "truck_id": "T_ARJ_605",
        "display_name": "Arjun 605",
        "aliases": ["Arjun 605", "ARJUN 605", "Arjun 3004", "Arjun 3484", "Arjun 5516"],
    },
    {
        "truck_id": "T_UP80",
        "display_name": "UP80CV0829",
        "aliases": ["UP80CV0829", "UP0829", "up80fz"],
    },
    {"truck_id": "T_UP26", "display_name": "UP26AB7192", "aliases": ["UP26AB7192"]},
    {
        "truck_id": "T_FARMTRAC",
        "display_name": "Farmtrac 6055",
        "aliases": ["FARMTRAC 6055", "Farmtrac 6055"],
    },
    {"truck_id": "T_JD", "display_name": "JD 5405", "aliases": ["JD 5405"]},
]

SITES = [
    {
        "site_id": "DAIRY",
        "display_name": "Dairy",
        "site_type": "unloading",
        "aliases": ["Dairy", "dairy", "DAIRY"],
    },
    {
        "site_id": "BG",
        "display_name": "Bhandara Ground",
        "site_type": "depot",
        "aliases": [
            "BG",
            "bg",
            "Bg",
            "Bhandara Ground",
            "Bhandaagar",
            "Bhandaagar Ground",
            "Bandaagar",
            "Bandaagar Ground",
        ],
    },
    {
        "site_id": "KN4",
        "display_name": "Kua No. 4",
        "site_type": "loading",
        "aliases": ["KN4", "kn4", "Kn4", "kN4", "Kua No 4", "Kua No. 4"],
    },
    {
        "site_id": "SOC",
        "display_name": "SOC",
        "site_type": "loading",
        "aliases": ["SOC", "SoC", "soc", "Soc", "SOc"],
    },
    {
        "site_id": "TN",
        "display_name": "TN Site",
        "site_type": "loading",
        "aliases": ["TN", "tn"],
    },
    {
        "site_id": "PL",
        "display_name": "PL Site",
        "site_type": "loading",
        "aliases": ["PL", "pl"],
    },
    {
        "site_id": "KHET",
        "display_name": "Field",
        "site_type": "loading",
        "aliases": ["Khet", "khet", "KHET"],
    },
]


def api_post(path, data):
    url = f"{BASE}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return json.loads(body) if body else {"error": str(e)}, e.code


def seed_trucks():
    print(f"Seeding {len(TRUCKS)} trucks...")
    for t in TRUCKS:
        resp, status = api_post("/api/registry/trucks", t)
        if status == 201:
            print(f"  ✓ {t['truck_id']} ({t['display_name']})")
        elif status == 409:
            print(f"  ~ {t['truck_id']} (already exists)")
        else:
            print(f"  ✗ {t['truck_id']}: {resp}")


def seed_sites():
    print(f"\nSeeding {len(SITES)} sites...")
    for s in SITES:
        resp, status = api_post("/api/registry/sites", s)
        if status == 201:
            print(f"  ✓ {s['site_id']} ({s['display_name']})")
        elif status == 409:
            print(f"  ~ {s['site_id']} (already exists)")
        else:
            print(f"  ✗ {s['site_id']}: {resp}")


def verify():
    import urllib.request

    with urllib.request.urlopen(f"{BASE}/api/registry/trucks", timeout=10) as r:
        trucks = json.loads(r.read()).get("trucks", [])
    with urllib.request.urlopen(f"{BASE}/api/registry/sites", timeout=10) as r:
        sites = json.loads(r.read()).get("sites", [])
    print(f"\nVerification: {len(trucks)} trucks, {len(sites)} sites")
    for t in trucks:
        print(
            f"  Truck: {t['truck_id']} | {t['display_name']} | aliases={t.get('aliases', [])}"
        )
    for s in sites:
        print(
            f"  Site:  {s['site_id']} | {s['display_name']} | type={s['site_type']} | aliases={s.get('aliases', [])}"
        )


if __name__ == "__main__":
    seed_trucks()
    seed_sites()
    verify()
