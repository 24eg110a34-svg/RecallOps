"""Postmortem and runbook generation.

Both are built from the *investigation record* (evidence, hypotheses, actions,
outcomes, timeline) plus the organisation's own written lessons. The record is
the source of truth for facts; the scenario's ground-truth text supplies the
narrative lessons an organisation would write after the incident.
"""

from __future__ import annotations

from typing import Any, Sequence

from recallops.agent.record import IncidentRecord
from recallops.domain.models import (
    ActionSpec,
    PostmortemReport,
    Runbook,
    RunbookStep,
    TimelineEvent,
)
from recallops.domain.signals import CAUSES, cause_name


def _fmt_minutes(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value < 1:
        return f"{value * 60:.0f}s"
    return f"{value:.0f}m"


def build_timeline(record: IncidentRecord) -> list[TimelineEvent]:
    return record.timeline()


def build_postmortem(
    record: IncidentRecord,
    *,
    root_cause_override: str | None = None,
    resolution_override: str | None = None,
) -> PostmortemReport:
    gt = record.scenario.ground_truth if record.scenario else None
    confirmed = record.confirmed_hypothesis or record.top_hypothesis

    root_cause = root_cause_override or record.root_cause or (confirmed.cause if confirmed else "Unconfirmed")
    if gt and not root_cause_override and record.root_cause_id == gt.root_cause_id:
        root_cause = gt.root_cause

    resolution = resolution_override or record.resolution
    if not resolution and gt:
        resolution = gt.resolution

    failed = record.failed_actions()
    successful = record.successful_actions()
    impact_text = _impact_text(record)
    duration = _fmt_minutes(record.resolution_minutes())

    summary = (
        f"{record.severity} incident on {record.service} ({record.id}). {record.symptom} "
        f"Root cause: {root_cause}. Resolved in {duration} after {record.step_count} investigation step(s)"
        + (f", with {len(failed)} failed action attempt(s) and {len(successful)} successful one(s)." if failed or successful else ".")
    )

    return PostmortemReport(
        incident_id=record.id,
        title=record.title,
        severity=record.severity,  # type: ignore[arg-type]
        service=record.service,
        summary=summary,
        impact=impact_text,
        timeline=build_timeline(record),
        root_cause=root_cause,
        contributing_factors=list(gt.contributing_factors) if gt else _fallback_factors(record),
        evidence=[e for e in record.evidence if e.kind.value in {"alert", "metric", "deployment", "outcome", "log"}][:14],
        actions_taken=record.executed_actions(),
        failed_actions=failed,
        successful_actions=successful,
        resolution=resolution or "Not recorded",
        prevention=list(gt.prevention) if gt else _fallback_prevention(record),
        runbook_changes=list(gt.runbook_changes) if gt else [],
        lessons=list(gt.lessons) if gt else _fallback_lessons(record),
        generated_by="recallops-agent",
    )


def _impact_text(record: IncidentRecord) -> str:
    if record.impact is not None and record.impact.error_rate_pct:
        imp = record.impact
        base = (
            f"{imp.error_rate_pct:.1f}% of requests failing (baseline "
            f"{imp.baseline_error_rate_pct if imp.baseline_error_rate_pct is not None else 'n/a'}%), "
            f"customer impact {imp.customer_impact}, service criticality {imp.service_criticality}"
        )
        if imp.affected_endpoints:
            base += f", affected endpoints: {', '.join(imp.affected_endpoints[:5])}"
        if imp.estimated_affected_requests:
            base += f", roughly {imp.estimated_affected_requests} requests affected"
        return base
    metric = record.metric_value("http_5xx_pct")
    return f"Up to {metric:.1f}% of requests failing at peak." if metric is not None else "Impact not quantified."


def _fallback_factors(record: IncidentRecord) -> list[str]:
    factors: list[str] = []
    for dep in record.deployments:
        minutes = dep.get("minutes_before_incident")
        if minutes is not None and minutes <= 60:
            factors.append(f"{dep.get('version')} deployed {minutes:.0f} min before onset ({dep.get('change_summary', '')[:120]})")
    for dep in record.dependencies:
        if dep.get("status") not in {"healthy", None}:
            factors.append(f"Dependency {dep.get('name')} was {dep.get('status')}")
    if record.blocked_actions:
        factors.append(f"{len(record.blocked_actions)} candidate action(s) were blocked by organisational memory")
    return factors or ["No contributing factors recorded"]


def _fallback_prevention(record: IncidentRecord) -> list[str]:
    return [
        f"Add a regression test for the failure mode confirmed in {record.id}",
        f"Add an alert on the metric that identified this incident ({record.metric_value('http_5xx_pct') is not None and 'error rate' or 'latency'})",
    ]


def _fallback_lessons(record: IncidentRecord) -> list[str]:
    lessons: list[str] = []
    for action in record.failed_actions():
        lesson = (action.result.lesson if action.result else "") or ""
        if lesson:
            lessons.append(lesson)
    if record.confirmed_hypothesis:
        lessons.append(
            f"Confirmed cause was {record.confirmed_hypothesis.cause.lower()}; the decisive evidence was "
            + (record.confirmed_hypothesis.supporting[0].title if record.confirmed_hypothesis.supporting else "the first diagnostic")
        )
    return lessons or ["No lessons recorded - add them in the postmortem review."]


# --------------------------------------------------------------------------- runbook


def build_runbook(record: IncidentRecord, *, postmortem: PostmortemReport | None = None) -> Runbook:
    gt = record.scenario.ground_truth if record.scenario else None
    cause = CAUSES.get(record.root_cause_id)
    title = f"{record.service} - {cause_name(record.root_cause_id) if cause else record.root_cause or 'incident'}"
    symptoms = _symptoms(record)
    first_checks = list(gt.first_checks) if gt and gt.first_checks else _default_first_checks(record.root_cause_id)
    diagnostics = list(cause.diagnostics) if cause else []
    known_failed = _known_failed(record)
    recommended = _recommended(record, cause.remediations if cause else ())
    verification = [gt.confirmed_by] if gt and gt.confirmed_by else ["Leading metric returns to baseline and stays there for 5 minutes"]
    rollback = ["Roll back the most recent release of the service and watch the leading metric for 3 minutes"]
    prevention = list(gt.prevention) if gt else []

    steps: list[RunbookStep] = []
    order = 1
    for check in first_checks:
        steps.append(RunbookStep(order=order, step=check, kind="check", detail="Do this first - cheapest high-information check."))
        order += 1
    for diag in diagnostics:
        steps.append(RunbookStep(order=order, step=diag, kind="diagnose"))
        order += 1
    for bad in known_failed:
        steps.append(RunbookStep(order=order, step=f"AVOID: {bad}", kind="avoid", detail="Recorded as failed in a previous incident."))
        order += 1
    for good in recommended:
        steps.append(RunbookStep(order=order, step=good, kind="act"))
        order += 1
    for check in verification:
        steps.append(RunbookStep(order=order, step=check, kind="verify"))
        order += 1

    return Runbook(
        id=f"rb-{record.service}-{record.root_cause_id or 'generic'}".replace(" ", "-").lower()[:96],
        title=title,
        service=record.service,
        symptoms=symptoms,
        first_checks=first_checks,
        diagnostics=diagnostics,
        known_failed_actions=known_failed,
        recommended_actions=recommended,
        verification=verification,
        rollback=rollback,
        prevention=prevention,
        steps=steps,
        source_incidents=[record.id],
        generated_at=record.resolved_at or record.detected_at,
    )


def _symptoms(record: IncidentRecord) -> list[str]:
    symptoms: list[str] = []
    for ev in record.evidence:
        if ev.kind.value == "log" and ev.raw.get("count", 0) and ev.raw.get("count", 0) > 20:
            symptoms.append(ev.title[:160])
        if len(symptoms) >= 6:
            break
    for name, label in (("http_5xx_pct", "elevated 5xx"), ("redis_connections_active", "connection utilisation at maximum"), ("cpu_utilization_pct", "CPU saturation")):
        value = record.metric_value(name)
        if value is not None and name != "http_5xx_pct":
            symptoms.append(f"{label} ({value})")
    if record.symptom:
        symptoms.insert(0, record.symptom[:200])
    return _dedupe(symptoms)[:8]


def _default_first_checks(cause_id: str) -> list[str]:
    cause = CAUSES.get(cause_id)
    if cause:
        return list(cause.diagnostics[:3])
    return ["Inspect the leading metric and the deploy history", "Inspect the dependency graph"]


def _known_failed(record: IncidentRecord) -> list[str]:
    out: list[str] = []
    for action in record.failed_actions():
        if action.result and action.result.lesson:
            out.append(f"{action.description}: {action.result.lesson}")
        else:
            out.append(f"{action.description} (no measurable effect in {record.id})")
    for action in record.blocked_actions():
        if action.blocked_reason:
            out.append(f"{action.description}: blocked by memory - {action.blocked_reason}")
    return _dedupe(out)[:8]


def _recommended(record: IncidentRecord, remediations: Sequence[str]) -> list[str]:
    out: list[str] = []
    for action in record.successful_actions():
        out.append(f"{action.description} (worked in {record.id})")
    for r in remediations:
        label = r.replace("_", " ")
        if not any(label.split()[0] in existing.lower() for existing in out):
            out.append(f"Consider: {label}")
    return _dedupe(out)[:8]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        key = v.strip().lower()[:120]
        if key and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def runbook_payload(runbook: Runbook) -> dict[str, Any]:
    return runbook.model_dump(mode="json")


__all__ = ["build_postmortem", "build_runbook", "build_timeline", "runbook_payload"]
