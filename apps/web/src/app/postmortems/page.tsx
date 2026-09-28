"use client";

import { useEffect, useState } from "react";
import { ErrorNote, Panel } from "@/components/ui";
import { api } from "@/lib/api";

const SECTIONS: [string, string[]][] = [
  ["Impact", ["impact"]],
  ["Contributing factors", ["contributing_factors"]],
  ["Prevention", ["prevention"]],
  ["Runbook changes", ["runbook_changes"]],
  ["Lessons learned", ["lessons"]],
  ["Failed actions", ["failed_actions"]],
  ["Successful actions", ["successful_actions"]],
];

export default function PostmortemsPage() {
  const [items, setItems] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    api
      .postmortems()
      .then((r) => {
        setItems(r.postmortems ?? []);
        if (r.postmortems?.length) setOpen(r.postmortems[0].incident_id);
      })
      .catch((e) => setError((e as Error).message));
  }, []);

  const current = items.find((p) => p.incident_id === open) ?? items[0];

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-white">Postmortems</h1>
        <div className="flex gap-1.5">
          {items.map((p) => (
            <button
              key={p.incident_id}
              onClick={() => setOpen(p.incident_id)}
              className={`chip ${
                p.incident_id === open
                  ? "border-signal-cyan/50 bg-signal-cyan/15 text-signal-cyan"
                  : "border-ink-600 bg-ink-800 text-slate-400"
              }`}
            >
              {p.incident_id}
            </button>
          ))}
        </div>
      </div>

      {!current && <Panel title="Postmortem mode"><p className="text-[13px] text-slate-500">No postmortem yet — resolve an incident.</p></Panel>}

      {current && (
        <>
          <Panel
            title={`${current.incident_id} · ${current.title}`}
            right={
              <span className="text-[10px] text-slate-500">
                {current.memory_written ? `retained ${current.memory_written_count} memories` : "memory not retained"}
              </span>
            }
          >
            <p className="text-[13px] text-slate-200">{current.summary}</p>
            <dl className="kv mt-2">
              <dt>root cause</dt>
              <dd className="text-slate-100">{current.root_cause}</dd>
              <dt>resolution</dt>
              <dd>{current.resolution}</dd>
            </dl>
          </Panel>

          <div className="grid gap-4 md:grid-cols-2">
            {SECTIONS.map(([title, keys]) => (
              <Panel key={title} title={title}>
                <ul className="space-y-1 text-[12px] text-slate-300">
                  {keys.flatMap((k) => (Array.isArray(current[k]) ? current[k] : [])).map((value: any, i: number) => (
                    <li key={i}>• {typeof value === "string" ? value : value?.description ?? JSON.stringify(value)}</li>
                  ))}
                </ul>
              </Panel>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
