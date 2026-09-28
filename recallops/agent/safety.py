"""Safety gate.

The LLM never touches infrastructure. It proposes; this module decides:

* ``READ_ONLY``  - may run automatically in the simulator
* ``REVERSIBLE`` - must be explicitly approved by a human
* ``HIGH_RISK``  - advisory only: shown with warnings, refused by the executor

The gate is deliberately independent of the agent that calls it, so a bug in the
planner cannot accidentally promote a destructive action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from recallops.agent.catalog import ActionDef
from recallops.domain.enums import IncidentState, RiskLevel, read_only_allowed


class ExecutionDecision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    REFUSE = "refuse"


class SafetyViolation(RuntimeError):
    """Raised when something tries to execute an action the gate refuses."""


@dataclass
class SafetyVerdict:
    decision: ExecutionDecision
    risk: RiskLevel
    requires_confirmation: bool
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    auto_executable: bool = False
    authorized: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "risk": self.risk.value,
            "requires_confirmation": self.requires_confirmation,
            "auto_executable": self.auto_executable,
            "authorized": self.authorized,
            "warnings": self.warnings,
            "notes": self.notes,
            "blockers": self.blockers,
        }


HIGH_RISK_WARNING = (
    "HIGH RISK: this action is irreversible or can lose data in flight. RecallOps will not execute it. "
    "It is shown for awareness only - use the documented runbook with a human incident commander."
)


class SafetyGate:
    """Stateless policy object; the app holds one instance."""

    allow_high_risk_execution: bool = False

    def __init__(self, *, allow_high_risk_execution: bool = False) -> None:
        self.allow_high_risk_execution = allow_high_risk_execution

    # ------------------------------------------------------------------ classify
    def evaluate_action(self, action: ActionDef, *, state: IncidentState | str = IncidentState.INVESTIGATING) -> SafetyVerdict:
        risk = action.risk
        notes: list[str] = []
        warnings: list[str] = []
        blockers: list[str] = []

        if risk is RiskLevel.READ_ONLY:
            if not read_only_allowed(state):
                blockers.append(f"incident is {state}: read-only diagnostics are paused outside an active investigation")
            return SafetyVerdict(
                decision=ExecutionDecision.REFUSE if blockers else ExecutionDecision.ALLOW,
                risk=risk,
                requires_confirmation=False,
                auto_executable=not blockers,
                notes=notes + ["Read-only diagnostic: safe to run automatically in the simulator."],
                blockers=blockers,
            )

        if risk is RiskLevel.REVERSIBLE:
            notes.append("State-changing but reversible: requires an explicit human approval.")
            if action.production_impact:
                warnings.append("Affects production traffic.")
            return SafetyVerdict(
                decision=ExecutionDecision.REQUIRE_APPROVAL,
                risk=risk,
                requires_confirmation=True,
                auto_executable=False,
                warnings=warnings,
                notes=notes,
            )

        # HIGH_RISK
        warnings.append(HIGH_RISK_WARNING)
        if action.data_loss_risk:
            warnings.append("Can lose in-flight data or transactions.")
        if not action.reversible:
            warnings.append("Not reversible: there is no undo.")
        if action.production_impact:
            warnings.append("Affects production traffic.")
        blockers.append("high_risk_action_is_advisory_only")
        return SafetyVerdict(
            decision=ExecutionDecision.REFUSE,
            risk=risk,
            requires_confirmation=True,
            auto_executable=False,
            warnings=warnings,
            notes=notes + ["Approval is recorded for the audit trail but execution stays disabled."],
            blockers=blockers,
        )

    # ------------------------------------------------------------------ execution
    def authorize_execution(
        self,
        action: ActionDef,
        *,
        state: IncidentState | str = IncidentState.INVESTIGATING,
        approved: bool = False,
        approved_by: str = "",
    ) -> SafetyVerdict:
        """Gate used immediately before the simulator executes an action."""
        verdict = self.evaluate_action(action, state=state)
        if verdict.decision is ExecutionDecision.ALLOW:
            verdict.authorized = True
            return verdict
        if verdict.decision is ExecutionDecision.REQUIRE_APPROVAL:
            if not approved:
                verdict.blockers.append("awaiting_human_approval")
                return verdict
            if not approved_by:
                verdict.blockers.append("approval_must_name_a_human")
                return verdict
            verdict.notes.append(f"Approved by {approved_by}.")
            verdict.authorized = True
            return verdict
        if verdict.risk is RiskLevel.HIGH_RISK and self.allow_high_risk_execution and approved and approved_by:
            verdict.blockers = [b for b in verdict.blockers if b != "high_risk_action_is_advisory_only"]
            verdict.notes.append("High-risk execution explicitly unlocked by operator configuration.")
            verdict.authorized = True
            return verdict
        return verdict

    def assert_executable(self, verdict: SafetyVerdict, action_id: str) -> None:
        if not verdict.authorized:
            raise SafetyViolation(
                f"action '{action_id}' refused by the safety gate: {'; '.join(verdict.blockers) or 'policy'}",
            )


__all__ = ["ExecutionDecision", "HIGH_RISK_WARNING", "SafetyGate", "SafetyViolation", "SafetyVerdict"]
