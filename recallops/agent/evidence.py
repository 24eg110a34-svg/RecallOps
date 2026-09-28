"""Evidence collection and normalization.

All incident input is *untrusted*: log lines, alert bodies and dependency notes
come from the outside world. Everything is redacted, injection-hardened and
given stable ids so a hypothesis can cite ``[LOG-7]`` and the UI can link it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Iterable, Sequence

from recallops.agent.scenarios import (
    AlertSpec,
    DependencySpec,
    DeploymentSpec,
    LogLine,
    MetricSpec,
    Scenario,
)
from recallops.domain.enums import EvidenceKind
from recallops.domain.models import EvidenceRef, utcnow
from recallops.security import contains_injection, harden_untrusted, redact_obj, redact_text

_SLUG = re.compile(r"[^A-Za-z0-9]+")

_SIGNAL_HINTS: dict[str, tuple[str, ...]] = {
    "redis": ("redis_pool_saturation", "cache_timeout_burst"),
    "redistimeout": ("cache_timeout_burst",),
    "cache acquisition": ("cache_timeout_burst", "redis_pool_saturation"),
    "pool": ("redis_pool_saturation", "db_connection_saturation"),
    "postgres": ("db_connection_saturation",),
    "too many clients": ("db_connection_saturation",),
    "slow query": ("slow_query_regression",),
    "seq scan": ("slow_query_regression",),
    "n+1": ("n_plus_one_cpu",),
    "per-item": ("n_plus_one_cpu",),
    "queries per request": ("n_plus_one_cpu",),
    "cpu": ("n_plus_one_cpu", "memory_pressure"),
    "timeout": ("third_party_timeout", "network_connectivity"),
    "upstream timeout": ("third_party_timeout",),
    "provider": ("third_party_timeout",),
    "external_api": ("third_party_timeout",),
    "dns": ("network_connectivity",),
    "connection refused": ("network_connectivity",),
    "oom": ("memory_pressure",),
    "out of memory": ("memory_pressure",),
    "leak": ("connection_leak_after_deploy",),
    "not released": ("connection_leak_after_deploy",),
    "deployed": ("connection_leak_after_deploy", "n_plus_one_cpu"),
    "rollback": ("connection_leak_after_deploy",),
    "rps surge": ("traffic_saturation",),
}


def evidence_id(prefix: str, *parts: str) -> str:
    """Stable, collision-proof evidence id (content is part of the key)."""
    raw = "|".join(str(p) for p in parts if p)
    slug = _SLUG.sub("-", raw).strip("-").upper()[:40]
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
    return f"{prefix}-{slug}-{digest}" if slug else f"{prefix}-{digest}"


def hint_signals(text: str) -> list[str]:
    lowered = text.lower()
    hints: list[str] = []
    for needle, signals in _SIGNAL_HINTS.items():
        if needle in lowered:
            hints.extend(signals)
    seen: list[str] = []
    for h in hints:
        if h not in seen:
            seen.append(h)
    return seen[:6]


def _clean(text: str, max_len: int = 2000) -> tuple[str, bool]:
    raw = str(text or "")
    redacted = raw != redact_text(raw)
    return harden_untrusted(raw, max_len=max_len), redacted


# --------------------------------------------------------------------------- normalizers


def normalize_alert(alert: AlertSpec, incident_id: str, ts: datetime) -> EvidenceRef:
    detail_parts = [alert.symptom]
    if alert.monitor:
        detail_parts.append(f"Monitor: {alert.monitor}.")
    if alert.threshold:
        detail_parts.append(f"Threshold: {alert.threshold}.")
    if alert.runbook:
        detail_parts.append(f"Runbook: {alert.runbook}.")
    if alert.notes:
        detail_parts.append(alert.notes)
    detail, redacted = _clean(" ".join(p for p in detail_parts if p))
    title, _ = _clean(alert.title, 200)
    return EvidenceRef(
        id=evidence_id("ALERT", incident_id, alert.alert_id),
        kind=EvidenceKind.ALERT,
        source="datadog-monitor",
        title=title,
        detail=detail,
        ts=ts,
        raw=redact_obj(
            {
                "alert_id": alert.alert_id,
                "monitor": alert.monitor,
                "threshold": alert.threshold,
                "fired_at_offset_s": alert.fired_at_offset_s,
                "labels": alert.labels,
            }
        ),
        signal_hints=hint_signals(f"{alert.title} {alert.symptom}"),
        untrusted=True,
        redacted=redacted,
    )


def normalize_log(log: LogLine, incident_id: str, stage: str, ts: datetime) -> EvidenceRef:
    message, redacted = _clean(log.message, 600)
    injected = contains_injection(log.message)
    level = log.level.upper()
    return EvidenceRef(
        id=evidence_id("LOG", incident_id, f"{log.logger}-{log.ts_offset_s}", f"{message[:32]}"),
        kind=EvidenceKind.LOG,
        source=log.logger or "app",
        title=f"[{level}] {log.logger}: {message[:140]}",
        detail=f"count={log.count} stage={stage}" + (" [prompt-injection marker neutralized]" if injected else ""),
        ts=ts,
        stage=stage,
        signal_hints=hint_signals(log.message),
        raw=redact_obj({"level": level, "logger": log.logger, "count": log.count, "message": message, "stage": stage}),
        untrusted=True,
        redacted=redacted,
    )


def normalize_metric(metric: MetricSpec, incident_id: str, stage: str, ts: datetime) -> EvidenceRef:
    detail_parts = [f"value={metric.value}{_unit(metric.unit)}"]
    if metric.baseline is not None:
        detail_parts.append(f"baseline={metric.baseline}")
    if metric.limit is not None:
        detail_parts.append(f"limit={metric.limit}")
    if metric.note:
        detail_parts.append(metric.note)
    raw = {
        "name": metric.name,
        "label": metric.label,
        "value": metric.value,
        "unit": metric.unit,
        "baseline": metric.baseline,
        "limit": metric.limit,
        "stage": stage,
    }
    if metric.note:
        raw["note"] = metric.note
    saturated = metric.limit is not None and metric.limit > 0 and metric.value >= metric.limit * 0.95
    if saturated:
        raw["saturated"] = True
    if raw.get("saturated"):
        raw["diagnostic"] = True
    return EvidenceRef(
        id=evidence_id("METRIC", incident_id, metric.name),
        kind=EvidenceKind.METRIC,
        source="metrics",
        title=f"{metric.label} = {metric.value}{_unit(metric.unit)}",
        detail=" ".join(detail_parts),
        ts=ts,
        stage=stage,
        signal_hints=hint_signals(f"{metric.name} {metric.label} {metric.note}"),
        raw=redact_obj(raw),
        untrusted=False,
    )


def normalize_deployment(dep: DeploymentSpec, incident_id: str, minutes_before: float, ts: datetime, correlation: float) -> EvidenceRef:
    detail_parts = [dep.change_summary]
    if dep.author:
        detail_parts.append(f"by {dep.author}")
    if dep.files_changed:
        detail_parts.append(f"{dep.files_changed} files changed")
    detail_parts.append(f"deployed {minutes_before:.0f} minutes before the incident")
    detail_parts.append(f"risk={dep.risk}, status={dep.status}")
    if dep.note:
        detail_parts.append(dep.note)
    detail, redacted = _clean("; ".join(p for p in detail_parts if p))
    return EvidenceRef(
        id=evidence_id("DEPLOY", incident_id, dep.version),
        kind=EvidenceKind.DEPLOYMENT,
        source="deploy-pipeline",
        title=f"{incident_id.split('-')[-1]} {dep.version} deployed {minutes_before:.0f} min before the incident",
        detail=detail,
        ts=ts,
        raw=redact_obj(
            {
                "service": incident_id,
                "version": dep.version,
                "minutes_before": minutes_before,
                "author": dep.author,
                "risk": dep.risk,
                "status": dep.status,
                "correlation": correlation,
                "change_summary": dep.change_summary,
                "tags": dep.tags,
            }
        ),
        signal_hints=hint_signals(dep.change_summary),
        untrusted=True,
        redacted=redacted,
    )


def normalize_dependency(dep: DependencySpec, incident_id: str, ts: datetime) -> EvidenceRef:
    unhealthy = dep.status.lower() not in {"healthy", "ok", "up"}
    detail_parts = [f"status={dep.status}", f"kind={dep.kind}", f"criticality={dep.criticality}"]
    if dep.latency_p95_ms is not None:
        detail_parts.append(f"p95={dep.latency_p95_ms}ms")
    if dep.error_rate_pct is not None:
        detail_parts.append(f"error_rate={dep.error_rate_pct}%")
    if dep.note:
        detail_parts.append(dep.note)
    raw = {
        "service": dep.name,
        "status": dep.status,
        "kind": dep.kind,
        "direction": dep.direction,
        "criticality": dep.criticality,
        "latency_p95_ms": dep.latency_p95_ms,
        "error_rate_pct": dep.error_rate_pct,
    }
    if unhealthy:
        raw["diagnostic"] = True
    return EvidenceRef(
        id=evidence_id("DEP", incident_id, dep.name),
        kind=EvidenceKind.DEPENDENCY if dep.name != incident_id else EvidenceKind.SERVICE_HEALTH,
        source="service-catalog",
        title=f"{dep.name} is {dep.status} ({dep.kind}, {dep.direction})",
        detail=" ".join(detail_parts),
        ts=ts,
        raw=redact_obj(raw),
        signal_hints=hint_signals(f"{dep.name} {dep.status} {dep.note}"),
        untrusted=False,
    )


def normalize_stage_evidence(item: Any, incident_id: str, stage: str, ts: datetime) -> EvidenceRef:
    try:
        kind = EvidenceKind(item.kind)
    except ValueError:
        kind = EvidenceKind.SIMULATION
    title, _ = _clean(item.title, 200)
    detail, redacted = _clean(item.detail)
    raw = dict(item.raw or {})
    raw.setdefault("stage", stage)
    if raw.get("diagnostic"):
        raw["diagnostic"] = True
    # Scenario evidence ids are scenario-scoped ("A2-POOL"), so they must be
    # namespaced per incident: two incidents of the same scenario must not collide
    # on the primary key, and neither may silently skip the other's evidence.
    base_id = str(item.id or evidence_id(kind.value.upper()[:4], incident_id, stage, title[:24]))
    namespaced = base_id if base_id.startswith(incident_id) else f"{incident_id}:{base_id}"
    return EvidenceRef(
        id=namespaced,
        kind=kind,
        source=item.source,
        title=title,
        detail=detail,
        ts=ts,
        stage=stage,
        signal_hints=hint_signals(f"{title} {detail}"),
        raw=redact_obj(raw),
        untrusted=kind in {EvidenceKind.LOG, EvidenceKind.ALERT, EvidenceKind.DEPLOYMENT, EvidenceKind.OUTCOME, EvidenceKind.SIMULATION},
        redacted=redacted,
    )


def _unit(unit: str) -> str:
    return f" {unit}" if unit else ""


# --------------------------------------------------------------------------- helpers


def dedupe_evidence(items: Iterable[EvidenceRef]) -> list[EvidenceRef]:
    """Stable de-duplication by evidence id (EvidenceRef is unhashable)."""
    seen: set[str] = set()
    out: list[EvidenceRef] = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        out.append(item)
    return out


def evidence_by_id(items: Sequence[EvidenceRef]) -> dict[str, EvidenceRef]:
    return {e.id: e for e in items}


def find_evidence(items: Iterable[EvidenceRef], evidence_id_value: str) -> EvidenceRef | None:
    for e in items:
        if e.id == evidence_id_value:
            return e
    return None


def headline_metrics(items: Iterable[EvidenceRef], limit: int = 12) -> list[EvidenceRef]:
    return [e for e in items if e.kind in {EvidenceKind.METRIC, EvidenceKind.ALERT}][:limit]


def render_evidence_block(items: Sequence[EvidenceRef], *, limit: int = 40) -> str:
    lines: list[str] = []
    for e in items[:limit]:
        ts = e.ts.isoformat() if e.ts else "n/a"
        lines.append(f"[{e.id}] ({e.kind}, {ts}) {e.title}\n    {e.detail}".rstrip())
    return "\n".join(lines)


def evidence_counts(items: Iterable[EvidenceRef]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in items:
        counts[e.kind.value] = counts.get(e.kind.value, 0) + 1
    return counts


__all__ = [
    "evidence_by_id",
    "evidence_counts",
    "evidence_id",
    "find_evidence",
    "dedupe_evidence",
    "headline_metrics",
    "hint_signals",
    "normalize_alert",
    "normalize_deployment",
    "normalize_dependency",
    "normalize_log",
    "normalize_metric",
    "normalize_stage_evidence",
    "render_evidence_block",
    "utcnow",
]
