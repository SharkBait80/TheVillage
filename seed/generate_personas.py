"""Persona + AgentState generation for the Melbourne Agent Village.

Produces agent dicts conforming to the merged Persona + Agent_State schema in
DESIGN.md section 4, subject to Requirement 4 (personas) and Requirement 5.8 /
9.1 (initial state, employment).

Deterministic given ``seed``. Stdlib only.
"""

from __future__ import annotations

import random
from typing import Any

# Occupations here MUST match the occupations defined in jobs.json so that
# Req 4.7 employment (employed if occupation matches a held Job) resolves.
# The weight is the number of Job slots of that occupation in jobs.json, so the
# generated occupation distribution tracks real job availability and a healthy
# majority of agents can actually be assigned a job.
JOB_OCCUPATION_SLOTS = {
    "Barista": 2,
    "Chef": 2,
    "Waiter": 2,
    "Market Vendor": 2,
    "Nurse": 2,
    "Doctor": 1,
    "Lecturer": 2,
    "Librarian": 1,
    "Accountant": 1,
    "Lawyer": 1,
    "Software Engineer": 2,
    "Media Producer": 1,
    "Retail Assistant": 3,
    "Council Officer": 1,
    "Public Servant": 1,
}
JOB_OCCUPATIONS = list(JOB_OCCUPATION_SLOTS.keys())

# A few occupations with no matching Job -> agent starts unemployed (Req 4.7).
NON_JOB_OCCUPATIONS = [
    "Freelance Illustrator",
    "Retired Teacher",
    "Musician",
    "Student",
    "Writer",
]

# Culturally diverse, plausible Melbourne name pools (multicultural city).
FIRST_NAMES = [
    "Aroha", "Liam", "Mei", "Ravi", "Sofia", "Jack", "Aisha", "Noah",
    "Priya", "Marco", "Chloe", "Hamish", "Yuki", "Amara", "Dimitri",
    "Isla", "Sanjay", "Grace", "Tomas", "Leila", "Oliver", "Nadia",
    "Wei", "Ruby", "Kofi", "Elena", "Darcy", "Fatima", "Angus", "Thanh",
    "Zara", "Mateo", "Anh", "Ivy", "Diego", "Harper", "Sina", "Bilal",
    "Freya", "Kai", "Lucia", "Omar", "Poppy", "Rohan", "Talia", "Ezra",
    "Mila", "Jarrah", "Astrid", "Hassan",
    # Expanded pool for larger populations (culturally diverse Melbourne).
    "Ana", "Bao", "Cormac", "Deepa", "Eun", "Farida", "Gethin", "Hana",
    "Ihaka", "Jun", "Keira", "Lachlan", "Manon", "Nikhil", "Orla", "Paolo",
    "Qadir", "Rania", "Seong", "Tara", "Uma", "Viktor", "Willa", "Xanthe",
    "Yara", "Zane", "Amelie", "Bodhi", "Cara", "Dev", "Esme", "Finn",
    "Georgia", "Huong", "Indira", "Jonah", "Keanu", "Lena", "Malik", "Niamh",
]

LAST_NAMES = [
    "Nguyen", "Smith", "Chen", "Patel", "Rossi", "OConnor", "Khan",
    "Williams", "Sharma", "Ferrari", "Taylor", "MacLeod", "Tanaka",
    "Okafor", "Petrou", "Brown", "Reddy", "Kowalski", "Silva", "Haddad",
    "Wilson", "Ahmadi", "Wang", "Murphy", "Mensah", "Costa", "Anderson",
    "Begum", "Campbell", "Tran", "Lopez", "Singh", "Pham", "Jones",
    "Hernandez", "Dubois", "Yilmaz", "Ali", "Andersson", "Lee",
    # Expanded pool for larger populations.
    "Kim", "Martin", "Rahman", "Novak", "Fernandez", "Osei", "Ivanov",
    "Nakamura", "Delgado", "Fraser", "Bianchi", "Schmidt", "Popescu",
    "Abebe", "Vidal", "Nasser", "Walsh", "Ryan", "Gallo", "Zhou",
]

TRAITS = [
    "warm", "impulsive", "curious", "reserved", "ambitious", "cheerful",
    "meticulous", "anxious", "generous", "witty", "stubborn", "gentle",
    "adventurous", "pragmatic", "dreamy", "loyal", "sardonic", "patient",
    "restless", "empathetic", "frugal", "outgoing", "introspective",
    "competitive", "easygoing", "principled", "playful", "cautious",
]

