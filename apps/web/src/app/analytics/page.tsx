"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ErrorNote, Panel, Stat } from "@/components/ui";
import { api } from "@/lib/api";

type Row = { label: string; off: any; on: any; better: "up" | "down" | "none" };

const ROWS: Row[] = [
  { label: "Memories recalled", off: "memories_recalled", on: "memories_recalled", better: "up" },
  { label: "Similar incident surfaced", off: "similar_incident_found", on: "similar_incident_found", better: "up" },
  { label: "Failed-action warning", off: "failed_action_warning", on: "failed_action_warning", better: "up" },
  { label: "Repeated the known-bad action", off: "repeated_failed_action", on: "repeated_failed_action", better: "down" },
  { label: "Top hypothesis", off: "top_hypothesis", on: "top_hypothesis", better: "none" },
  { label: "Top hypothesis confidence", off: "top_hypothesis_confidence", on: "top_hypothesis_confidence", better: "up" },
  { label: "Actions executed", off: "actions_executed", on: "actions_executed", better: "down" },
  { label: "Diagnostic steps", off: "diagnostic_steps", on: "diagnostic_steps", better: "down" },
  { label: "Failed remediations", off: "failed_action_count", on: "failed_action_count", better: "down" },
  { label: "First recommended action", off: "recommended_first_action", on: "recommended_first_action", better: "none" },
  { label: "Resolution", off: "resolution", on: "resolution", better: "none" },
];

