"use client";

import { useEffect, useState } from "react";
import { ErrorNote, Panel, Stat } from "@/components/ui";
import { api } from "@/lib/api";

export default function MemoryPage() {
  const [items, setItems] = useState<any[]>([]);
  const [mode, setMode] = useState<string>("");
  const [label, setLabel] = useState<string>("");
  const [quality, setQuality] = useState<Record<string, any> | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      const [m, q, h] = await Promise.all([api.memory(), api.memoryQuality(), api.memoryHealth()]);
      setItems(m.items ?? []);
      setMode(m.mode);
      setLabel(h.mode_label);
      setQuality(q);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const search = async () => {
    if (query.trim().length < 2) return;
    try {
      const r = await api.memorySearch(query);
      setResults(r.items ?? []);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-white">Organisational memory</h1>
          <p className="text-[13px] text-slate-400">
            Active mode: <span className="font-mono">{mode}</span> — {label}
          </p>
        </div>
        <div className="flex gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && search()}
            placeholder="search memory (semantic + keyword + graph + recency)"
            className="w-72 rounded-lg border border-ink-600 bg-ink-900 px-3 py-1.5 text-[13px] text-slate-200 outline-none focus:border-signal-cyan/60"
          />
          <button className="btn-ghost" onClick={search}>
            Search
          </button>
        </div>
      </div>

      {results.length > 0 && (
        <Panel title={`Search results for “${query}”`}>
          <ul className="space-y-1.5">
            {results.map((r) => (
              <li key={r.id} className="rounded-lg bg-ink-850 px-2.5 py-1.5">
                <div className="flex items-center justify-between text-[11px] text-slate-500">
                  <span className="uppercase tracking-wider">{r.kind?.replace(/_/g, " ")}</span>
                  <span className="font-mono">{(r.score ?? 0).toFixed(3)}</span>
                </div>
                <p className="text-[12px] text-slate-200">{r.text ?? r.content}</p>
                <p className="text-[11px] text-slate-500">
                  {r.incident_id} · {(r.strategy_hits ?? []).join(", ") || "n/a"}
                </p>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="Memories" value={items.length} />
        <Stat label="Durable" value={quality?.durable_ratio ?? 0} tone="text-signal-green" />
        <Stat label="With lesson" value={quality?.with_reusable_lesson ?? 0} tone="text-signal-cyan" />
        <Stat label="Failed actions" value={quality?.failed_action_memories ?? 0} tone="text-signal-red" />
        <Stat label="Sources" value={Object.keys(quality?.sources ?? {}).length} />
      </div>

      <Panel title="Retained memories">
        <div className="space-y-2">
          {items.map((m) => (
            <div key={m.id} className="rounded-lg bg-ink-850 p-2.5">
              <div className="flex flex-wrap items-center gap-2 text-[11px]">
                <span className="chip border-ink-600 bg-ink-800 uppercase text-slate-400">{String(m.kind).replace(/_/g, " ")}</span>
                <span className="font-mono text-slate-500">{m.id}</span>
                {m.incident_id && <span className="text-slate-600">from {m.incident_id}</span>}
                {m.helped === false && <span className="chip border-signal-red/50 bg-signal-red/15 text-signal-red">failed action</span>}
                {m.helped === true && <span className="chip border-signal-green/50 bg-signal-green/15 text-signal-green">worked</span>}
                <span className="ml-auto text-slate-600">{m.source}</span>
              </div>
              <div className="mt-1 text-[13px] font-semibold text-slate-100">{m.title}</div>
              <p className="text-[12px] text-slate-400">{m.content}</p>
              {m.reusable_lesson && (
                <p className="mt-1 rounded border border-signal-amber/30 bg-signal-amber/10 px-2 py-1 text-[11px] text-signal-amber">
                  reusable lesson: {m.reusable_lesson}
                </p>
              )}
            </div>
          ))}
          {items.length === 0 && (
            <p className="text-[13px] text-slate-500">
              No memories yet — resolve an incident (postmortem → retain) to build organisational memory.
            </p>
          )}
        </div>
      </Panel>
    </div>
  );
}
