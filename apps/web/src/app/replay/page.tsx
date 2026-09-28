"use client";

import { useEffect, useState } from "react";
import { ErrorNote, Panel } from "@/components/ui";
import { api } from "@/lib/api";

const PHASE_ORDER = [
  "incident",
  "evidence",
  "memory_recall",
  "hypothesis",
  "recommendation",
  "approval",
  "action",
  "outcome",
  "resolution",
  "memory",
];

export default function ReplayPage() {
  const [incidents, setIncidents] = useState<any[]>([]);
  const [selected, setSelected] = useState("INC-A1");
  const [replay, setReplay] = useState<Record<string, any> | null>(null);
  const [step, setStep] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.incidents().then((r) => setIncidents(r.incidents)).catch((e) => setError((e as Error).message));
  }, []);

  useEffect(() => {
    api.replay(selected).then(setReplay).catch(() => setReplay(null));
    setStep(0);
  }, [selected]);

  const events = (replay?.timeline ?? []) as any[];
  const shown = events.slice(0, step);

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold text-white">Incident replay</h1>
          <p className="text-[13px] text-slate-400">Step through the investigation exactly as it happened.</p>
        </div>
        <select
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
          className="rounded-lg border border-ink-600 bg-ink-900 px-3 py-1.5 text-[13px] text-slate-200"
        >
          {incidents.map((i) => (
            <option key={i.id} value={i.id}>
              {i.id} · {i.service}
            </option>
          ))}
        </select>
      </div>

      <Panel
        title={`Timeline (${step}/${events.length})`}
        right={
          <div className="flex gap-2">
            <button className="btn-ghost !px-2 !py-1 text-[12px]" onClick={() => setStep(Math.max(0, step - 1))}>
              ◀ back
            </button>
            <button className="btn-primary !px-2 !py-1 text-[12px]" onClick={() => setStep(Math.min(events.length, step + 1))}>
              next ▶
            </button>
            <button className="btn-ghost !px-2 !py-1 text-[12px]" onClick={() => setStep(events.length)}>
              all
            </button>
          </div>
        }
      >
        {events.length === 0 && <p className="text-[13px] text-slate-500">No timeline recorded yet.</p>}
        <ol className="space-y-1.5">
          {shown.map((e) => (
            <li
              key={e.id}
              className={`rounded-lg px-2.5 py-1.5 ${
                PHASE_ORDER.includes(e.phase) ? "bg-ink-850" : "bg-ink-900"
              } border-l-2 ${
                e.phase === "outcome"
                  ? "border-signal-amber"
                  : e.phase === "resolution" || e.phase === "memory"
                  ? "border-signal-green"
                  : e.phase === "memory_recall"
                  ? "border-signal-violet"
                  : "border-signal-cyan"
              }`}
            >
              <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-slate-500">
                <span>{e.seq}</span>
                <span className="text-signal-cyan">{e.phase}</span>
                <span>{new Date(e.ts).toISOString().slice(11, 19)}</span>
                <span>{e.actor}</span>
              </div>
              <div className="text-[13px] text-slate-100">{e.title}</div>
              {e.detail && <p className="text-[11px] text-slate-400">{e.detail}</p>}
            </li>
          ))}
        </ol>
      </Panel>

      {replay && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Panel title="Actions in this incident">
            <ul className="space-y-1.5">
              {(replay.actions ?? []).map((a: any) => (
                <li key={a.id} className="rounded-lg bg-ink-850 px-2.5 py-1.5 text-[12px]">
                  <div className="flex items-center justify-between">
                    <span className="text-slate-200">{a.description}</span>
                    <span className="chip border-ink-600 bg-ink-800 text-[10px] text-slate-400">{a.status}</span>
                  </div>
                  {a.outcome && <p className="text-[11px] text-slate-500">outcome: {a.outcome.outcome} — {a.outcome.detail}</p>}
                  {a.lesson && <p className="text-[11px] text-signal-amber">{a.lesson}</p>}
                </li>
              ))}
            </ul>
          </Panel>
          <Panel title="Run summary">
            <dl className="kv">
              {Object.entries(replay.summary ?? {}).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt>{k.replace(/_/g, " ")}</dt>
                  <dd>{String(v)}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-2 text-[12px] text-slate-400">root cause: {replay.root_cause || "unconfirmed"}</p>
            <p className="text-[12px] text-slate-400">memory mode: {replay.memory_mode || "n/a"}</p>
          </Panel>
        </div>
      )}
    </div>
  );
}