function fmt(value: any) {
  if (value === true) return "yes";
  if (value === false) return "no";
  if (typeof value === "number") return value.toFixed(2);
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

/** Memory OFF vs ON: the board that proves organisational memory matters. */
export default function AnalyticsPage() {
  const [analytics, setAnalytics] = useState<Record<string, any> | null>(null);
  const [comparison, setComparison] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      setAnalytics(await api.analytics());
      setComparison(await api.comparison("INC-A2").catch(() => null));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setComparison(await api.runComparison("INC-A2"));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const off = comparison?.memory_off;
  const on = comparison?.memory_on;

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold text-white">Incident intelligence</h1>
          <p className="text-[13px] text-slate-400">
            Every number below is measured from real orchestrator runs of the same scenario.
          </p>
        </div>
        <button className="btn-primary" disabled={busy} onClick={run}>
          {busy ? "Measuring…" : "Run memory OFF vs ON (INC-A2)"}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <Stat label="Incidents" value={analytics?.totals?.incidents ?? 0} />
        <Stat label="Resolved" value={analytics?.totals?.resolved ?? 0} tone="text-signal-green" />
        <Stat label="Avg steps" value={analytics?.efficiency?.avg_investigation_steps ?? 0} />
        <Stat label="Avg resolution" value={`${analytics?.efficiency?.avg_resolution_minutes ?? 0}m`} />
        <Stat label="Memories" value={analytics?.totals?.memories ?? 0} tone="text-signal-cyan" />
        <Stat label="Memory assisted" value={analytics?.totals?.memory_assisted_incidents ?? 0} tone="text-signal-cyan" />
        <Stat label="Failed actions" value={analytics?.totals?.failed_actions ?? 0} tone="text-signal-red" />
        <Stat label="Avoided" value={analytics?.totals?.failed_actions_avoided ?? 0} tone="text-signal-green" />
      </div>

      <Panel title="Memory OFF vs Memory ON">
        {!off || !on ? (
          <p className="text-[13px] text-slate-500">
            No comparison recorded yet. Resolve INC-A1 first (so the organisation has learned something), then run the comparison.
          </p>
        ) : (
          <table className="w-full text-left text-[13px]">
            <thead className="text-[11px] uppercase tracking-wider text-slate-500">
              <tr>
                <th className="py-1">metric</th>
                <th>memory OFF</th>
                <th>memory ON</th>
                <th>delta</th>
              </tr>
            </thead>
            <tbody>
              {ROWS.map((row) => {
                const a = off[row.off];
                const b = on[row.on];
                const numeric = typeof a === "number" && typeof b === "number";
                const delta = numeric ? Number((b - a).toFixed(2)) : null;
                const good =
                  delta === null
                    ? null
                    : delta === 0
                    ? null
                    : row.better === "up"
                    ? delta > 0
                    : delta < 0;
                return (
                  <tr key={row.label} className="border-t border-ink-700/50">
                    <td className="py-1.5 text-slate-400">{row.label}</td>
                    <td className="text-slate-200">{fmt(a)}</td>
                    <td className="font-semibold text-slate-100">{fmt(b)}</td>
                    <td className={good === null ? "text-slate-500" : good ? "text-signal-green" : "text-signal-red"}>
                      {delta === null ? "—" : `${delta > 0 ? "+" : ""}${delta}`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {comparison?.verdict && (
          <p className="mt-3 rounded-lg border border-signal-cyan/30 bg-signal-cyan/5 px-3 py-2 text-[13px] text-slate-200">
            {comparison.verdict.headline} — memory changed the recommendation:{" "}
            <span className="font-semibold text-signal-cyan">{String(comparison.verdict.memory_changed_recommendation)}</span>
          </p>
        )}
      </Panel>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Top root causes">
          <ul className="space-y-2">
            {(analytics?.top_root_causes ?? []).map((c: any) => (
              <li key={c.cause_id}>
                <div className="flex items-center justify-between text-[13px]">
                  <span className="text-slate-200">{c.cause}</span>
                  <span className="text-slate-400">
                    {c.count} · {c.share_pct}%
                  </span>
                </div>
                <div className="bar mt-1">
                  <span className="bg-signal-cyan" style={{ width: `${c.share_pct}%` }} />
                </div>
              </li>
            ))}
            {(analytics?.top_root_causes ?? []).length === 0 && <p className="text-[13px] text-slate-500">No resolved incidents yet.</p>}
          </ul>
        </Panel>

        <Panel title="Retrieval + memory quality">
          <dl className="kv">
            <dt>MRR</dt>
            <dd>{analytics?.retrieval_metrics?.mrr ?? 0}</dd>
            <dt>citations</dt>
            <dd>{analytics?.retrieval_metrics?.citations ?? 0}</dd>
            <dt>previous-incident citations</dt>
            <dd>{analytics?.retrieval_metrics?.previous_incident_citations ?? 0}</dd>
            <dt>durable ratio</dt>
            <dd>{analytics?.memory_quality?.durable_ratio ?? 0}</dd>
            <dt>with reusable lesson</dt>
            <dd>{analytics?.memory_quality?.with_reusable_lesson ?? 0}</dd>
            <dt>failed-action memories</dt>
            <dd>{analytics?.memory_quality?.failed_action_memories ?? 0}</dd>
          </dl>
        </Panel>
      </div>

      <Panel title="Recurring incident patterns">
        <ul className="space-y-2">
          {(analytics?.recurring_patterns ?? []).map((p: any) => (
            <li key={p.cause_id} className="rounded-lg bg-ink-850 p-2.5">
              <div className="flex items-center justify-between">
                <span className="text-[13px] font-semibold text-slate-100">{p.cause}</span>
                <span className={p.recurring ? "chip border-signal-amber/50 bg-signal-amber/15 text-signal-amber" : "chip border-ink-600 bg-ink-800 text-slate-400"}>
                  {p.recurring ? `recurring · ${p.occurrences}×` : "single occurrence"}
                </span>
              </div>
              <p className="mt-1 text-[12px] text-slate-400">
                services: {p.services.join(", ")} · trigger: {p.common_trigger}
              </p>
              <p className="text-[12px] text-slate-500">check first: {p.suggested_investigation}</p>
              {p.known_bad_actions.length > 0 && (
                <p className="mt-1 text-[12px] text-signal-red">known-bad actions: {p.known_bad_actions.join(", ")}</p>
              )}
            </li>
          ))}
          {(analytics?.recurring_patterns ?? []).length === 0 && (
            <p className="text-[13px] text-slate-500">Patterns appear once incidents are resolved.</p>
          )}
        </ul>
      </Panel>

      <Panel title="Failed-action ledger">
        <ul className="space-y-1.5">
          {(analytics?.failed_action_ledger ?? []).map((f: any, i: number) => (
            <li key={i} className="rounded-lg bg-ink-850 px-2.5 py-1.5 text-[12px]">
              <span className="font-mono text-slate-500">{f.incident_id}</span> · {f.action} →{" "}
              <span className="text-signal-red">{f.outcome}</span>
              {f.lesson && <span className="text-slate-400"> — {f.lesson}</span>}
            </li>
          ))}
          {(analytics?.failed_action_ledger ?? []).length === 0 && (
            <p className="text-[13px] text-slate-500">No failed actions recorded yet.</p>
          )}
        </ul>
      </Panel>

      <p className="text-[12px] text-slate-500">
        Need the incident view? <Link className="text-signal-cyan hover:underline" href="/incidents/INC-A1">open the command centre</Link>
      </p>
    </div>
  );
}
