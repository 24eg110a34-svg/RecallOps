"""Deterministic incident simulator data model + loader.

Everything the demo needs lives in ``scenarios/<ID>/``:

==================  =========================================================
alert.json          the alert that opened the incident
logs.ndjson         log stream, each line tagged with the stage it belongs to
deployments.json    deployment history (with the minutes-before-incident maths)
metrics.json        metric samples present when the incident was detected
dependencies.json   dependency graph + health
stages.json         progressive evidence stages the simulator reveals
actions.json        what happens when each action is executed (the ground truth)
ground_truth.json   confirmed root cause, resolution, lessons, memory seeds
scenario.json       service/severity metadata (optional, sensible defaults)
==================  =========================================================

Timestamps are derived from ``scenario.base_time`` plus an offset, never from the
wall clock, so two runs of the same scenario are byte-identical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from recallops.config import get_settings
from recallops.domain.enums import EvidenceKind

DEFAULT_BASE_TIME = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class ScenarioError(RuntimeError):
    """Raised when scenario data is missing or malformed."""


def _load_json(path: Path) -> Any:
    if not path.exists():
        raise ScenarioError(f"missing scenario file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"invalid JSON in {path}: {exc}") from exc


def _load_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _parse_time(value: str | None) -> datetime:
    if not value:
        return DEFAULT_BASE_TIME
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScenarioError(f"invalid base_time: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- specs


@dataclass
class ScenarioMeta:
    id: str
    title: str
    service: str
    role: str = "other"
    summary: str = ""
    severity_hint: str = "SEV-3"
    service_criticality: str = "standard"
    customer_impact: str = "low"
    symptom: str = ""
    base_time: datetime = DEFAULT_BASE_TIME
    baseline_error_rate_pct: float = 0.5
    tags: list[str] = field(default_factory=list)
    memory_expected: bool = False
    demo_notes: str = ""


@dataclass
class AlertSpec:
    alert_id: str
    title: str
    symptom: str
    monitor: str = ""
    threshold: str = ""
    runbook: str = ""
    fired_at_offset_s: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    notes: str = ""


@dataclass
class LogLine:
    ts_offset_s: int
    level: str
    logger: str
    message: str
    count: int = 1
    stage: str = ""


@dataclass
class DeploymentSpec:
    version: str
    deployed_at_offset_s: int
    author: str = ""
    change_summary: str = ""
    status: str = "healthy"
    risk: str = "low"
    files_changed: int = 0
    tags: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class MetricSpec:
    name: str
    label: str
    value: float
    unit: str = ""
    baseline: float | None = None
    limit: float | None = None
    stage: str = ""
    ts_offset_s: int = 0
    note: str = ""


@dataclass
class DependencySpec:
    name: str
    kind: str = "internal"
    direction: str = "downstream"
    status: str = "healthy"
    criticality: str = "standard"
    latency_p95_ms: float | None = None
    error_rate_pct: float | None = None
    note: str = ""


@dataclass
class StageEvidence:
    id: str
    kind: str
    source: str
    title: str
    detail: str = ""
    ts_offset_s: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageSpec:
    id: str
    label: str
    offset_s: int = 0
    description: str = ""
    evidence: list[StageEvidence] = field(default_factory=list)
    metrics: list[MetricSpec] = field(default_factory=list)
    logs: list[LogLine] = field(default_factory=list)
    unlocks: list[str] = field(default_factory=list)
    hint: str = ""
    gated_by: list[str] = field(default_factory=list)


@dataclass
class ActionOutcomeSpec:
    action_id: str
    outcome: str = "no_effect"
    helped: bool | None = None
    resolves: bool = False
    next_stage: str | None = None
    detail: str = ""
    projection: list[dict[str, Any]] = field(default_factory=list)
    regression_after_s: int | None = None
    lesson: str = ""
    reusable_lesson: str = ""
    warning: str = ""


@dataclass
class GroundTruth:
    root_cause_id: str
    root_cause: str
    root_cause_detail: str = ""
    secondary_cause_id: str = ""
    resolution: str = ""
    resolution_summary: str = ""
    verification: str = ""
    contributing_factors: list[str] = field(default_factory=list)
    prevention: list[str] = field(default_factory=list)
    runbook_changes: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    first_checks: list[str] = field(default_factory=list)
    successful_action_ids: list[str] = field(default_factory=list)
    failed_action_ids: list[str] = field(default_factory=list)
    avoid_action_ids: list[str] = field(default_factory=list)
    resolvable_by: list[str] = field(default_factory=list)
    confirmed_by: str = ""
    memory_episode: str = ""
    memory_service_pattern: str = ""

    @property
    def is_known(self) -> bool:
        return bool(self.root_cause_id)


@dataclass
class Scenario:
    meta: ScenarioMeta
    alert: AlertSpec
    logs: list[LogLine]
    deployments: list[DeploymentSpec]
    metrics: list[MetricSpec]
    dependencies: list[DependencySpec]
    stages: list[StageSpec]
    actions: dict[str, ActionOutcomeSpec]
    ground_truth: GroundTruth
    path: Path | None = None

    # ------------------------------------------------------------------ accessors
    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def service(self) -> str:
        return self.meta.service

    @property
    def base_time(self) -> datetime:
        return self.meta.base_time

    def timestamp(self, offset_s: float) -> datetime:
        return self.base_time + timedelta(seconds=float(offset_s))

    def stage(self, stage_id: str) -> StageSpec | None:
        return next((s for s in self.stages if s.id == stage_id), None)

    def stage_index(self, stage_id: str) -> int:
        for i, s in enumerate(self.stages):
            if s.id == stage_id:
                return i
        return -1

    @property
    def first_stage_id(self) -> str:
        return self.stages[0].id if self.stages else "alert"

    @property
    def resolved_stage_id(self) -> str:
        for s in self.stages:
            if "resolved" in s.id or "recovered" in s.id:
                return s.id
        return self.stages[-1].id if self.stages else self.first_stage_id

    def initial_metrics(self) -> list[MetricSpec]:
        first = self.first_stage_id
        return [m for m in self.metrics if (m.stage or first) == first]

    def all_metrics(self) -> list[MetricSpec]:
        seen: dict[str, MetricSpec] = {}
        for m in self.metrics:
            seen.setdefault(m.name, m)
        for stage in self.stages:
            for m in stage.metrics:
                seen[m.name] = m
        return list(seen.values())

    def metric(self, name: str) -> MetricSpec | None:
        for m in self.all_metrics():
            if m.name == name:
                return m
        return None

    def recent_deployment(self, window_minutes: int = 60) -> DeploymentSpec | None:
        candidates = [d for d in self.deployments if d.deployed_at_offset_s >= -window_minutes * 60]
        if not candidates:
            return None
        return max(candidates, key=lambda d: d.deployed_at_offset_s)

    def deployment_minutes_before(self, deployment: DeploymentSpec) -> float:
        return abs(deployment.deployed_at_offset_s) / 60.0

    def available_actions(self, stage_id: str) -> list[str]:
        stage = self.stage(stage_id)
        return list(stage.unlocks) if stage else []

    def action_outcome(self, action_id: str) -> ActionOutcomeSpec | None:
        return self.actions.get(action_id)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.meta.title,
            "service": self.service,
            "role": self.meta.role,
            "severity_hint": self.meta.severity_hint,
            "symptom": self.meta.symptom,
            "summary": self.meta.summary,
            "tags": self.meta.tags,
            "memory_expected": self.meta.memory_expected,
            "base_time": self.base_time.isoformat(),
            "stages": [s.id for s in self.stages],
            "deployments": [d.version for d in self.deployments],
            "dependencies": [d.name for d in self.dependencies],
            "root_cause_hint": self.ground_truth.root_cause if self.ground_truth.is_known else "unknown",
        }


# --------------------------------------------------------------------------- parsing


def _stage_from_dict(raw: dict[str, Any]) -> StageSpec:
    return StageSpec(
        id=str(raw.get("id", "")),
        label=str(raw.get("label", raw.get("id", ""))),
        offset_s=int(raw.get("offset_s", 0)),
        description=str(raw.get("description", "")),
        evidence=[
            StageEvidence(
                id=str(e.get("id", "")),
                kind=str(e.get("kind", EvidenceKind.SIMULATION.value)),
                source=str(e.get("source", "simulator")),
                title=str(e.get("title", "")),
                detail=str(e.get("detail", "")),
                ts_offset_s=int(e.get("ts_offset_s", 0)),
                raw=dict(e.get("raw", {})),
            )
            for e in raw.get("evidence", []) or []
        ],
        metrics=[
            MetricSpec(
                name=str(m.get("name", "")),
                label=str(m.get("label", m.get("name", ""))),
                value=float(m.get("value", 0) or 0),
                unit=str(m.get("unit", "")),
                baseline=m.get("baseline"),
                limit=m.get("limit"),
                stage=str(raw.get("id", "")),
                ts_offset_s=int(m.get("ts_offset_s", 0)),
                note=str(m.get("note", "")),
            )
            for m in raw.get("metrics", []) or []
        ],
        unlocks=[str(a) for a in raw.get("unlocks", []) or []],
        hint=str(raw.get("hint", "")),
        # A stage is only reachable by executing an action when its evidence says
        # so (``after_action``). Stages that merely *follow* an action (a
        # regression you can also reach by waiting) are not gated.
        gated_by=sorted(
            {
                str(e.get("raw", {}).get("after_action"))
                for e in raw.get("evidence", []) or []
                if e.get("raw", {}).get("after_action")
            }
        ),
    )


def parse_scenario(directory: Path) -> Scenario:
    meta_raw = _load_json(directory / "scenario.json") if (directory / "scenario.json").exists() else {}
    alert_raw = _load_json(directory / "alert.json")

    alert = AlertSpec(
        alert_id=str(alert_raw.get("alert_id", directory.name)),
        title=str(alert_raw.get("title", directory.name)),
        symptom=str(alert_raw.get("symptom", "")),
        monitor=str(alert_raw.get("monitor", "")),
        threshold=str(alert_raw.get("threshold", "")),
        runbook=str(alert_raw.get("runbook", "")),
        fired_at_offset_s=int(alert_raw.get("fired_at_offset_s", 0)),
        labels={str(k): str(v) for k, v in (alert_raw.get("labels") or {}).items()},
        notes=str(alert_raw.get("notes", "")),
    )

    meta = ScenarioMeta(
        id=str(meta_raw.get("id", directory.name)),
        title=str(meta_raw.get("title", alert.title)),
        service=str(meta_raw.get("service", alert.labels.get("service", "unknown-service"))),
        role=str(meta_raw.get("role", "other")),
        summary=str(meta_raw.get("summary", "")),
        severity_hint=str(meta_raw.get("severity_hint", alert.labels.get("severity_hint", "SEV-3"))),
        service_criticality=str(meta_raw.get("service_criticality", "standard")),
        customer_impact=str(meta_raw.get("customer_impact", "low")),
        symptom=str(meta_raw.get("symptom", alert.symptom)),
        base_time=_parse_time(meta_raw.get("base_time")),
        baseline_error_rate_pct=float(
            meta_raw.get("baseline_error_rate_pct", alert_raw.get("baseline_error_rate_pct", 0.5))
        ),
        tags=[str(t) for t in meta_raw.get("tags", []) or []],
        memory_expected=bool(meta_raw.get("memory_expected", False)),
        demo_notes=str(meta_raw.get("demo_notes", "")),
    )

    logs = [
        LogLine(
            ts_offset_s=int(row.get("ts_offset_s", 0)),
            level=str(row.get("level", "INFO")),
            logger=str(row.get("logger", "")),
            message=str(row.get("message", "")),
            count=int(row.get("count", 1)),
            stage=str(row.get("stage", "")),
        )
        for row in _load_ndjson(directory / "logs.ndjson")
    ]

    deployments = [
        DeploymentSpec(
            version=str(d.get("version", "")),
            deployed_at_offset_s=int(d.get("deployed_at_offset_s", 0)),
            author=str(d.get("author", "")),
            change_summary=str(d.get("change_summary", "")),
            status=str(d.get("status", "healthy")),
            risk=str(d.get("risk", "low")),
            files_changed=int(d.get("files_changed", 0)),
            tags=[str(t) for t in d.get("tags", []) or []],
            note=str(d.get("note", "")),
        )
        for d in _load_json(directory / "deployments.json")
    ]

    metrics = [
        MetricSpec(
            name=str(m.get("name", "")),
            label=str(m.get("label", m.get("name", ""))),
            value=float(m.get("value", 0) or 0),
            unit=str(m.get("unit", "")),
            baseline=m.get("baseline"),
            limit=m.get("limit"),
            stage=str(m.get("stage", "")),
            ts_offset_s=int(m.get("ts_offset_s", 0)),
            note=str(m.get("note", "")),
        )
        for m in _load_json(directory / "metrics.json")
    ]

    dependencies = [
        DependencySpec(
            name=str(d.get("name", "")),
            kind=str(d.get("kind", "internal")),
            direction=str(d.get("direction", "downstream")),
            status=str(d.get("status", "healthy")),
            criticality=str(d.get("criticality", "standard")),
            latency_p95_ms=d.get("latency_p95_ms"),
            error_rate_pct=d.get("error_rate_pct"),
            note=str(d.get("note", "")),
        )
        for d in _load_json(directory / "dependencies.json")
    ]

    stages_raw = _load_json(directory / "stages.json")
    stages = [_stage_from_dict(s) for s in stages_raw]
    if stages:
        first_id = stages[0].id
        for stage in stages:
            stage.logs = [log for log in logs if (log.stage or first_id) == stage.id]
        if not any(stage.logs for stage in stages):
            stages[0].logs = list(logs)

    actions_raw = _load_json(directory / "actions.json") if (directory / "actions.json").exists() else {}
    actions = {
        str(aid): ActionOutcomeSpec(
            action_id=str(aid),
            outcome=str(spec.get("outcome", "no_effect")),
            helped=spec.get("helped"),
            resolves=bool(spec.get("resolves", False)),
            next_stage=spec.get("next_stage"),
            detail=str(spec.get("detail", "")),
            projection=list(spec.get("projection", []) or []),
            regression_after_s=spec.get("regression_after_s"),
            lesson=str(spec.get("lesson", "")),
            reusable_lesson=str(spec.get("reusable_lesson", "")),
            warning=str(spec.get("warning", "")),
        )
        for aid, spec in actions_raw.items()
    }

    gt_raw = _load_json(directory / "ground_truth.json")
    ground_truth = GroundTruth(
        root_cause_id=str(gt_raw.get("root_cause_id", "")),
        root_cause=str(gt_raw.get("root_cause", "")),
        root_cause_detail=str(gt_raw.get("root_cause_detail", "")),
        secondary_cause_id=str(gt_raw.get("secondary_cause_id", "")),
        resolution=str(gt_raw.get("resolution", "")),
        resolution_summary=str(gt_raw.get("resolution_summary", "")),
        verification=str(gt_raw.get("verification", "")),
        contributing_factors=[str(x) for x in gt_raw.get("contributing_factors", []) or []],
        prevention=[str(x) for x in gt_raw.get("prevention", []) or []],
        runbook_changes=[str(x) for x in gt_raw.get("runbook_changes", []) or []],
        lessons=[str(x) for x in gt_raw.get("lessons", []) or []],
        first_checks=[str(x) for x in gt_raw.get("first_checks", []) or []],
        successful_action_ids=[str(x) for x in gt_raw.get("successful_action_ids", []) or []],
        failed_action_ids=[str(x) for x in gt_raw.get("failed_action_ids", []) or []],
        avoid_action_ids=[str(x) for x in gt_raw.get("avoid_action_ids", []) or []],
        resolvable_by=[str(x) for x in gt_raw.get("resolvable_by", []) or []],
        confirmed_by=str(gt_raw.get("confirmed_by", "")),
        memory_episode=str(gt_raw.get("memory_episode", "")),
        memory_service_pattern=str(gt_raw.get("memory_service_pattern", "")),
    )

    return Scenario(
        meta=meta,
        alert=alert,
        logs=logs,
        deployments=deployments,
        metrics=metrics,
        dependencies=dependencies,
        stages=stages,
        actions=actions,
        ground_truth=ground_truth,
        path=directory,
    )


class ScenarioLibrary:
    """All scenarios on disk, indexed by id."""

    def __init__(self, scenarios: list[Scenario], root: Path) -> None:
        self._scenarios = {s.id: s for s in scenarios}
        self.root = root

    def __iter__(self) -> Iterator[Scenario]:
        return iter(self._scenarios.values())

    def __len__(self) -> int:
        return len(self._scenarios)

    def get(self, scenario_id: str) -> Scenario:
        scenario = self._scenarios.get(scenario_id)
        if scenario is None:
            raise ScenarioError(
                f"unknown scenario '{scenario_id}'; available: {', '.join(sorted(self._scenarios))}"
            )
        return scenario

    def has(self, scenario_id: str) -> bool:
        return scenario_id in self._scenarios

    def ids(self) -> list[str]:
        return list(self._scenarios)

    def catalog(self) -> list[dict[str, Any]]:
        return [s.summary() for s in self._scenarios.values()]

    def by_service(self, service: str) -> list[Scenario]:
        return [s for s in self._scenarios.values() if s.service == service]


def load_library(root: Path | None = None) -> ScenarioLibrary:
    base = root or get_settings().scenarios_path
    if not base.exists():
        raise ScenarioError(f"scenarios directory not found: {base}")
    scenarios: list[Scenario] = []
    for directory in sorted(p for p in base.iterdir() if p.is_dir()):
        if not (directory / "alert.json").exists():
            continue
        scenarios.append(parse_scenario(directory))
    if not scenarios:
        raise ScenarioError(f"no scenarios found under {base}")
    return ScenarioLibrary(scenarios, base)


@lru_cache(maxsize=1)
def get_library() -> ScenarioLibrary:
    return load_library()


def clear_library_cache() -> None:
    get_library.cache_clear()


__all__ = [
    "ActionOutcomeSpec",
    "AlertSpec",
    "DependencySpec",
    "DeploymentSpec",
    "GroundTruth",
    "LogLine",
    "MetricSpec",
    "Scenario",
    "ScenarioError",
    "ScenarioLibrary",
    "ScenarioMeta",
    "StageEvidence",
    "StageSpec",
    "clear_library_cache",
    "get_library",
    "load_library",
    "parse_scenario",
]
