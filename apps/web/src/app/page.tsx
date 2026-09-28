"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ErrorNote, Panel, Stat } from "@/components/ui";
import { api, type Scenario } from "@/lib/api";

export default function DashboardPage() {
  const [health, setHealth] = useState<Record<string, any> | null>(null);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [analytics, setAnalytics] = useState<Record<string, any> | null>(null);
  const [incidents, setIncidents] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = async () => {
    try {
      const [h, s, a, i] = await Promise.all([
        api.health(),
        api.scenarios(),
        api.analytics(),
        api.incidents(),
      ]);
      setHealth(h);
      setScenarios(s.scenarios);
      setAnalytics(a);
      setIncidents(i.incidents);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const totals = analytics?.totals ?? {};

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">Incident command centre</h1>
          <p className="text-[13px] text-slate-400">
            Incident → evidence → memory recall → hypotheses → approval-gated action → postmortem → learning
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn-primary" disabled={busy !== null} onClick={() => act("demo", () => api.runDemo())}>
            {busy === "demo" ? "Running full demo…" : "▶ Run full demo (A1 → A2 → comparison)"}
          </button>
          <button className="btn-ghost" disabled={busy !== null} onClick={() => act("reset", () => api.resetDemo())}>
            Reset demo
          </button>
          <Link className="btn-ghost" href="/incidents/INC-A1">
            Open INC-A1
          </Link>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Stat label="Incidents" value={totals.incidents ?? 0} />
        <Stat label="Resolved" value={totals.resolved ?? 0} tone="text-signal-green" />
        <Stat label="Open" value={totals.open ?? 0} tone="text-signal-amber" />
        <Stat label="Memories" value={totals.memories ?? 0} tone="text-signal-cyan" />
        <Stat label="Failed actions" value={totals.failed_actions ?? 0} tone="text-signal-red" />
        <Stat label="Failed actions avoided" value={totals.failed_actions_avoided ?? 0} tone="text-signal-green" />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Provider health">
          {health ? (
            <div className="space-y-2">
              {Object.entries(health.components as Record<string, any>).map(([name, info]) => (
                <div key={name} className="flex items-center justify-between rounded-lg bg-ink-850 px-3 py-2">
                  <span className="font-mono text-[12px] uppercase text-slate-400">{name}</span>
                  <span className="flex items-center gap-2 text-[12px] text-slate-300">
                    <span
                      className={`h-2 w-2 rounded-full ${
                        info.state === "connected"
                          ? "bg-signal-green"
                          : info.state === "degraded"
                          ? "bg-signal-amber"
                          : "bg-signal-red"
                      }`}
                    />
                    {info.detail}
                  </span>
                </div>
              ))}
              <div className="pt-1 text-[11px] text-slate-500">
                memory mode: <span className="font-mono">{health.components.memory.mode}</span> · llm:{" "}
                <span className="font-mono">{health.config.llm.provider}</span> ({health.config.llm.model})
              </div>
            </div>
          ) : (
            <p className="text-[13px] text-slate-500">Loading…</p>
          )}
        </Panel>

        <Panel title="Seed scenarios (deterministic simulator)">
          <ul className="space-y-2">
            {scenarios.map((s) => (
              <li key={s.id} className="rounded-lg bg-ink-850 px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[12px] text-slate-200">{s.id}</span>
                    <span className="chip border-ink-600 bg-ink-800 text-slate-400">{s.role}</span>
                    <span className="chip border-signal-amber/40 bg-signal-amber/10 text-signal-amber">{s.severity_hint}</span>
                  </div>
                  <div className="flex gap-1">
                    <button
                      className="btn-ghost !px-2 !py-1 text-[12px]"
                      disabled={busy !== null}
                      onClick={() => act(s.id, () => api.createIncident(s.id))}
                    >
                      create
                    </button>
                    <button
                      className="btn-ghost !px-2 !py-1 text-[12px]"
                      disabled={busy !== null}
                      onClick={() => act(s.id, () => api.advanceScenario(s.id, 1))}
                    >
                      advance
                    </button>
                  </div>
                </div>
                <p className="mt-1 text-[12px] text-slate-400">{s.summary}</p>
              </li>
            ))}
          </ul>
        </Panel>
      </div>

      <Panel title="Live incidents">
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
            {incidents.length === 0 && (
              <tr>
                <td colSpan={7} className="py-3 text-slate-500">
                  No incidents yet — press “Run full demo” or create a scenario.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}
