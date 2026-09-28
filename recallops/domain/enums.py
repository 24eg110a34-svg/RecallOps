"""Domain enumerations and the incident state machine."""

from __future__ import annotations

from enum import StrEnum


class IncidentState(StrEnum):
    NEW = "NEW"
    TRIAGING = "TRIAGING"
    INVESTIGATING = "INVESTIGATING"
    MITIGATED = "MITIGATED"
    MONITORING = "MONITORING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    LEARNED = "LEARNED"

    @property
    def is_active(self) -> bool:
        return self not in {IncidentState.RESOLVED, IncidentState.CLOSED, IncidentState.LEARNED}

    @property
    def is_terminal(self) -> bool:
        return self in {IncidentState.CLOSED, IncidentState.LEARNED}


# Investigation loops are explicitly allowed: TRIAGING <-> INVESTIGATING and
# INVESTIGATING <-> MITIGATED may repeat any number of times.
_ALLOWED: dict[IncidentState, set[IncidentState]] = {
    IncidentState.NEW: {IncidentState.TRIAGING},
    IncidentState.TRIAGING: {IncidentState.INVESTIGATING, IncidentState.CLOSED},
    IncidentState.INVESTIGATING: {
        IncidentState.TRIAGING,
        IncidentState.MITIGATED,
        IncidentState.CLOSED,
    },
    IncidentState.MITIGATED: {
        IncidentState.INVESTIGATING,
        IncidentState.MONITORING,
        IncidentState.CLOSED,
    },
    IncidentState.MONITORING: {
        IncidentState.INVESTIGATING,
        IncidentState.RESOLVED,
        IncidentState.CLOSED,
    },
    IncidentState.RESOLVED: {IncidentState.LEARNED, IncidentState.CLOSED, IncidentState.INVESTIGATING},
    IncidentState.CLOSED: set(),
    IncidentState.LEARNED: {IncidentState.CLOSED},
}

# Actions are available only in these states.
_READ_ONLY_STATES = {IncidentState.TRIAGING, IncidentState.INVESTIGATING, IncidentState.MITIGATED, IncidentState.MONITORING}


class StateTransitionError(ValueError):
    """Raised when code attempts an illegal incident state transition."""


def can_transition(current: IncidentState | str, target: IncidentState | str) -> bool:
    cur = IncidentState(current)
    tgt = IncidentState(target)
    return tgt in _ALLOWED.get(cur, set())


def assert_transition(current: IncidentState | str, target: IncidentState | str) -> IncidentState:
    cur, tgt = IncidentState(current), IncidentState(target)
    if not can_transition(cur, tgt):
        raise StateTransitionError(
            f"illegal incident transition {cur} -> {tgt}; allowed: {sorted(s.value for s in _ALLOWED.get(cur, set())) or 'none'}"
        )
    return tgt


def next_states(current: IncidentState | str) -> list[str]:
    return sorted(s.value for s in _ALLOWED.get(IncidentState(current), set()))


def read_only_allowed(current: IncidentState | str) -> bool:
    return IncidentState(current) in _READ_ONLY_STATES


class Severity(StrEnum):
    SEV1 = "SEV-1"
    SEV2 = "SEV-2"
    SEV3 = "SEV-3"
    SEV4 = "SEV-4"

    @property
    def rank(self) -> int:
        return int(self.value.split("-")[1])


class EvidenceKind(StrEnum):
    ALERT = "alert"
    LOG = "log"
    METRIC = "metric"
    DEPLOYMENT = "deployment"
    DEPENDENCY = "dependency"
    SERVICE_HEALTH = "service_health"
    SIMULATION = "simulation"
    MEMORY = "memory"
    ENGINEER = "engineer"
    OUTCOME = "outcome"


class RiskLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    REVERSIBLE = "REVERSIBLE"
    HIGH_RISK = "HIGH_RISK"

    @property
    def is_state_changing(self) -> bool:
        return self is not RiskLevel.READ_ONLY


class ActionType(StrEnum):
    DIAGNOSTIC = "diagnostic"
    REMEDIATION = "remediation"
    MITIGATION = "mitigation"
    SCALING = "scaling"
    CONFIGURATION = "configuration"
    DEPENDENCY = "dependency"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED_BY_MEMORY = "blocked_by_memory"
    SIMULATED = "simulated"
    EXECUTED = "executed"
    FAILED = "failed"
    REVERTED = "reverted"


class Outcome(StrEnum):
    HELPED = "helped"
    NO_EFFECT = "no_effect"
    TEMPORARY = "temporary_improvement"
    HURT = "hurt"


class MemoryKind(StrEnum):
    INCIDENT_EPISODE = "incident_episode"
    ACTION_OUTCOME = "action_outcome"
    RUNBOOK_LESSON = "runbook_lesson"
    SERVICE_PATTERN = "service_pattern"
    ENGINEER_CORRECTION = "engineer_correction"
    POSTMORTEM_LESSON = "postmortem_lesson"


class Durability(StrEnum):
    DURABLE = "durable"
    EPISODIC = "episodic"
    REJECTED = "rejected"


class MemoryMode(StrEnum):
    """Which layer actually answered a recall. Never fake Hindsight."""

    HINDSIGHT = "hindsight"          # real Hindsight server / cloud
    LOCAL_HINDSIGHT = "local_hindsight"  # self-hosted-compatible local memory store
    DEMO_FALLBACK = "demo_fallback"  # deterministic in-process store, demo only
    DISABLED = "disabled"            # memory OFF (comparison mode)


class TimelinePhase(StrEnum):
    INCIDENT = "incident"
    EVIDENCE = "evidence"
    MEMORY_RECALL = "memory_recall"
    HYPOTHESIS = "hypothesis"
    RECOMMENDATION = "recommendation"
    APPROVAL = "approval"
    ACTION = "action"
    OUTCOME = "outcome"
    RESOLUTION = "resolution"
    MEMORY = "memory"
    SYSTEM = "system"