# The 16 Myers-Briggs personality types. Each agent is assigned one (Req: varied
# personalities). The type governs decisions (E/I socialising, J/P structure)
# and conversational tone (S/N, T/F) in the engine's heuristics.
MBTI_TYPES = [
    "ISTJ", "ISFJ", "INFJ", "INTJ",
    "ISTP", "ISFP", "INFP", "INTP",
    "ESTP", "ESFP", "ENFP", "ENTP",
    "ESTJ", "ESFJ", "ENFJ", "ENTJ",
]

# Rough suburb feel for background flavour keyed off home location id prefix.
BACKGROUND_TEMPLATES = [
    "Grew up in regional Victoria before moving to Melbourne for {occ_lower} work; loves the laneway coffee culture.",
    "A long-time inner-city local who knows every tram route and the best {occ_lower} shortcuts across the CBD.",
    "Moved to Melbourne from overseas a few years ago and has slowly built a life around {occ_lower} and weekend markets.",
    "Studied nearby and stayed in the city, balancing {occ_lower} with a fondness for the Botanic Gardens and Fed Square.",
    "Raised in the northern suburbs, now settled in the inner city and devoted to their craft as a {occ_lower}.",
    "A creative soul who fell for Melbourne's arts scene and now works as a {occ_lower} while chasing side projects.",
]


def _make_name_pools(rng: random.Random) -> tuple[list[str], list[str]]:
    first = FIRST_NAMES[:]
    last = LAST_NAMES[:]
    rng.shuffle(first)
    rng.shuffle(last)
    return first, last


def _unique_full_name(rng: random.Random, first: list[str], last: list[str],
                      used: set[str]) -> str:
    """Generate a full name unique case-insensitively after trimming (Req 4.5/4.6)."""
    for _ in range(200):
        name = f"{rng.choice(first)} {rng.choice(last)}"
        key = name.strip().lower()
        if key not in used:
            used.add(key)
            return name
    # Fall back to a suffixed name to guarantee uniqueness.
    base = f"{rng.choice(first)} {rng.choice(last)}"
    n = 2
    while f"{base} {n}".strip().lower() in used:
        n += 1
    name = f"{base} {n}"
    used.add(name.strip().lower())
    return name


