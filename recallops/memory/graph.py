"""Memory graph projection.

Nodes: services, incidents, root causes, actions, deployments and the memories
themselves. Edges come from the structured metadata we retain, so the graph is a
projection of real memory content rather than a decorative mock. When Hindsight is
available its own entity graph is merged in (see ``HindsightAdapter.graph``).
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from recallops.domain.enums import MemoryKind
from recallops.memory.models import MemoryItem

_KIND_TO_NODE = {
    MemoryKind.INCIDENT_EPISODE: ("episode", "incident_episode"),
    MemoryKind.ACTION_OUTCOME: ("action", "action_outcome"),
    MemoryKind.RUNBOOK_LESSON: ("runbook", "runbook_lesson"),
    MemoryKind.SERVICE_PATTERN: ("pattern", "service_pattern"),
    MemoryKind.ENGINEER_CORRECTION: ("correction", "engineer_correction"),
    MemoryKind.POSTMORTEM_LESSON: ("postmortem", "postmortem_lesson"),
}


def build_graph(items: Sequence[MemoryItem], *, root_incident_id: str | None = None, source: str = "local_hindsight") -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def node(node_id: str, label: str, node_type: str, **extra: Any) -> str:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "label": label, "type": node_type, "weight": 1, **extra}
        else:
            nodes[node_id]["weight"] += 1
            for k, v in extra.items():
                nodes[node_id].setdefault(k, v)
        return node_id

    def edge(src: str, dst: str, relation: str) -> None:
        key = (src, dst, relation)
        if src in nodes and dst in nodes and key not in seen_edges:
            seen_edges.add(key)
            edges.append({"source": src, "target": dst, "relation": relation})

    for item in items:
        node_type, _ = _KIND_TO_NODE.get(item.kind, ("memory", "memory"))
        mem_id = node(
            f"mem:{item.id}",
            item.title[:70] or item.id,
            node_type,
            kind=item.kind.value,
            score=item.score,
            service=item.service,
            incident_id=item.incident_id,
            outcome=item.outcome,
            durability=item.durability.value,
            source=item.source.value,
        )

        if item.service:
            svc = node(f"svc:{item.service}", item.service, "service")
            edge(svc, mem_id, "has_memory")

        if item.incident_id:
            inc = node(
                f"inc:{item.incident_id}",
                item.incident_id,
                "incident",
                root=(item.incident_id == root_incident_id),
            )
            edge(inc, mem_id, "learned")
            if item.service:
                edge(inc, f"svc:{item.service}", "affects")

        if item.cause_id:
            cause = node(f"cause:{item.cause_id}", item.cause_id.replace("_", " "), "cause")
            edge(cause, mem_id, "explains")
            if item.incident_id:
                edge(f"inc:{item.incident_id}", cause, "root_cause")

        if item.action:
            action_label = item.action[:70]
            act = node(f"act:{item.action_id or item.action}", action_label, "action", outcome=item.outcome)
            edge(act, mem_id, "evidenced_by")
            if item.incident_id:
                edge(f"inc:{item.incident_id}", act, "attempted")
            if item.cause_id:
                edge(act, f"cause:{item.cause_id}", "targeted")

        for ent in item.entities[:6]:
            e = node(f"ent:{ent.lower()}", ent, "entity")
            edge(e, mem_id, "mentions")

    return {
        "supported": True,
        "source": source,
        "root_incident_id": root_incident_id,
        "nodes": sorted(nodes.values(), key=lambda n: (-n["weight"], n["label"])),
        "edges": edges,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "incidents": sum(1 for n in nodes.values() if n["type"] == "incident"),
            "causes": sum(1 for n in nodes.values() if n["type"] == "cause"),
            "actions": sum(1 for n in nodes.values() if n["type"] == "action"),
        },
    }


def graph_from_rows(rows: Iterable[dict[str, Any]], *, source: str = "local_hindsight") -> dict[str, Any]:
    items = [MemoryItem.model_validate(row) for row in rows]
    return build_graph(items, source=source)


__all__ = ["build_graph", "graph_from_rows"]
