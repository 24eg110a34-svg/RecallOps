"""Deterministic incident simulator.

The simulator *is* the demo environment: there is no production monitoring
integration. Given a scenario it holds a world state (current stage + metric
values), reveals evidence stage by stage, and answers two questions exactly:

* what would happen if we ran this action? (``what_if``)
* what actually happened? (``apply_action``)

Both come from the scenario's ``actions.json`` ground truth, so the demo is
repeatable and the comparison numbers are measured, not invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from recallops.agent.evidence import normalize_metric, normalize_stage_evidence
from recallops.agent.scenarios import ActionOutcomeSpec, MetricSpec, Scenario, StageSpec
from recallops.domain.enums import EvidenceKind, Outcome
from recallops.domain.models import ActionResult, EvidenceRef, WhatIfResult
from recallops.memory.models import MemoryItem
from recallops.security import redact_obj

DEFAULT_OUTCOMES: dict[str, dict[str, Any]] = {
    # Diagnostics observe; they never change the world.
    "diagnostic": {
        "outcome": "no_effect",
        "helped": None,
        "resolves": False,
        "detail": "Read-only observation recorded; the incident state is unchanged, which is itself a signal.",
    },
    "neutral_remediation": {
        "outcome": "no_effect",
        "helped": None,
        "resolves": False,
        "detail": "No measurable change in the leading metric within the observation window.",
    },
}


@dataclass
class WorldState:
    stage_id: str
    stage_label: str
    offset_s: float
    metrics: dict[str, float] = field(default_factory=dict)
    resolved: bool = False
    regressed: bool = False
    note: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "stage_label": self.stage_label,
            "offset_s": self.offset_s,
            "metrics": dict(self.metrics),
            "resolved": self.resolved,
            "regressed": self.regressed,
            "note": self.note,
        }


@dataclass
class ActionExecution:
    definition_id: str
    outcome: Outcome
    result: ActionResult
    state: WorldState
    revealed: list[EvidenceRef] = field(default_factory=list)
    metrics: list[MetricSpec] = field(default_factory=list)
    spec: ActionOutcomeSpec | None = None
    resolves: bool = False

    def lesson(self) -> str:
        if self.spec and self.spec.reusable_lesson:
            return self.spec.reusable_lesson
        return self.result.lesson or ""

    def warning(self) -> str:
        return self.spec.warning if self.spec else ""


class ScenarioSimulator:
    """World model for one scenario instance."""

    def __init__(self, scenario: Scenario, *, stage_id: str | None = None, offset_s: float | None = None) -> None:
        self.scenario = scenario
        self.stage_id = stage_id or scenario.first_stage_id
        self.offset_s = float(offset_s if offset_s is not None else self._stage(self.stage_id).offset_s)
        self.metrics: dict[str, float] = {}
        self.resolved = False
        self.regressed = False
        self._seed_metrics()

    # ------------------------------------------------------------------ helpers
    def _stage(self, stage_id: str | None = None) -> StageSpec:
        stage = self.scenario.stage(stage_id or self.stage_id)
        if stage is None:
            raise KeyError(f"unknown stage: {stage_id}")
        return stage

    @property
    def stage(self) -> StageSpec:
        return self._stage()

    def _seed_metrics(self) -> None:
        for metric in self.scenario.initial_metrics():
            self.metrics[metric.name] = metric.value
        for metric in self.stage.metrics:
            self.metrics[metric.name] = metric.value
        if not self.metrics:
            for metric in self.scenario.metrics:
                self.metrics.setdefault(metric.name, metric.value)

    def _gated_stages(self) -> set[str]:
        return {stage.id for stage in self.scenario.stages if stage.gated_by}

    def timestamp(self, offset_s: float | None = None) -> Any:
        """Scenario time (never the wall clock) - this keeps the demo deterministic."""
        return self.scenario.timestamp(self.offset_s if offset_s is None else offset_s)

    def snapshot(self) -> WorldState:
        return WorldState(
            stage_id=self.stage_id,
            stage_label=self.stage.label,
            offset_s=self.offset_s,
            metrics=dict(self.metrics),
            resolved=self.resolved,
            regressed=self.regressed,
            note=self.stage.hint or self.stage.description,
        )

    def available_actions(self) -> list[str]:
        return self.scenario.available_actions(self.stage_id)

    def headline(self) -> dict[str, Any]:
        return {
            "stage": self.stage_id,
            "label": self.stage.label,
            "resolved": self.resolved,
            "offset_s": self.offset_s,
            "metrics": dict(self.metrics),
        }

    # ------------------------------------------------------------------ advance
    def advance(self) -> tuple[WorldState, list[EvidenceRef], list[MetricSpec]]:
        """Reveal the next natural stage. Action-gated stages are not skipped into."""
        gated = self._gated_stages()
        stages = self.scenario.stages
        idx = self.scenario.stage_index(self.stage_id)
        for candidate in stages[idx + 1 :]:
            if candidate.id in gated:
                continue
            if candidate.id == self.scenario.resolved_stage_id:
                # The incident is only resolved by an action; do not let the
                # clock resolve it.
                self.offset_s = float(candidate.offset_s)
                return self.snapshot(), [], []
            self.stage_id = candidate.id
            self.offset_s = float(candidate.offset_s)
            return self.snapshot(), self.reveal_stage(candidate), list(candidate.metrics)
        self.offset_s += 30.0
        return self.snapshot(), [], []

    def reveal_stage(self, stage: StageSpec) -> list[EvidenceRef]:
        incident_id = self.scenario.id
        ts = self.scenario.timestamp(stage.offset_s)
        return [
            normalize_stage_evidence(item, incident_id, stage.id, ts)
            for item in stage.evidence
        ]

    def apply_stage_metrics(self, stage: StageSpec) -> list[MetricSpec]:
        for metric in stage.metrics:
            self.metrics[metric.name] = metric.value
        return list(stage.metrics)

    # ------------------------------------------------------------------ what-if
    def what_if(self, definition_id: str, *, read_only: bool = False) -> WhatIfResult:
        spec = self.scenario.action_outcome(definition_id)
        if read_only:
            return WhatIfResult(
                projection=[],
                predicted_outcome=Outcome.NO_EFFECT,
                predicted_detail="Read-only diagnostic: it observes the incident and changes nothing.",
                will_resolve=False,
                expected_signal="a measurement, not a change",
                confidence=0.99,
                notes=["Safe to run without approval."],
            )
        if spec is None:
            return WhatIfResult(
                projection=[],
                predicted_outcome=Outcome.NO_EFFECT,
                predicted_detail="No simulation configured for this action; it is expected to be neutral.",
                will_resolve=False,
                expected_signal="no change",
                confidence=0.4,
                notes=["Unmodelled action: treat the projection as unknown."],
            )
        projection = [_project_point(p) for p in spec.projection]
        notes: list[str] = []
        if spec.outcome == Outcome.TEMPORARY:
            notes.append(
                f"Expect a short-lived improvement: this simulated action regresses after about {spec.regression_after_s or 0}s."
            )
        if spec.outcome is Outcome.HURT:
            notes.append("This action is predicted to make the incident worse.")
        if spec.resolves:
            notes.append("This action is predicted to resolve the incident.")
        return WhatIfResult(
            projection=projection,
            predicted_outcome=_outcome(spec.outcome),
            predicted_detail=spec.detail,
            will_resolve=spec.resolves,
            expected_signal=projection[-1].get("t", "") if projection else "",
            confidence=0.75 if projection else 0.35,
            notes=notes,
        )

    # ------------------------------------------------------------------ execute
    def apply_action(self, definition_id: str, *, read_only: bool = False) -> ActionExecution:
        spec = self.scenario.action_outcome(definition_id)
        if spec is None:
            spec = self._synthetic_outcome(definition_id, read_only=read_only)

        for point in spec.projection:
            for key, value in point.items():
                if key == "t":
                    continue
                if isinstance(value, (int, float)):
                    self.metrics[key] = float(value)

        self.offset_s = max(self.offset_s, float(spec.projection[-1].get("t_seconds", self.offset_s))) if spec.projection else self.offset_s

        revealed: list[EvidenceRef] = []
        metric_specs: list[MetricSpec] = []
        if spec.next_stage and self.scenario.stage(spec.next_stage) is not None:
            self.stage_id = spec.next_stage
            stage = self.stage
            self.offset_s = max(self.offset_s, float(stage.offset_s))
            revealed = self.reveal_stage(stage)
            metric_specs = self.apply_stage_metrics(stage)
            self.resolved = self.resolved or spec.resolves or stage.id == self.scenario.resolved_stage_id
            self.regressed = self.regressed or stage.id == "regression"
        elif spec.resolves:
            self.resolved = True

        if spec.regression_after_s:
            self.regressed = True

        observed = redact_obj({k: v for k, v in self.metrics.items()})
        result = ActionResult(
            outcome=_outcome(spec.outcome),
            verdict=_verdict(spec),
            detail=spec.detail,
            observed=observed,
            helped=spec.helped,
            lesson=spec.lesson or spec.reusable_lesson or None,
            executed_at=self.scenario.timestamp(self.offset_s),
        )
        return ActionExecution(
            definition_id=definition_id,
            outcome=result.outcome,
            result=result,
            state=self.snapshot(),
            revealed=revealed,
            metrics=metric_specs,
            spec=spec,
            resolves=bool(spec.resolves),
        )

    def _synthetic_outcome(self, definition_id: str, *, read_only: bool) -> ActionOutcomeSpec:
        template = DEFAULT_OUTCOMES["diagnostic" if read_only else "neutral_remediation"]
        return ActionOutcomeSpec(action_id=definition_id, **template)

    # ------------------------------------------------------------------ memory
    def lesson_for(self, definition_id: str) -> MemoryItem | None:
        spec = self.scenario.action_outcome(definition_id)
        if spec is None or not (spec.reusable_lesson or spec.lesson):
            return None
        return MemoryItem(
            id=f"{self.scenario.id}~{definition_id}",
            kind=self._memory_kind(spec),
            title=f"{self.scenario.service}: {definition_id.replace('_', ' ')} "
            f"({'helped' if spec.helped else 'failed' if spec.helped is False else 'inconclusive'})",
            content=spec.detail,
            service=self.scenario.service,
            incident_id=self.scenario.id,
            cause_id=self.scenario.ground_truth.root_cause_id,
            action=definition_id.replace("_", " "),
            action_id=definition_id,
            outcome=spec.outcome,
            helped=spec.helped,
            actual_outcome=spec.detail,
            lesson=spec.lesson,
            reusable_lesson=spec.reusable_lesson,
            tags=[t for t in [*self.scenario.meta.tags, definition_id] if t][:8],
        )

    def _memory_kind(self, spec: ActionOutcomeSpec):
        from recallops.domain.enums import MemoryKind

        if spec.helped is False or spec.outcome in {Outcome.TEMPORARY, Outcome.HURT, Outcome.NO_EFFECT}:
            return MemoryKind.ACTION_OUTCOME
        return MemoryKind.ACTION_OUTCOME


def _outcome(value: str):
    try:
        return Outcome(value)
    except ValueError:
        return Outcome.NO_EFFECT


def _verdict(spec: ActionOutcomeSpec) -> str:
    return {
        "helped": "Worked: the incident condition is gone.",
        "temporary_improvement": "Temporary improvement followed by regression - the cause is untouched.",
        "no_effect": "No measurable effect.",
        "hurt": "Made the incident worse.",
    }.get(spec.outcome, "Outcome recorded.")


def _project_point(point: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in point.items():
        if key == "t":
            out["label"] = value
        elif key == "t_seconds":
            continue
        else:
            out[key] = value
    return out


__all__ = ["ActionExecution", "DEFAULT_OUTCOMES", "ScenarioSimulator", "WorldState"]
