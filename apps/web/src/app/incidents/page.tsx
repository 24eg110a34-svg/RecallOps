"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ErrorNote, Panel } from "@/components/ui";
import { api } from "@/lib/api";

export default function IncidentsPage() {
  const [incidents, setIncidents] = useState<any[]>([]);
  const [scenarios, setScenarios] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      const [i, s] = await Promise.all([api.incidents(), api.scenarios()]);
      setIncidents(i.incidents);
      setScenarios(s.scenarios);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    load();
  }, []);

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-white">Incidents</h1>
        <Link className="btn-primary" href="/incidents/INC-A1">
          Open command view
        </Link>
      </div>

      <Panel title="Open / resolved incidents">
        <table className="w-full text-left text-[13px]">
          <thead className="text-[11px] uppercase tracking-wider text-slate-500">
            <tr>
              <th className="py-1">ID</th>
              <th>Service</th>
              <th>Sev</th>
              <th>State</th>
              <th>Root cause</th>
              <th>Memory</th>
              <th>Steps</th>
            </tr>
          </thead>
          <tbody>
            {incidents.map((i) => (
              <tr key={i.id} className="border-t border-ink-700/50">
                <td className="py-1.5">
                  <Link className="font-mono text-signal-cyan hover:underline" href={`/incidents/${i.id}`}>
                    {i.id}
                  </Link>
                </td>
                <td>{i.service}</td>
                <td className="text-signal-amber">{i.severity}</td>
                <td>{i.state}</td>
                <td className="text-slate-400">{i.root_cause || "-"}</td>
                <td className="font-mono text-[11px] text-slate-400">{i.memory_mode || "-"}</td>
                <td>{i.step_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="Scenarios">
        <ul className="grid gap-2 md:grid-cols-2">
          {scenarios.map((s) => (
            <li key={s.id} className="rounded-lg bg-ink-850 p-2.5">
              <div className="flex items-center justify-between">
                <span className="font-mono text-[12px] text-slate-200">{s.id}</span>
                <span className="chip border-ink-600 bg-ink-800 text-[10px] text-slate-400">{s.role}</span>
              </div>
              <p className="mt-1 text-[12px] text-slate-400">{s.summary}</p>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
