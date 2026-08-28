"""Domain models for the Melbourne Agent Village.

Pure dataclasses + enums matching the canonical JSON schemas in DESIGN.md §4.
schemaVersion is fixed at 1 (see SUPPORTED_SCHEMA_VERSIONS in __init__).
All monetary values are AUD unless noted; budget figures are USD.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------
class LocationCategory(str, Enum):
    RESIDENCE = "residence"
    WORKPLACE = "workplace"
    FOOD = "food"
    RETAIL = "retail"
    LEISURE = "leisure"
    TRANSIT = "transit"
    CIVIC = "civic"


class LocationStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    AT_CAPACITY = "at_capacity"


class EmploymentStatus(str, Enum):
    EMPLOYED = "employed"
    UNEMPLOYED = "unemployed"
    SUSPENDED = "suspended"


class LegalStatus(str, Enum):
    CLEAR = "clear"
    SUSPECTED = "suspected"
    CHARGED = "charged"
    DETAINED = "detained"


class ActionType(str, Enum):
    SLEEP = "sleep"
    EAT = "eat"
    WORK = "work"
    TRAVEL = "travel"
    SOCIALISE = "socialise"
    SHOP = "shop"
    LEISURE = "leisure"
    COMMIT_CRIME = "commit_crime"
    IDLE = "idle"


class CrimeType(str, Enum):
    THEFT = "theft"
    BURGLARY = "burglary"
    VANDALISM = "vandalism"
    FRAUD = "fraud"


class TravelMode(str, Enum):
    WALK = "walk"
    TRAM = "tram"
    CAR = "car"


class TargetType(str, Enum):
    LOCATION = "location"
    AGENT = "agent"


class Provenance(str, Enum):
    GENERATED = "generated"
    SUPPLIED = "supplied"


class SimStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class Outcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Detection(str, Enum):
    DETECTED = "detected"
    UNDETECTED = "undetected"


# Needs are the four canonical need names, index order fixed.
NEED_NAMES = ("hunger", "energy", "social", "fun")

# Event categories per DESIGN.md §4.
EVENT_CATEGORIES = (
    "action", "conversation", "crime", "legal", "employment",
    "model", "clock", "system", "planning", "memory",
)


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------
@dataclass
class OpeningHours:
    """One open/close pair for a single day of the week (HH:MM strings)."""
    open: str
    close: str

    def to_dict(self) -> Dict[str, Any]:
        return {"open": self.open, "close": self.close}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OpeningHours":
        return cls(open=d["open"], close=d["close"])


@dataclass
class Location:
    id: str
    name: str
    category: LocationCategory
    lat: float
    lon: float
    capacity: int
    # 7 entries, index 0 = Monday .. index 6 = Sunday.
    hours: List[OpeningHours] = field(default_factory=list)
    isDetentionFacility: bool = False
    price: Optional[float] = None  # food/retail only, 0.01..999.99 AUD
    schemaVersion: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "schemaVersion": self.schemaVersion,
            "id": self.id,
            "name": self.name,
            "category": self.category.value,
            "lat": self.lat,
            "lon": self.lon,
            "capacity": self.capacity,
            "hours": [h.to_dict() for h in self.hours],
            "isDetentionFacility": self.isDetentionFacility,
        }
        if self.price is not None:
            d["price"] = self.price
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Location":
        return cls(
            id=d["id"],
            name=d["name"],
            category=LocationCategory(d["category"]),
            lat=float(d["lat"]),
            lon=float(d["lon"]),
            capacity=int(d["capacity"]),
            hours=[OpeningHours.from_dict(h) for h in d.get("hours", [])],
            isDetentionFacility=bool(d.get("isDetentionFacility", False)),
            price=(float(d["price"]) if d.get("price") is not None else None),
            schemaVersion=int(d.get("schemaVersion", SCHEMA_VERSION)),
        )


@dataclass
class Persona:
    name: str
    age: int
    occupation: str
    traits: List[str]
    background: str
    homeLocationId: str
    wakeTime: str = "07:00"
    # Myers-Briggs personality type (e.g. "ENFP"). Optional for backward
    # compatibility with personas persisted before this field existed; when
    # absent it reads as "" and the behaviour engine falls back to neutral.
    mbti: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "age": self.age,
            "occupation": self.occupation,
            "traits": list(self.traits),
            "background": self.background,
            "homeLocationId": self.homeLocationId,
            "wakeTime": self.wakeTime,
            "mbti": self.mbti,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Persona":
        return cls(
            name=d["name"],
            age=int(d["age"]),
            occupation=d["occupation"],
            traits=list(d.get("traits", [])),
            background=d.get("background", ""),
            homeLocationId=d["homeLocationId"],
            wakeTime=d.get("wakeTime", "07:00"),
            mbti=(d.get("mbti") or "").upper(),
        )


@dataclass
class Action:
    type: ActionType
    targetType: TargetType
    targetId: str
    expectedDurationMin: int
    startedSimTime: Optional[str] = None
    progress: float = 0.0
    route: Optional[List[List[float]]] = None  # list of [lat, lon]
    crimeType: Optional[CrimeType] = None
    travelMode: Optional[TravelMode] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "type": self.type.value,
            "targetType": self.targetType.value,
            "targetId": self.targetId,
            "expectedDurationMin": self.expectedDurationMin,
            "startedSimTime": self.startedSimTime,
            "progress": self.progress,
            "route": self.route,
        }
        if self.crimeType is not None:
            d["crimeType"] = self.crimeType.value
        if self.travelMode is not None:
            d["travelMode"] = self.travelMode.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Action":
        return cls(
            type=ActionType(d["type"]),
            targetType=TargetType(d["targetType"]),
            targetId=d["targetId"],
            expectedDurationMin=int(d["expectedDurationMin"]),
            startedSimTime=d.get("startedSimTime"),
            progress=float(d.get("progress", 0.0)),
            route=d.get("route"),
            crimeType=(CrimeType(d["crimeType"]) if d.get("crimeType") else None),
            travelMode=(TravelMode(d["travelMode"]) if d.get("travelMode") else None),
        )


@dataclass
class AgentState:
    lat: float
    lon: float
    presentLocationId: Optional[str]
    needs: Dict[str, int]  # hunger/energy/social/fun -> int 0..100
    needsFraction: Dict[str, float] = field(default_factory=dict)
    critical: Dict[str, bool] = field(default_factory=dict)
    cash: float = 0.0
    employmentStatus: EmploymentStatus = EmploymentStatus.UNEMPLOYED
    legalStatus: LegalStatus = LegalStatus.CLEAR
    jobId: Optional[str] = None
    dailyLivingCost: float = 40.00
    currentAction: Optional[Action] = None
    dayPlan: List[Dict[str, Any]] = field(default_factory=list)
    detainedReleaseSimTime: Optional[str] = None
    detectedCrimeCount: int = 0
    suspectedSince: Optional[str] = None
    missedShiftStreak: int = 0
    # tracks conversation ids already credited social recovery (Req 5.4).
    creditedConversations: List[str] = field(default_factory=list)
    # Transient injected-world-event hints (backward-compatible, default empty).
    # avoidedLocations maps a hazardous locationId -> expiry sim-time ISO string
    # (the heuristic steers agents away until the hint expires). attractorLocation
    # is an optional {"locationId": str, "expiry": str} pull toward a positive
    # event (festival/market). Both round-trip through persistence.
    avoidedLocations: Dict[str, str] = field(default_factory=dict)
    attractorLocation: Optional[Dict[str, Any]] = None
    # Transient live-conversation marker for the current tick:
    # {"participants": [ids], "locationId": str}. Surfaced by the API /state so
    # the SPA renders a conversation indicator; cleared each tick when not in one.
    conversation: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "presentLocationId": self.presentLocationId,
            "needs": dict(self.needs),
            "needsFraction": dict(self.needsFraction),
            "critical": dict(self.critical),
            "cash": self.cash,
            "employmentStatus": self.employmentStatus.value,
            "legalStatus": self.legalStatus.value,
            "jobId": self.jobId,
            "dailyLivingCost": self.dailyLivingCost,
            "currentAction": self.currentAction.to_dict() if self.currentAction else None,
            "dayPlan": list(self.dayPlan),
            "detainedReleaseSimTime": self.detainedReleaseSimTime,
            "detectedCrimeCount": self.detectedCrimeCount,
            "suspectedSince": self.suspectedSince,
            "missedShiftStreak": self.missedShiftStreak,
            "creditedConversations": list(self.creditedConversations),
            "avoidedLocations": dict(self.avoidedLocations),
            "attractorLocation": (dict(self.attractorLocation)
                                  if self.attractorLocation else None),
            "conversation": (dict(self.conversation)
                             if self.conversation else None),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentState":
        return cls(
            lat=float(d["lat"]),
            lon=float(d["lon"]),
            presentLocationId=d.get("presentLocationId"),
            needs={k: int(v) for k, v in d.get("needs", {}).items()},
            needsFraction={k: float(v) for k, v in d.get("needsFraction", {}).items()},
            critical={k: bool(v) for k, v in d.get("critical", {}).items()},
            cash=float(d.get("cash", 0.0)),
            employmentStatus=EmploymentStatus(d.get("employmentStatus", "unemployed")),
            legalStatus=LegalStatus(d.get("legalStatus", "clear")),
            jobId=d.get("jobId"),
            dailyLivingCost=float(d.get("dailyLivingCost", 40.00)),
            currentAction=(Action.from_dict(d["currentAction"]) if d.get("currentAction") else None),
            dayPlan=list(d.get("dayPlan", [])),
            detainedReleaseSimTime=d.get("detainedReleaseSimTime"),
            detectedCrimeCount=int(d.get("detectedCrimeCount", 0)),
            suspectedSince=d.get("suspectedSince"),
            missedShiftStreak=int(d.get("missedShiftStreak", 0)),
            creditedConversations=list(d.get("creditedConversations", [])),
            avoidedLocations={str(k): str(v) for k, v in
                              (d.get("avoidedLocations") or {}).items()},
            attractorLocation=(dict(d["attractorLocation"])
                               if d.get("attractorLocation") else None),
            conversation=(dict(d["conversation"]) if d.get("conversation") else None),
        )


@dataclass
class Agent:
    id: str
    persona: Persona
    state: AgentState
    provenance: Provenance = Provenance.GENERATED
    persistedSimTime: Optional[str] = None
    schemaVersion: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "id": self.id,
            "provenance": self.provenance.value,
            "persistedSimTime": self.persistedSimTime,
            "persona": self.persona.to_dict(),
            "state": self.state.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Agent":
        return cls(
            id=d["id"],
            persona=Persona.from_dict(d["persona"]),
            state=AgentState.from_dict(d["state"]),
            provenance=Provenance(d.get("provenance", "generated")),
            persistedSimTime=d.get("persistedSimTime"),
            schemaVersion=int(d.get("schemaVersion", SCHEMA_VERSION)),
        )


@dataclass
class Job:
    id: str
    locationId: str
    occupation: str
    wagePerHour: float
    shiftStart: str  # HH:MM
    shiftDurationHours: int
    assignedAgentId: Optional[str] = None
    schemaVersion: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "id": self.id,
            "locationId": self.locationId,
            "occupation": self.occupation,
            "wagePerHour": self.wagePerHour,
            "shiftStart": self.shiftStart,
            "shiftDurationHours": self.shiftDurationHours,
            "assignedAgentId": self.assignedAgentId,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Job":
        return cls(
            id=d["id"],
            locationId=d["locationId"],
            occupation=d["occupation"],
            wagePerHour=float(d["wagePerHour"]),
            shiftStart=d["shiftStart"],
            shiftDurationHours=int(d["shiftDurationHours"]),
            assignedAgentId=d.get("assignedAgentId"),
            schemaVersion=int(d.get("schemaVersion", SCHEMA_VERSION)),
        )


@dataclass
class Relationship:
    from_id: str
    to_id: str
    familiarity: int = 0
    sentiment: int = 0
    schemaVersion: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "from": self.from_id,
            "to": self.to_id,
            "familiarity": self.familiarity,
            "sentiment": self.sentiment,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Relationship":
        return cls(
            from_id=d["from"],
            to_id=d["to"],
            familiarity=int(d.get("familiarity", 0)),
            sentiment=int(d.get("sentiment", 0)),
            schemaVersion=int(d.get("schemaVersion", SCHEMA_VERSION)),
        )


@dataclass
class CrimeEvent:
    id: str
    simTime: str
    perpetrator: str
    crimeType: CrimeType
    targetType: TargetType
    targetId: str
    witnesses: List[str] = field(default_factory=list)
    outcome: Outcome = Outcome.FAILED
    detection: Detection = Detection.UNDETECTED
    stolenAmount: int = 0
    schemaVersion: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "id": self.id,
            "simTime": self.simTime,
            "perpetrator": self.perpetrator,
            "crimeType": self.crimeType.value,
            "targetType": self.targetType.value,
            "targetId": self.targetId,
            "witnesses": list(self.witnesses),
            "outcome": self.outcome.value,
            "detection": self.detection.value,
            "stolenAmount": self.stolenAmount,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CrimeEvent":
        return cls(
            id=d["id"],
            simTime=d["simTime"],
            perpetrator=d["perpetrator"],
            crimeType=CrimeType(d["crimeType"]),
            targetType=TargetType(d["targetType"]),
            targetId=d["targetId"],
            witnesses=list(d.get("witnesses", [])),
            outcome=Outcome(d.get("outcome", "failed")),
            detection=Detection(d.get("detection", "undetected")),
            stolenAmount=int(d.get("stolenAmount", 0)),
            schemaVersion=int(d.get("schemaVersion", SCHEMA_VERSION)),
        )


@dataclass
class EventLogEntry:
    seq: int
    simTime: str
    realTime: str
    category: str
    agents: List[str] = field(default_factory=list)
    locationId: Optional[str] = None
    description: str = ""
    detail: Optional[Dict[str, Any]] = None
    schemaVersion: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "schemaVersion": self.schemaVersion,
            "seq": self.seq,
            "simTime": self.simTime,
            "realTime": self.realTime,
            "category": self.category,
            "agents": list(self.agents),
            "locationId": self.locationId,
            "description": self.description,
        }
        if self.detail is not None:
            d["detail"] = self.detail
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EventLogEntry":
        return cls(
            seq=int(d["seq"]),
            simTime=d["simTime"],
            realTime=d["realTime"],
            category=d["category"],
            agents=list(d.get("agents", [])),
            locationId=d.get("locationId"),
            description=d.get("description", ""),
            detail=d.get("detail"),
            schemaVersion=int(d.get("schemaVersion", SCHEMA_VERSION)),
        )


@dataclass
class ModelPrice:
    per1kInput: float
    per1kOutput: float

    def to_dict(self) -> Dict[str, Any]:
        return {"per1kInput": self.per1kInput, "per1kOutput": self.per1kOutput}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModelPrice":
        return cls(per1kInput=float(d["per1kInput"]), per1kOutput=float(d["per1kOutput"]))


@dataclass
class Budget:
    maxInvocationsPerSimHour: int = 5000
    maxSpendUSD: float = 25.00
    prices: Dict[str, ModelPrice] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "maxInvocationsPerSimHour": self.maxInvocationsPerSimHour,
            "maxSpendUSD": self.maxSpendUSD,
            "prices": {k: v.to_dict() for k, v in self.prices.items()},
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Budget":
        return cls(
            maxInvocationsPerSimHour=int(d.get("maxInvocationsPerSimHour", 5000)),
            maxSpendUSD=float(d.get("maxSpendUSD", 25.00)),
            prices={k: ModelPrice.from_dict(v) for k, v in d.get("prices", {}).items()},
        )


@dataclass
class Config:
    simId: str
    status: SimStatus = SimStatus.STOPPED
    accelerationFactor: int = 4
    startSimTime: str = "2026-03-02T06:00:00+11:00"
    timezone: str = "Australia/Melbourne"
    detentionFacilityId: Optional[str] = None
    artStyleClause: str = ""
    decayRates: Dict[str, float] = field(
        default_factory=lambda: {"hunger": 6.0, "energy": 4.0, "social": 3.0, "fun": 4.0}
    )
    energyRecoveryRate: float = 12.0
    initialNeeds: Dict[str, int] = field(
        default_factory=lambda: {"hunger": 70, "energy": 70, "social": 70, "fun": 70}
    )
    budget: Budget = field(default_factory=Budget)
    # Default population when none is supplied. The DEPLOYED scale is 500
    # (see seed/config.json and infra sizing); this 25 is only the fallback
    # default used when a config omits ``population``. Do not confuse the two.
    population: int = 25
    schemaVersion: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "simId": self.simId,
            "status": self.status.value,
            "accelerationFactor": self.accelerationFactor,
            "startSimTime": self.startSimTime,
            "timezone": self.timezone,
            "detentionFacilityId": self.detentionFacilityId,
            "artStyleClause": self.artStyleClause,
            "decayRates": dict(self.decayRates),
            "energyRecoveryRate": self.energyRecoveryRate,
            "initialNeeds": dict(self.initialNeeds),
            "budget": self.budget.to_dict(),
            "population": self.population,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Config":
        return cls(
            simId=d["simId"],
            status=SimStatus(d.get("status", "stopped")),
            accelerationFactor=int(d.get("accelerationFactor", 4)),
            startSimTime=d.get("startSimTime", "2026-03-02T06:00:00+11:00"),
            timezone=d.get("timezone", "Australia/Melbourne"),
            detentionFacilityId=d.get("detentionFacilityId"),
            artStyleClause=d.get("artStyleClause", ""),
            decayRates={k: float(v) for k, v in d.get(
                "decayRates", {"hunger": 6.0, "energy": 4.0, "social": 3.0, "fun": 4.0}
            ).items()},
            energyRecoveryRate=float(d.get("energyRecoveryRate", 12.0)),
            initialNeeds={k: int(v) for k, v in d.get(
                "initialNeeds", {"hunger": 70, "energy": 70, "social": 70, "fun": 70}
            ).items()},
            budget=Budget.from_dict(d.get("budget", {})),
            population=int(d.get("population", 25)),
        )


__all__ = [
    "SCHEMA_VERSION", "NEED_NAMES", "EVENT_CATEGORIES",
    "LocationCategory", "LocationStatus", "EmploymentStatus", "LegalStatus",
    "ActionType", "CrimeType", "TravelMode", "TargetType", "Provenance",
    "SimStatus", "Outcome", "Detection",
    "OpeningHours", "Location", "Persona", "Action", "AgentState", "Agent",
    "Job", "Relationship", "CrimeEvent", "EventLogEntry",
    "ModelPrice", "Budget", "Config",
]