def generate_personas(count: int, locations: list, seed: int = 42,
                      enrich=None) -> list[dict]:
    """Generate ``count`` merged Persona + Agent_State agent dicts.

    Args:
        count: number of agents, 5..2000 (Req 4.2/4.4; cap raised to support
            large-scale populations of 500+).
        locations: list of Location dicts (as in locations.json); used to pick
            residence homes and set initial position (Req 4.7).
        seed: RNG seed for deterministic output.

    Returns:
        list of agent dicts matching DESIGN.md section 4 Agent schema.
    """
    if not isinstance(count, int) or isinstance(count, bool):
        raise ValueError("count must be an integer")
    if not (5 <= count <= 2000):
        raise ValueError("count must be an integer between 5 and 2000 inclusive")

    residences = [l for l in locations if l.get("category") == "residence"]
    if not residences:
        raise ValueError("no residence locations available for homeLocationId")

    rng = random.Random(seed)
    first, last = _make_name_pools(rng)
    used_names: set[str] = set()

    # Build the occupation assignment deterministically. We aim to employ a
    # clear majority (~72%) of the population by assigning job-matching
    # occupations, then pad the remainder with non-job occupations
    # (intentionally unemployed, Req 4.7). For small populations the number of
    # job occupations is bounded by the concrete Job-slot pool; for large
    # populations we repeat the job-occupation pool (weighted by slot count) so
    # the employed ratio scales rather than staying pinned to ~24 agents.
    job_occ_set = set(JOB_OCCUPATIONS)
    slot_occupations: list[str] = []
    for occ, slots in JOB_OCCUPATION_SLOTS.items():
        slot_occupations.extend([occ] * slots)

    # Target ~72% of the population as employed (job) occupations.
    target_job_agents = int(round(count * 0.72))

    # Repeat the slot-weighted occupation pool until we have enough entries to
    # cover the target, so the occupation mix tracks real job availability.
    job_pool: list[str] = []
    while len(job_pool) < target_job_agents:
        job_pool.extend(slot_occupations)
    rng.shuffle(job_pool)
    occupation_assignment = job_pool[:target_job_agents]
    while len(occupation_assignment) < count:
        occupation_assignment.append(rng.choice(NON_JOB_OCCUPATIONS))
    rng.shuffle(occupation_assignment)

    # Assign MBTI personality types so the population is evenly varied across
    # all 16 types (deterministic). Cycling the shuffled type list then
    # shuffling the per-agent assignment keeps the distribution balanced while
    # decoupling personality from occupation order.
    mbti_pool: list[str] = []
    while len(mbti_pool) < count:
        mbti_pool.extend(MBTI_TYPES)
    mbti_assignment = mbti_pool[:count]
    rng.shuffle(mbti_assignment)

    agents: list[dict] = []
    for i in range(count):
        name = _unique_full_name(rng, first, last, used_names)
        occupation = occupation_assignment[i]
        age = rng.randint(18, 85)

        n_traits = rng.randint(3, 6)
        traits = rng.sample(TRAITS, n_traits)

        occ_lower = occupation.lower()
        background = rng.choice(BACKGROUND_TEMPLATES).format(occ_lower=occ_lower)
        background = background[:1000]  # clamp to 1..1000 chars
        mbti = mbti_assignment[i]

        # Optional LLM enrichment: when an `enrich` callable is supplied it may
        # return a unique, in-character biography and personality traits for this
        # agent. Any failure or missing field falls back to the deterministic
        # template above so seeding never breaks (Req 4.1 length constraints are
        # re-clamped defensively).
        if enrich is not None:
            try:
                extra = enrich({
                    "name": name, "age": age, "occupation": occupation,
                    "mbti": mbti, "traits": traits,
                }) or {}
                bio = str(extra.get("background") or "").strip()
                if bio:
                    background = bio[:1000]
                new_traits = extra.get("traits")
                if isinstance(new_traits, list):
                    cleaned = [str(t).strip()[:40] for t in new_traits if str(t).strip()]
                    if 3 <= len(cleaned) <= 6:
                        traits = cleaned
            except Exception:  # noqa: BLE001 — enrichment must never break seeding
                pass

        home = rng.choice(residences)
        home_id = home["id"]
        home_lat = home["lat"]
        home_lon = home["lon"]

        wake_minutes = rng.randint(6 * 60, 9 * 60)  # 06:00..09:00 inclusive
        wake_time = f"{wake_minutes // 60:02d}:{wake_minutes % 60:02d}"

        # Needs 60..90 (Req 4.7). Config default is 70; we vary within range.
        needs = {
            "hunger": rng.randint(60, 90),
            "energy": rng.randint(60, 90),
            "social": rng.randint(60, 90),
            "fun": rng.randint(60, 90),
        }
        needs_fraction = {k: 0.0 for k in needs}
        critical = {k: (v < 20) for k, v in needs.items()}

        employed = occupation in job_occ_set
        employment_status = "employed" if employed else "unemployed"

        cash = round(rng.uniform(50.0, 500.0), 2)
        daily_living_cost = round(rng.uniform(20.0, 80.0), 2)

        agent_id = f"agent_{i + 1:02d}"

        agent = {
            "schemaVersion": 1,
            "id": agent_id,
            "provenance": "generated",
            "persistedSimTime": None,
            "persona": {
                "name": name,
                "age": age,
                "occupation": occupation,
                "traits": traits,
                "background": background,
                "homeLocationId": home_id,
                "wakeTime": wake_time,
                "mbti": mbti,
            },
            "state": {
                "lat": home_lat,
                "lon": home_lon,
                "presentLocationId": home_id,
                "needs": needs,
                "needsFraction": needs_fraction,
                "critical": critical,
                "cash": cash,
                "employmentStatus": employment_status,
                "legalStatus": "clear",
                "jobId": None,
                "dailyLivingCost": daily_living_cost,
                "currentAction": {
                    "type": "idle",
                    "targetType": "location",
                    "targetId": home_id,
                    "expectedDurationMin": 10,
                    "startedSimTime": None,
                    "progress": 0.0,
                    "route": None,
                },
                "dayPlan": [],
                "detainedReleaseSimTime": None,
                "detectedCrimeCount": 0,
                "suspectedSince": None,
                "missedShiftStreak": 0,
            },
            "relationships": [],
        }
        agents.append(agent)

    # Initial relationships: 0..10 per agent (Req 4.1). Deterministic.
    _seed_relationships(rng, agents)

    return agents


def _seed_relationships(rng: random.Random, agents: list[dict]) -> None:
    """Attach 0..10 directed initial relationships per agent (Req 4.1)."""
    ids = [a["id"] for a in agents]
    for a in agents:
        others = [x for x in ids if x != a["id"]]
        max_rel = min(10, len(others))
        n = rng.randint(0, max_rel)
        targets = rng.sample(others, n) if n else []
        rels = []
        for t in targets:
            rels.append({
                "schemaVersion": 1,
                "from": a["id"],
                "to": t,
                "familiarity": rng.randint(0, 40),
                "sentiment": rng.randint(-20, 40),
            })
        a["relationships"] = rels


if __name__ == "__main__":
    import json
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "locations.json")) as f:
        locs = json.load(f)["locations"]
    result = generate_personas(25, locs, seed=42)
    print(json.dumps(result[:2], indent=2))
    print(f"generated {len(result)} agents")
