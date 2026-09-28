"use client";

import { useEffect, useState } from "react";
import { ErrorNote, Panel } from "@/components/ui";
import { api } from "@/lib/api";

/** Live incident view: header, evidence, memory, hypotheses, recommendation, timeline. */
export function CommandView({ incidentId }: { incidentId: string }) {
  const [incident, setIncident] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [live, setLive] = useState(true);

  const load = async () => {
    try {
      setIncident(await api.incident(incidentId));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    load();
  }, [incidentId]);

  useEffect(() => {
    if (!live || !incidentId) return;
    const source = new EventSource(`${process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8765"}/api/incidents/${incidentId}/stream`);
    source.onmessage = () => load();
    source.addEventListener("timeline", () => load());
    source.onerror = () => {
      /* keep polling: the incident screen must survive a broken stream */
    };
    return () => source.close();
  }, [live, incidentId]);

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

  if (!incident) {
    return (
      <div className="space-y-3">
        <ErrorNote error={error} />
        <p className="text-[13px] text-slate-500">Loading incident {incidentId}…</p>
      </div>
    );
  }

  const top = incident.hypotheses?.[0];
  const pending = (incident.actions ?? []).filter((a: any) => !a.result && a.status !== "rejected" && a.status !== "blocked_by_memory");
  const blocked = (incident.actions ?? []).filter((a: any) => a.status === "blocked_by_memory");
  const executed = (incident.actions ?? []).filter((a: any) => a.result);
  const metrics = (incident.metrics ?? []).slice(-12);
  const deps = incident.dependencies ?? [];

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold text-white">{incident.title}</h1>
          <p className="text-[13px] text-slate-400">{incident.symptom}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn-primary" disabled={busy !== null} onClick={() => act("analyze", () => api.analyze(incidentId))}>
            Analyse
          </button>
          <button className="btn-ghost" disabled={busy !== null} onClick={() => act("advance", () => api.advance(incidentId, 1))}>
            Advance simulation
          </button>
          <button className="btn-green" disabled={busy !== null} onClick={() => act("resolve", () => api.resolve(incidentId))}>
            Resolve + postmortem
          </button>
          <button className="btn-ghost" onClick={() => setLive((v) => !v)}>
            {live ? "Live: on" : "Live: off"}
          </button>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        {/* ---------------------------------------------------------- evidence */}
        <div className="space-y-4">
          <Panel title={`Current evidence (${incident.evidence?.length ?? 0})`}>
            <ul className="max-h-72 space-y-2 overflow-y-auto pr-1">
              {(incident.evidence ?? []).slice(-18).map((e: any) => (
                <li key={e.id} className="rounded-lg bg-ink-850 px-2.5 py-1.5">
                  <div className="flex items-center gap-2">
                    <span className="chip border-ink-600 bg-ink-800 text-[10px] uppercase text-slate-400">{e.kind}</span>
                    <span className="truncate text-[12px] text-slate-200">{e.title}</span>
                  </div>
                  {e.detail && <p className="mt-0.5 line-clamp-2 text-[11px] text-slate-500">{e.detail}</p>}
                  <span className="font-mono text-[10px] text-slate-600">{e.id}</span>
                </li>
              ))}
            </ul>
          </Panel>

          <Panel title="Live metrics">
            <div className="grid grid-cols-2 gap-2">
              {metrics.map((m: any) => {
                const live2 = incident.sim_metrics?.[m.name];
                const value = live2 ?? m.value;
                const limit = m.limit ?? null;
                const pct = limit ? Math.min(100, (Number(value) / limit) * 100) : 50;
                const tone = limit && Number(value) >= limit * 0.95 ? "bg-signal-red" : pct > 70 ? "bg-signal-amber" : "bg-signal-cyan";
                return (
                  <div key={m.name} className="rounded-lg bg-ink-850 px-2.5 py-1.5">
                    <div className="truncate text-[10px] uppercase tracking-wider text-slate-500">{m.name}</div>
                    <div className="text-[15px] font-semibold text-slate-100">
                      {value}
                      {m.unit ? <span className="ml-1 text-[10px] text-slate-500">{m.unit}</span> : null}
                    </div>
                    <div className="bar mt-1">
                      <span className={tone} style={{ width: `${pct}%` }} />
                    </div>
                  </div>
                );
              })}
            </div>
          </Panel>

          <Panel title="Dependency graph">
            <ul className="space-y-1.5">
              {deps.map((d: any) => {
                const bad = d.status && d.status !== "healthy";
                return (
                  <li key={d.name} className="flex items-center justify-between rounded-lg bg-ink-850 px-2.5 py-1.5 text-[12px]">
                    <span className="flex items-center gap-2">
                      <span className={`h-1.5 w-1.5 rounded-full ${bad ? "bg-signal-red" : "bg-signal-green"}`} />
                      <span className="text-slate-200">{d.name}</span>
                      <span className="chip border-ink-600 bg-ink-800 text-[10px] text-slate-400">{d.kind}</span>
                    </span>
                    <span className={bad ? "text-signal-red" : "text-slate-500"}>{d.status}</span>
                  </li>
                );
              })}
            </ul>
          </Panel>

          <Panel title="Deployment history">
            <ul className="space-y-1.5">
              {(incident.deployments ?? []).map((d: any) => (
                <li key={d.version} className="rounded-lg bg-ink-850 px-2.5 py-1.5 text-[12px]">
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-slate-100">{d.version}</span>
                    <span
                      className={
                        (d.minutes_before_incident ?? 999) <= 60 ? "text-signal-amber" : "text-slate-500"
                      }
                    >
                      {d.minutes_before_incident != null ? `${d.minutes_before_incident}m before` : "n/a"}
                    </span>
                  </div>
                  <p className="text-[11px] text-slate-500">{d.change_summary}</p>
                </li>
              ))}
            </ul>
          </Panel>
        </div>

        {/* ------------------------------------------------------- memory + RCA */}
        <div className="space-y-4">
          <Panel
            title="Organisational memory"
            right={
              <span className="font-mono text-[10px] text-slate-500">
                {incident.recalled_memory_ids?.length ?? 0} cited · {incident.memory_mode}
              </span>
            }
          >
            {(incident.conflicts ?? []).length > 0 && (
              <div className="mb-2 rounded-lg border border-signal-amber/40 bg-signal-amber/10 p-2 text-[12px] text-signal-amber">
                <div className="font-semibold">⚠ Historical memory conflict</div>
                {incident.conflicts.slice(0, 2).map((c: any) => (
                  <p key={c.id} className="mt-1 text-slate-200">
                    {c.resolution}
                  </p>
                ))}
              </div>
            )}
            {top?.memory_links?.length ? (
              <ul className="space-y-1.5">
                {top.memory_links.map((m: any) => (
                  <li key={m.id} className="rounded-lg bg-ink-850 px-2.5 py-1.5">
                    <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-slate-500">
                      <span>{m.kind.replace(/_/g, " ")}</span>
                      <span className="font-mono">{m.score.toFixed(2)}</span>
                    </div>
                    <p className="text-[12px] text-slate-200">{m.text}</p>
                    <p className="text-[10px] text-slate-500">
                      {m.why} {m.strategy_hits?.length ? `· ${m.strategy_hits.join(", ")}` : ""}
                    </p>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-[13px] text-slate-500">
                No memory recalled — this is a cold start. The agent is reasoning from current evidence only.
              </p>
            )}
          </Panel>

          <Panel title="Hypothesis stack">
            <ol className="space-y-2">
              {(incident.hypotheses ?? []).map((h: any, index: number) => (
                <li
                  key={h.id}
                  className={`rounded-lg border p-2.5 ${
                    index === 0 ? "border-signal-cyan/40 bg-signal-cyan/5" : "border-ink-700/60 bg-ink-850"
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <div className="text-[13px] font-semibold text-slate-100">
                        {index + 1}. {h.cause}
                      </div>
                      <div className="text-[11px] text-slate-500">
                        {h.confidence_basis}
                        {h.precedent?.length ? ` · precedent: ${h.precedent.join(", ")}` : ""}
                      </div>
                    </div>
                    <div className="text-right">
                      <div className="text-[15px] font-bold text-slate-100">{(h.confidence * 100).toFixed(0)}%</div>
                      {h.confirmed && <span className="chip border-signal-green/50 bg-signal-green/15 text-signal-green">confirmed</span>}
                    </div>
                  </div>
                  <div className="mt-1.5 grid gap-1.5 md:grid-cols-2">
                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-signal-green">supporting</div>
                      <ul className="text-[11px] text-slate-300">
                        {h.supporting.slice(0, 3).map((e: any) => (
                          <li key={e.id} className="truncate">
                            <span className="font-mono text-slate-500">{e.id}</span> {e.title}
                          </li>
                        ))}
                      </ul>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-signal-red">contradicting</div>
                      <ul className="text-[11px] text-slate-300">
                        {h.contradicting.length === 0 && <li className="text-slate-500">none found</li>}
                        {h.contradicting.slice(0, 3).map((e: any) => (
                          <li key={e.id} className="truncate">
                            <span className="font-mono text-slate-500">{e.id}</span> {e.title}
                          </li>
                        ))}
                      </ul>
                    </div>
                  </div>
                  <p className="mt-1.5 text-[11px] text-slate-400">next: {h.next_diagnostic}</p>
                  <div className="mt-1.5 flex gap-2">
                    <button
                      className="btn-ghost !px-2 !py-0.5 text-[11px]"
                      onClick={() =>
                        act("fb", () =>
                          api.feedback(incidentId, {
                            target_type: "hypothesis",
                            target_id: h.id,
                            verdict: "correct",
                            comment: "confirmed by on-call",
                          })
                        )
                      }
                    >
                      👍 correct
                    </button>
                    <button
                      className="btn-ghost !px-2 !py-0.5 text-[11px]"
                      onClick={() =>
                        act("fb", () =>
                          api.feedback(incidentId, {
                            target_type: "hypothesis",
                            target_id: h.id,
                            verdict: "incorrect",
                            comment: "wrong cause",
                            corrected_cause: "slow_query_regression",
                          })
                        )
                      }
                    >
                      👎 incorrect
                    </button>
                  </div>
                </li>
              ))}
            </ol>
          </Panel>
        </div>

        {/* ----------------------------------------------- action + timeline */}
        <div className="space-y-4">
          <Panel title="Recommended action">
            {pending.length === 0 && blocked.length === 0 && (
              <p className="text-[13px] text-slate-500">No action pending — resolve the incident or advance the simulation.</p>
            )}
            <div className="space-y-2">
              {pending.map((a: any) => (
                <div key={a.id} className="rounded-lg border border-ink-700/60 bg-ink-850 p-2.5">
                  <div className="flex items-start justify-between gap-2">
                    <div className="text-[13px] font-semibold text-slate-100">{a.description}</div>
                    <span
                      className={`chip ${
                        a.risk === "READ_ONLY"
                          ? "border-signal-green/40 bg-signal-green/10 text-signal-green"
                          : "border-signal-amber/50 bg-signal-amber/15 text-signal-amber"
                      }`}
                    >
                      {a.risk}
                    </span>
                  </div>
                  <p className="mt-1 text-[11px] text-slate-400">{a.reason}</p>
                  <p className="text-[11px] text-slate-500">expected signal: {a.expected_signal}</p>
                  {a.requires_confirmation && (
                    <p className="mt-1 text-[11px] font-semibold text-signal-amber">
                      ⚠ state-changing action — requires explicit approval
                    </p>
                  )}
                  <div className="mt-2 flex flex-wrap gap-2">
                    {a.requires_confirmation && (
                      <button className="btn-ghost !px-2 !py-1 text-[12px]" disabled={busy !== null} onClick={() => act("approve", () => api.approve(incidentId, a.id))}>
                        Approve
                      </button>
                    )}
                    <button
                      className="btn-primary !px-2 !py-1 text-[12px]"
                      disabled={busy !== null}
                      onClick={() => act("execute", () => api.execute(incidentId, a.id))}
                    >
                      Execute in simulator
                    </button>
                    <button className="btn-ghost !px-2 !py-1 text-[12px]" disabled={busy !== null} onClick={() => act("whatif", () => api.whatIf(incidentId, a.id.split("~").pop() ?? ""))}>
                      What-if
                    </button>
                    <button className="btn-ghost !px-2 !py-1 text-[12px]" disabled={busy !== null} onClick={() => act("reject", () => api.reject(incidentId, a.id))}>
                      Reject
                    </button>
                  </div>
                </div>
              ))}
              {blocked.map((a: any) => (
                <div key={a.id} className="rounded-lg border border-signal-red/40 bg-signal-red/5 p-2.5">
                  <div className="text-[13px] font-semibold text-signal-red">🚫 {a.description}</div>
                  <p className="mt-1 text-[11px] text-slate-300">{a.blocked_reason}</p>
                  {a.memory_warnings?.map((w: string, i: number) => (
                    <p key={i} className="mt-1 rounded border border-signal-amber/30 bg-signal-amber/10 px-2 py-1 text-[11px] text-signal-amber">
                      LEARNED LESSON: {w}
                    </p>
                  ))}
                </div>
              ))}
            </div>
          </Panel>

          <Panel title="Action outcomes">
            {executed.length === 0 && <p className="text-[13px] text-slate-500">Nothing executed yet.</p>}
            <ul className="space-y-1.5">
              {executed.map((a: any) => (
                <li key={a.id} className="rounded-lg bg-ink-850 px-2.5 py-1.5 text-[12px]">
                  <div className="flex items-center justify-between">
                    <span className="text-slate-200">{a.description}</span>
                    <span
                      className={`chip ${
                        a.result.outcome === "helped"
                          ? "border-signal-green/50 bg-signal-green/15 text-signal-green"
                          : a.result.outcome === "temporary_improvement"
                          ? "border-signal-amber/50 bg-signal-amber/15 text-signal-amber"
                          : "border-signal-red/50 bg-signal-red/15 text-signal-red"
                      }`}
                    >
                      {a.result.outcome.replace(/_/g, " ")}
                    </span>
                  </div>
                  <p className="text-[11px] text-slate-500">{a.result.detail}</p>
                  {a.result.lesson && <p className="text-[11px] text-slate-300">lesson: {a.result.lesson}</p>}
                </li>
              ))}
            </ul>
          </Panel>

          <Panel title="Timeline">
            <ol className="max-h-[28rem] space-y-1.5 overflow-y-auto pr-1">
              {(incident.events ?? []).map((e: any) => (
                <li key={e.id} className="rounded-lg bg-ink-850 px-2.5 py-1.5">
                  <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider">
                    <span className="text-signal-cyan">{e.phase}</span>
                    <span className="text-slate-600">{new Date(e.ts).toISOString().slice(11, 19)}</span>
                    <span className="text-slate-600">{e.actor}</span>
                  </div>
                  <div className="text-[12px] text-slate-200">{e.title}</div>
                  {e.detail && <p className="line-clamp-3 text-[11px] text-slate-500">{e.detail}</p>}
                </li>
              ))}
            </ol>
          </Panel>
        </div>
      </div>
    </div>
  );
}
