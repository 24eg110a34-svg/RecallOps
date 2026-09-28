"use client";

import { useEffect, useMemo, useState } from "react";
import { ErrorNote, Panel, Stat } from "@/components/ui";
import { api } from "@/lib/api";

type Node = { id: string; label: string; type: string; weight: number; incident_id?: string };
type Edge = { source: string; target: string; relation: string };

const COLORS: Record<string, string> = {
  service: "#38bdf8",
  incident: "#ff4d6a",
  cause: "#a78bfa",
  action: "#ffb020",
  episode: "#2ee6a8",
  runbook: "#2ee6a8",
  pattern: "#a78bfa",
  postmortem: "#38bdf8",
  correction: "#ffb020",
  entity: "#64748b",
};

export default function GraphPage() {
  const [graph, setGraph] = useState<{ nodes: Node[]; edges: Edge[]; stats?: Record<string, number> }>({ nodes: [], edges: [] });
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    api
      .memoryGraph()
      .then((g) => setGraph(g as any))
      .catch((e) => setError((e as Error).message));
  }, []);

  const layout = useMemo(() => {
    const width = 900;
    const height = 560;
    const byType: Record<string, Node[]> = {};
    graph.nodes.forEach((n) => {
      (byType[n.type] ||= []).push(n);
    });
    const positions: Record<string, { x: number; y: number }> = {};
    const types = Object.keys(byType);
    types.forEach((type, ti) => {
      const nodes = byType[type];
      const cx = 120 + (ti * width) / Math.max(1, types.length);
      nodes.forEach((n, ni) => {
        const cy = 70 + ((ni + 1) * height) / (nodes.length + 1);
        positions[n.id] = { x: cx, y: cy };
      });
    });
    return positions;
  }, [graph]);

  const neighbours = useMemo(() => {
    if (!selected) return new Set<string>();
    const set = new Set<string>([selected]);
    graph.edges.forEach((e) => {
      if (e.source === selected) set.add(e.target);
      if (e.target === selected) set.add(e.source);
    });
    return set;
  }, [selected, graph]);

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div>
        <h1 className="text-lg font-semibold text-white">Memory graph</h1>
        <p className="text-[13px] text-slate-400">
          Services, incidents, root causes, actions and the memories that connect them — projected from what was actually retained.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="Nodes" value={graph.nodes.length} />
        <Stat label="Edges" value={graph.edges.length} />
        <Stat label="Incidents" value={graph.stats?.incidents ?? 0} tone="text-signal-red" />
        <Stat label="Causes" value={graph.stats?.causes ?? 0} tone="text-signal-violet" />
        <Stat label="Actions" value={graph.stats?.actions ?? 0} tone="text-signal-amber" />
      </div>

      <Panel title="Graph">
        {graph.nodes.length === 0 ? (
          <p className="text-[13px] text-slate-500">No memory relationships yet — resolve an incident first.</p>
        ) : (
          <div className="overflow-x-auto">
            <svg viewBox="0 0 900 620" className="h-[560px] w-full min-w-[900px]">
              {graph.edges.map((e, i) => {
                const a = layout[e.source];
                const b = layout[e.target];
                if (!a || !b) return null;
                const active = selected && (e.source === selected || e.target === selected);
                return (
                  <line
                    key={`${e.source}-${e.target}-${i}`}
                    x1={a.x}
                    y1={a.y}
                    x2={b.x}
                    y2={b.y}
                    stroke={active ? "#38bdf8" : "#243049"}
                    strokeWidth={active ? 1.6 : 1}
                  />
                );
              })}
              {graph.nodes.map((n) => {
                const pos = layout[n.id];
                if (!pos) return null;
                const dim = selected && !neighbours.has(n.id);
                return (
                  <g
                    key={n.id}
                    transform={`translate(${pos.x},${pos.y})`}
                    onClick={() => setSelected(selected === n.id ? null : n.id)}
                    style={{ cursor: "pointer" }}
                  >
                    <circle
                      r={selected === n.id ? 9 : 6}
                      fill={COLORS[n.type] ?? "#64748b"}
                      opacity={dim ? 0.25 : 0.95}
                    />
                    <text
                      y={-11}
                      textAnchor="middle"
                      className="fill-slate-300 text-[9px]"
                      opacity={dim ? 0.3 : 1}
                    >
                      {n.label.length > 26 ? `${n.label.slice(0, 26)}…` : n.label}
                    </text>
                  </g>
                );
              })}
            </svg>
          </div>
        )}
      </Panel>

      <Panel title="Nodes">
        <div className="flex flex-wrap gap-1.5">
          {graph.nodes.map((n) => (
            <button
              key={n.id}
              onClick={() => setSelected(n.id)}
              className="chip border-ink-600 bg-ink-800 text-slate-300 hover:border-signal-cyan/50"
            >
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: COLORS[n.type] ?? "#64748b" }} />
              {n.type}: {n.label}
            </button>
          ))}
        </div>
      </Panel>
    </div>
  );
}
