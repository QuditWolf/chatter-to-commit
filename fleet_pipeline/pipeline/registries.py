"""
Load truck/site registries from the database for use in Level 3 prompt building.
Also provides build_vocab_from_seed() for Level 2 without a DB.
"""
import json
from typing import Any, Dict, Set, Tuple

from fleet_pipeline.config import DB_PATH
from fleet_pipeline.db.database import db_conn, get_all_trucks, get_all_sites


def load_truck_registry(db_path: str = DB_PATH) -> Dict[str, list]:
    """Returns {truck_id: [alias, ...]} for all active trucks."""
    with db_conn(db_path) as conn:
        trucks = get_all_trucks(conn)
    return {t["truck_id"]: json.loads(t["aliases"]) for t in trucks}


def load_site_registry(db_path: str = DB_PATH) -> Dict[str, list]:
    """Returns {site_id: [alias, ...]} for all active sites."""
    with db_conn(db_path) as conn:
        sites = get_all_sites(conn)
    return {s["site_id"]: json.loads(s["aliases"]) for s in sites}


def resolve_truck_id(alias: str, truck_registry: Dict[str, list]) -> str | None:
    """Find truck_id for a given alias string (case-insensitive)."""
    alias_lc = alias.lower()
    for truck_id, aliases in truck_registry.items():
        for a in aliases:
            if a.lower() == alias_lc:
                return truck_id
    return None


def resolve_site_id(alias: str, site_registry: Dict[str, list]) -> str | None:
    """Find site_id for a given alias string (case-insensitive)."""
    alias_lc = alias.lower()
    for site_id, aliases in site_registry.items():
        for a in aliases:
            if a.lower() == alias_lc:
                return site_id
    return None


def build_vocab_from_seed() -> Tuple[Set[str], Set[str]]:
    """
    Returns (truck_vocab, site_vocab) sets built from the seed data lists.
    Used by Level 2 when DB is not available.
    """
    from fleet_pipeline.db.seed_data import TRUCKS, SITES
    truck_vocab: Set[str] = set()
    for t in TRUCKS:
        truck_vocab.add(t["truck_id"])
        for a in t["aliases"]:
            truck_vocab.add(a)
    site_vocab: Set[str] = set()
    for s in SITES:
        site_vocab.add(s["site_id"])
        for a in s["aliases"]:
            site_vocab.add(a)
    return truck_vocab, site_vocab


def build_vocab_from_db(db_path: str = DB_PATH) -> Tuple[Set[str], Set[str]]:
    """
    Returns (truck_vocab, site_vocab) sets loaded from the DB.
    Used by Level 2 when DB is available.
    """
    truck_registry = load_truck_registry(db_path)
    site_registry = load_site_registry(db_path)
    truck_vocab: Set[str] = set()
    for truck_id, aliases in truck_registry.items():
        truck_vocab.add(truck_id)
        truck_vocab.update(aliases)
    site_vocab: Set[str] = set()
    for site_id, aliases in site_registry.items():
        site_vocab.add(site_id)
        site_vocab.update(aliases)
    return truck_vocab, site_vocab
