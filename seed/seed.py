#!/usr/bin/env python3
"""Seed loader for the Melbourne Agent Village.

Reads locations.json, jobs.json, config.json and generates personas, validates
everything against Requirements 3 (locations) and 4 (personas), assigns jobs to
matching employed agents, and writes all items to DynamoDB using the DESIGN.md
single-table key schema (PK=SIM#<simId>, SK per entity type).

Usage:
    python3 seed.py --dry-run                 # validate only, no AWS calls
    python3 seed.py --table village --sim-id melb

stdlib + boto3 only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

from generate_personas import generate_personas

HERE = os.path.dirname(os.path.abspath(__file__))

# Map bounds (Req 3.3).
LAT_MIN, LAT_MAX = -38.00, -37.70
LON_MIN, LON_MAX = 144.85, 145.10
CATEGORIES = {"residence", "workplace", "food", "retail", "leisure", "transit", "civic"}
TIME_RE_OK = lambda s: (  # noqa: E731
    isinstance(s, str)
    and len(s) == 5
    and s[2] == ":"
    and s[:2].isdigit()
    and s[3:].isdigit()
    and 0 <= int(s[:2]) <= 23
    and 0 <= int(s[3:]) <= 59
)


class ValidationError(Exception):
    pass


def _load_json(name: str) -> dict:
    with open(os.path.join(HERE, name)) as f:
        return json.load(f)


# Cache of raw decimal-place counts per (locationId, coord) parsed from the
# JSON source text, so trailing-zero loss in float repr doesn't cause false
# failures on the "at least 6 decimal places" constraint (Req 3.1).
_RAW_DECIMALS: dict[tuple[str, str], int] = {}


def _build_raw_decimals() -> None:
    import re
    path = os.path.join(HERE, "locations.json")
    with open(path) as f:
        text = f.read()
    # Split into per-location blocks by id, then find lat/lon numeric literals.
    id_iter = list(re.finditer(r'"id"\s*:\s*"([^"]+)"', text))
    for idx, m in enumerate(id_iter):
        lid = m.group(1)
        start = m.end()
        end = id_iter[idx + 1].start() if idx + 1 < len(id_iter) else len(text)
        block = text[start:end]
        for coord in ("lat", "lon"):
            cm = re.search(rf'"{coord}"\s*:\s*(-?\d+)\.(\d+)', block)
            if cm:
                _RAW_DECIMALS[(lid, coord)] = len(cm.group(2))


def _raw_coord_decimals(lid: str, coord: str):
    if not _RAW_DECIMALS:
        _build_raw_decimals()
    return _RAW_DECIMALS.get((lid, coord))


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_locations(locations: list) -> None:
    """Validate against Requirement 3 constraints."""
    if not (30 <= len(locations) <= 500):
        raise ValidationError(
            f"location count {len(locations)} outside 30..500 (Req 3.1)"
        )
    seen_ids: set[str] = set()
    cat_counts: dict[str, int] = {c: 0 for c in CATEGORIES}
    detention = []
    for i, loc in enumerate(locations):
        lid = loc.get("id")
        if not lid:
            raise ValidationError(f"location #{i} missing id")
        if lid in seen_ids:
            raise ValidationError(f"duplicate location id {lid} (Req 3.8)")
        seen_ids.add(lid)
        name = loc.get("name", "")
        if not (1 <= len(name) <= 80):
            raise ValidationError(f"{lid}: name length invalid (Req 3.1)")
        cat = loc.get("category")
        if cat not in CATEGORIES:
            raise ValidationError(f"{lid}: bad category {cat!r} (Req 3.8)")
        cat_counts[cat] += 1
        lat, lon = loc.get("lat"), loc.get("lon")
        if not (isinstance(lat, (int, float)) and LAT_MIN <= lat <= LAT_MAX):
            raise ValidationError(f"{lid}: lat {lat} outside bounds (Req 3.3)")
        if not (isinstance(lon, (int, float)) and LON_MIN <= lon <= LON_MAX):
            raise ValidationError(f"{lid}: lon {lon} outside bounds (Req 3.3)")
        # 6+ decimal places check (against the JSON source text, since float
        # repr drops trailing zeros: 144.967190 -> 144.96719).
        for coord_name in ("lat", "lon"):
            raw = _raw_coord_decimals(lid, coord_name)
            if raw is not None and raw < 6:
                raise ValidationError(
                    f"{lid}: {coord_name} written with {raw} decimal places (<6) (Req 3.1)"
                )
        cap = loc.get("capacity")
        if not (isinstance(cap, int) and 1 <= cap <= 5000):
            raise ValidationError(f"{lid}: capacity {cap} outside 1..5000 (Req 3.1/3.8)")
        hours = loc.get("hours")
        if not (isinstance(hours, list) and len(hours) == 7):
            raise ValidationError(f"{lid}: hours must be 7 entries (Req 3.1)")
        for d, h in enumerate(hours):
            if not (TIME_RE_OK(h.get("open")) and TIME_RE_OK(h.get("close"))):
                raise ValidationError(f"{lid}: bad hours HH:MM at day {d} (Req 3.1)")
        if cat in ("food", "retail"):
            price = loc.get("price")
            if not (isinstance(price, (int, float)) and 0.01 <= price <= 999.99):
                raise ValidationError(f"{lid}: price {price} outside 0.01..999.99")
        if loc.get("isDetentionFacility"):
            if cat != "civic":
                raise ValidationError(f"{lid}: detention facility must be civic")
            detention.append(lid)
    for c in CATEGORIES:
        if cat_counts[c] < 2:
            raise ValidationError(f"category {c} has {cat_counts[c]} (<2) (Req 3.2)")
    if len(detention) != 1:
        raise ValidationError(
            f"expected exactly 1 detention facility, got {len(detention)}"
        )
    return cat_counts, detention[0]


def validate_jobs(jobs: list, locations: list) -> None:
    """Validate Job constraints (Req 9.1)."""
    loc_ids = {l["id"] for l in locations}
    loc_by_id = {l["id"]: l for l in locations}
    valid_job_loc_cats = {"workplace", "food", "retail", "civic"}
    seen: set[str] = set()
    for j in jobs:
        jid = j.get("id")
        if not jid or jid in seen:
            raise ValidationError(f"job id missing or duplicate: {jid}")
        seen.add(jid)
        loc = j.get("locationId")
        if loc not in loc_ids:
            raise ValidationError(f"{jid}: locationId {loc} not found")
        if loc_by_id[loc]["category"] not in valid_job_loc_cats:
            raise ValidationError(
                f"{jid}: location {loc} category not workplace/food/retail/civic"
            )
        wage = j.get("wagePerHour")
        if not (isinstance(wage, (int, float)) and 15.00 <= wage <= 200.00):
            raise ValidationError(f"{jid}: wagePerHour {wage} outside 15..200 (Req 9.1)")
        if not TIME_RE_OK(j.get("shiftStart")):
            raise ValidationError(f"{jid}: bad shiftStart (Req 9.1)")
        dur = j.get("shiftDurationHours")
        if not (isinstance(dur, int) and 1 <= dur <= 12):
            raise ValidationError(f"{jid}: shiftDurationHours {dur} outside 1..12")
        if not j.get("occupation"):
            raise ValidationError(f"{jid}: missing occupation")


def validate_agents(agents: list, locations: list) -> None:
    """Validate against Requirement 4 constraints."""
    if not (5 <= len(agents) <= 2000):
        raise ValidationError(f"agent count {len(agents)} outside 5..2000 (Req 4.2)")
    residence_ids = {l["id"] for l in locations if l["category"] == "residence"}
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for a in agents:
        aid = a.get("id")
        if not aid or aid in seen_ids:
            raise ValidationError(f"agent id missing or duplicate: {aid}")
        seen_ids.add(aid)
        if a.get("provenance") != "generated":
            raise ValidationError(f"{aid}: provenance must be 'generated' (Req 4.10)")
        p = a.get("persona", {})
        name = p.get("name", "")
        if not (1 <= len(name) <= 80):
            raise ValidationError(f"{aid}: name length invalid (Req 4.1)")
        key = name.strip().lower()
        if key in seen_names:
            raise ValidationError(f"{aid}: duplicate full name {name!r} (Req 4.5)")
        seen_names.add(key)
        age = p.get("age")
        if not (isinstance(age, int) and 18 <= age <= 85):
            raise ValidationError(f"{aid}: age {age} outside 18..85 (Req 4.1)")
        occ = p.get("occupation", "")
        if not (1 <= len(occ) <= 80):
            raise ValidationError(f"{aid}: occupation length invalid (Req 4.1)")
        traits = p.get("traits", [])
        if not (3 <= len(traits) <= 6):
            raise ValidationError(f"{aid}: {len(traits)} traits outside 3..6 (Req 4.1)")
        for t in traits:
            if not (1 <= len(t) <= 40):
                raise ValidationError(f"{aid}: trait length invalid (Req 4.1)")
        bg = p.get("background", "")
        if not (1 <= len(bg) <= 1000):
            raise ValidationError(f"{aid}: background length invalid (Req 4.1)")
        home = p.get("homeLocationId")
        if home not in residence_ids:
            raise ValidationError(f"{aid}: homeLocationId {home} not a residence (Req 4.1/3.7)")
        rels = a.get("relationships", [])
        if not (0 <= len(rels) <= 10):
            raise ValidationError(f"{aid}: {len(rels)} relationships outside 0..10 (Req 4.1)")
        # AgentState (Req 4.7 / 5.8)
        s = a.get("state", {})
        for need, v in s.get("needs", {}).items():
            if not (0 <= v <= 100):
                raise ValidationError(f"{aid}: need {need}={v} outside 0..100")
            if not (60 <= v <= 90):
                raise ValidationError(f"{aid}: initial need {need}={v} outside 60..90 (Req 4.7)")
        if s.get("legalStatus") != "clear":
            raise ValidationError(f"{aid}: legalStatus must be 'clear' initially (Req 4.7)")
        if s.get("employmentStatus") not in ("employed", "unemployed"):
            raise ValidationError(f"{aid}: bad employmentStatus (Req 4.7)")
        cash = s.get("cash")
        if not (isinstance(cash, (int, float)) and 50.0 <= cash <= 500.0):
            raise ValidationError(f"{aid}: cash {cash} outside 50..500 (Req 4.7)")
        dlc = s.get("dailyLivingCost")
        if not (isinstance(dlc, (int, float)) and 20.0 <= dlc <= 80.0):
            raise ValidationError(f"{aid}: dailyLivingCost {dlc} outside 20..80 (Req 4)")
        wt = p.get("wakeTime")
        if not TIME_RE_OK(wt) or not ("06:00" <= wt <= "09:00"):
            raise ValidationError(f"{aid}: wakeTime {wt} outside 06:00..09:00 (Req 4)")
        # position == home
        home_loc = next(l for l in locations if l["id"] == home)
        if abs(s.get("lat") - home_loc["lat"]) > 1e-9 or abs(s.get("lon") - home_loc["lon"]) > 1e-9:
            raise ValidationError(f"{aid}: position not at home location (Req 4.7)")


def validate_config(config: dict, detention_id: str) -> None:
    if config.get("simId") != "melb":
        raise ValidationError("config simId must be 'melb'")
    if config.get("status") != "stopped":
        raise ValidationError("config status must be 'stopped'")
    af = config.get("accelerationFactor")
    if not (isinstance(af, int) and 1 <= af <= 60):
        raise ValidationError(f"accelerationFactor {af} outside 1..60")
    if config.get("detentionFacilityId") != detention_id:
        raise ValidationError(
            f"detentionFacilityId {config.get('detentionFacilityId')} != {detention_id}"
        )
    art = config.get("artStyleClause", "")
    if not (0 < len(art) <= 200):
        raise ValidationError(f"artStyleClause length {len(art)} outside 1..200")
    budget = config.get("budget", {})
    if budget.get("maxInvocationsPerSimHour") != 5000:
        raise ValidationError("budget.maxInvocationsPerSimHour must be 5000")
    if abs(budget.get("maxSpendUSD", 0) - 25.00) > 1e-9:
        raise ValidationError("budget.maxSpendUSD must be 25.00")
    prices = budget.get("prices", {})
    for m in ("au.anthropic.claude-opus-5",
              "au.anthropic.claude-haiku-4-5-20251001-v1:0"):
        if m not in prices:
            raise ValidationError(f"budget.prices missing {m}")
        for k in ("per1kInput", "per1kOutput"):
            if not (0.0 <= prices[m].get(k, -1) <= 1000.00):
                raise ValidationError(f"{m}.{k} outside 0..1000")


# --------------------------------------------------------------------------- #
# Job assignment
# --------------------------------------------------------------------------- #
def assign_jobs(agents: list, jobs: list) -> int:
    """Assign concrete Job rows to matching employed agents.

    Returns the number of concrete jobs assigned. There are only a limited
    number of Job rows (jobs.json); at large populations more agents are
    ``employed`` by occupation than there are concrete Job rows. Such agents
    remain ``employed`` (their occupation matches a real job occupation) but
    carry ``jobId = None`` until a concrete shift is available — we do NOT
    demote them to unemployed, so the seeded world keeps a healthy employment
    rate at scale.
    """
    jobs_by_occ: dict[str, list] = {}
    for j in jobs:
        j["assignedAgentId"] = None
        jobs_by_occ.setdefault(j["occupation"], []).append(j)
    assigned = 0
    for a in agents:
        if a["state"]["employmentStatus"] != "employed":
            continue
        occ = a["persona"]["occupation"]
        pool = jobs_by_occ.get(occ, [])
        free = next((j for j in pool if j["assignedAgentId"] is None), None)
        if free is None:
            # No free concrete Job of that occupation. Keep the agent employed
            # (occupation matches a real job occupation) with no jobId yet.
            a["state"]["jobId"] = None
            continue
        free["assignedAgentId"] = a["id"]
        a["state"]["jobId"] = free["id"]
        assigned += 1
    return assigned


# --------------------------------------------------------------------------- #
# DynamoDB serialization
# --------------------------------------------------------------------------- #
def _to_dynamo(obj):
    """Recursively convert floats to Decimal for DynamoDB."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_dynamo(v) for v in obj]
    return obj


