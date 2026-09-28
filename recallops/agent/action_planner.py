"""Action planner.

Turns the leading hypothesis into a concrete, risk-rated next step, and - the
part that makes the demo land - refuses to recommend an action that
organisational memory says already failed here.

Memory can only *lower* an action's ranking or block it. It can never invent an
action, and it can never mark a destructive action as safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from recallops.agent.catalog import ALL_ACTIONS, ActionDef, actions_for_cause, get_action
from recallops.agent.safety import SafetyGate
from recallops.domain.enums import ActionStatus, IncidentState, MemoryKind, RiskLevel
from recallops.domain.models import ActionSpec, CauseHypothesis, Recommendation
from recallops.domain.signals import CAUSES, cause_name, keyword_overlap
from recallops.memory.models import MemoryItem

# action_id layout: "<incident>~<nn>~<definition id>"  ('~' is URL-safe, ids are stable)
ACTION_ID_SEP = "~"


def make_action_id(incident_id: str, step_index: int, definition_id: str) -> str:
    return f"{incident_id}{ACTION_ID_SEP}{step_index:02d}{ACTION_ID_SEP}{definition_id}"


def definition_id_from_spec(action_id: str) -> str:
    parts = action_id.split(ACTION_ID_SEP)
    return parts[-1] if len(parts) >= 3 else parts[-1]


@dataclass
class PlanOutcome:
    recommendation: Recommendation | None = None
    alternatives: list[ActionSpec] = field(default_factory=list)
    blocked: list[ActionSpec] = field(default_factory=list)
    memory_warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def action_spec(
    definition: ActionDef,
    *,
    incident_id: str,
    step_index: int,
    hypothesis: CauseHypothesis | None = None,
    status: ActionStatus = ActionStatus.PROPOSED,
    blocked_reason: str | None = None,
    memory_warnings: Sequence[str] = (),
) -> ActionSpec:
    return ActionSpec(
        id=make_action_id(incident_id, step_index, definition.id),
        incident_id=incident_id,
        description=definition.description,
        type=definition.type,
        risk=definition.risk,
        reason=definition.reason,
        expected_signal=definition.expected_signal,
        requires_confirmation=definition.requires_confirmation,
        status=status,
        tool=definition.tool,
        params=dict(definition.params),
        reversible=definition.reversible,
        production_impact=definition.production_impact,
        data_loss_risk=definition.data_loss_risk,
        blocked_reason=blocked_reason,
        hypothesis_id=hypothesis.id if hypothesis else None,
        step_index=step_index,
        memory_warnings=list(memory_warnings),
        safety_notes=[definition.notes] if definition.notes else [],
    )


def failed_actions_from_memory(memories: Sequence[MemoryItem]) -> list[MemoryItem]:
    """Memories recording an action that failed, regressed or was harmful."""
    out: list[MemoryItem] = []
    for m in memories:
        if m.kind is MemoryKind.ACTION_OUTCOME and (m.helped is False or m.outcome in {"temporary_improvement", "hurt", "no_effect"}):
            out.append(m)
    return out


def action_matches_memory(definition: ActionDef, memory: MemoryItem) -> float:
    """How strongly a recalled failed action refers to this definition."""
    if memory.action_id:
        return 1.0 if memory.action_id == definition.id else 0.0
    text = " ".join(
        filter(None, [memory.action, memory.reusable_lesson, memory.lesson, memory.actual_outcome, memory.content])
    )
    if not text:
        return 0.0
    return keyword_overlap(text, [definition.id.replace("_", " "), definition.description, *definition.tags])


class ActionPlanner:
    def __init__(self, safety: SafetyGate | None = None) -> None:
        self.safety = safety or SafetyGate()

    # ------------------------------------------------------------------ plan
    def plan(
        self,
        *,
        incident_id: str,
        hypotheses: Sequence[CauseHypothesis],
        memories: Sequence[MemoryItem],
        available_action_ids: Sequence[str] | None = None,
        executed_action_ids: Sequence[str] = (),
        state: IncidentState | str = IncidentState.INVESTIGATING,
        step_index: int = 1,
    ) -> PlanOutcome:
        outcome = PlanOutcome()
        top = hypotheses[0] if hypotheses else None
        available = set(available_action_ids or [])
        already = set(executed_action_ids)

        candidates = self._candidates(hypotheses, available)
        failed_memories = failed_actions_from_memory(memories)
        specs: list[ActionSpec] = []
        blocked: list[ActionSpec] = []
        next_index = step_index

        for definition in candidates:
            if definition.id in already:
                continue
            spec = action_spec(definition, incident_id=incident_id, step_index=next_index, hypothesis=top)
            # Only state-changing actions can be blocked by a remembered failure.
            # A read-only diagnostic is cheap and safe, so memory may only add a note.
            match = 0.0
            if not definition.read_only:
                match = max((action_matches_memory(definition, m) for m in failed_memories), default=0.0)
            if match >= 0.5:
                matching = next(m for m in failed_memories if action_matches_memory(definition, m) >= 0.5)
                warning = (
                    matching.reusable_lesson
                    or matching.lesson
                    or f"{matching.incident_id or 'a previous incident'}: this action produced only temporary improvement and regression."
                )
                spec.status = ActionStatus.BLOCKED_BY_MEMORY
                spec.blocked_reason = (
                    "Organisational memory records this action failing in a similar incident "
                    f"({matching.incident_id or 'previous incident'}). Not recommended as remediation."
                )
                spec.memory_warnings = [warning]
                blocked.append(spec)
                outcome.memory_warnings.append(warning)
                continue
            if definition.read_only and failed_memories:
                # Read-only checks stay available; the learned lesson is attached as context.
                spec.memory_warnings = [
                    f"Previous similar incident attempted a similar check: {warning}"
                    for warning in outcome.memory_warnings[:1]
                ]
            specs.append(spec)
            next_index += 1

        if not specs and blocked:
            diag = next((d for d in ALL_ACTIONS if d.read_only and d.id not in already), None)
            if diag is not None:
                fallback = action_spec(diag, incident_id=incident_id, step_index=next_index, hypothesis=top)
                fallback.memory_warnings = outcome.memory_warnings[:2]
                specs.append(fallback)
                outcome.notes.append(
                    "Every candidate remediation is blocked by failed-action memory; recommending a diagnostic instead."
                )

        if not specs:
            outcome.notes.append("No actionable step available from the current evidence and memory.")
            return outcome

        for spec in specs:
            self._apply_safety(spec, state=state)
        primary = specs[0]
        if primary.status is ActionStatus.PROPOSED:
            primary.status = ActionStatus.AWAITING_APPROVAL
        if primary.risk is not RiskLevel.READ_ONLY and primary.requires_confirmation:
            primary.status = ActionStatus.AWAITING_APPROVAL

        warnings = list(outcome.memory_warnings)
        if primary.risk is RiskLevel.HIGH_RISK:
            warnings.append("Advisory only: RecallOps never executes a high-risk action.")
        outcome.recommendation = Recommendation(
            action=primary,
            why=self._why(primary, top),
            expected_signal=primary.expected_signal,
            risk=primary.risk,
            requires_approval=primary.requires_confirmation,
            warnings=warnings,
            alternatives=specs[1:4],
            blocked_by_memory=bool(blocked),
        )
        outcome.alternatives = specs[1:4]
        outcome.blocked = blocked
        return outcome

    # ------------------------------------------------------------------ helpers
    def _candidates(self, hypotheses: Sequence[CauseHypothesis], available: set[str]) -> list[ActionDef]:
        """Diagnostics that match the leading causes, then their remediations."""
        diagnostics: list[ActionDef] = []
        remediations: list[ActionDef] = []
        seen: set[str] = set()

        def push(bucket: list[ActionDef], defn: ActionDef | None) -> None:
            if defn and defn.id not in seen and (not available or defn.id in available):
                seen.add(defn.id)
                bucket.append(defn)

        for hypothesis in hypotheses[:2]:
            cause_id = hypothesis.cause_id
            cause = CAUSES.get(cause_id)
            if cause:
                for defn in ALL_ACTIONS:
                    if defn.read_only and keyword_overlap(f"{defn.description} {defn.expected_signal}", cause.diagnostics) > 0.2:
                        push(diagnostics, defn)
            for defn in actions_for_cause(cause_id):
                push(diagnostics if defn.read_only else remediations, defn)

        for defn in (get_action("inspect_error_rate"), get_action("inspect_recent_deploy")):
            push(diagnostics, defn)

        return diagnostics + remediations

    def _apply_safety(self, spec: ActionSpec, *, state: IncidentState | str) -> None:
        definition = get_action(definition_id_from_spec(spec.id))
        if definition is None:
            return
        verdict = self.safety.evaluate_action(definition, state=state)
        spec.risk = verdict.risk
        spec.requires_confirmation = verdict.requires_confirmation
        spec.safety_notes = list(dict.fromkeys([*spec.safety_notes, *verdict.notes, *verdict.warnings]))

    def _why(self, spec: ActionSpec, top: CauseHypothesis | None) -> str:
        if top is None:
            return f"{spec.reason} No dominant signal yet, so start with the cheapest broad check."
        base = f"Leading hypothesis: {cause_name(top.cause_id).lower()} ({top.confidence:.0%}). {spec.reason}"
        if spec.blocked_reason:
            base = f"{base} {spec.blocked_reason}"
        return base


__all__ = [
    "ACTION_ID_SEP",
    "ActionPlanner",
    "PlanOutcome",
    "action_matches_memory",
    "action_spec",
    "definition_id_from_spec",
    "failed_actions_from_memory",
    "make_action_id",
]