def build_items(sim_id: str, config: dict, locations: list, agents: list,
                jobs: list) -> list[dict]:
    """Build all DynamoDB items per DESIGN.md section 3 key schema."""
    pk = f"SIM#{sim_id}"
    items: list[dict] = []
    # Config
    items.append({"PK": pk, "SK": "CONFIG", **config})
    # Status
    items.append({"PK": pk, "SK": "STATUS",
                  "status": "stopped",
                  "simTime": config["startSimTime"],
                  "accel": config["accelerationFactor"]})
    # Locations
    for loc in locations:
        items.append({"PK": pk, "SK": f"LOC#{loc['id']}", **loc})
    # Agents
    for a in agents:
        items.append({"PK": pk, "SK": f"AGENT#{a['id']}", **a})
    # Jobs
    for j in jobs:
        items.append({"PK": pk, "SK": f"JOB#{j['id']}", **j})
    # Relationships (directed)
    for a in agents:
        for r in a.get("relationships", []):
            items.append({"PK": pk, "SK": f"REL#{r['from']}#{r['to']}", **r})
    return items


def write_to_dynamo(table_name: str, items: list[dict]) -> None:
    import boto3  # imported lazily so dry-run needs no boto3

    ddb = boto3.resource("dynamodb")
    table = ddb.Table(table_name)
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=_to_dynamo(item))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(table_name: str | None, sim_id: str, dry_run: bool,
        population: int | None = None) -> int:
    config = _load_json("config.json")
    locations = _load_json("locations.json")["locations"]
    jobs = _load_json("jobs.json")["jobs"]

    pop = population if population is not None else config.get("population", 25)

    # Validate source data.
    cat_counts, detention_id = validate_locations(locations)
    validate_jobs(jobs, locations)
    validate_config(config, detention_id)

    # Generate + validate agents.
    agents = generate_personas(pop, locations, seed=42)
    validate_agents(agents, locations)

    # Assign jobs.
    n_assigned = assign_jobs(agents, jobs)

    # Summary.
    print("=" * 60)
    print("Melbourne Agent Village — seed summary")
    print("=" * 60)
    print(f"simId: {sim_id}")
    print(f"locations: {len(locations)}")
    for c in sorted(cat_counts):
        print(f"    {c:<11}: {cat_counts[c]}")
    print(f"detention facility: {detention_id}")
    print(f"jobs: {len(jobs)}")
    print(f"agents: {len(agents)} (population={pop})")
    employed = sum(1 for a in agents if a["state"]["employmentStatus"] == "employed")
    print(f"employed agents: {employed} ({employed / len(agents) * 100:.0f}%)")
    print(f"jobs assigned: {n_assigned}")
    print("Validation: PASSED (Req 3 & Req 4 constraints satisfied)")

    if dry_run:
        print("\n[dry-run] No AWS calls made.")
        return 0

    if not table_name:
        print("ERROR: --table is required when not in --dry-run mode", file=sys.stderr)
        return 2

    items = build_items(sim_id, config, locations, agents, jobs)
    print(f"\nWriting {len(items)} items to DynamoDB table {table_name!r}...")
    write_to_dynamo(table_name, items)
    print(f"Wrote {len(items)} items.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Seed the Melbourne Agent Village")
    ap.add_argument("--table", help="DynamoDB table name (e.g. village)")
    ap.add_argument("--sim-id", default="melb", help="simulation id (default melb)")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate only; no AWS calls")
    ap.add_argument("--population", type=int, default=None,
                    help="override population count (5..2000)")
    args = ap.parse_args(argv)
    try:
        return run(args.table, args.sim_id, args.dry_run, args.population)
    except ValidationError as e:
        print(f"VALIDATION ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
