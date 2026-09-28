"use client";

import { useEffect, useState } from "react";
import { ErrorNote, Panel } from "@/components/ui";
import { api } from "@/lib/api";

const STATE_COLOR: Record<string, string> = {
  connected: "bg-signal-green",
  degraded: "bg-signal-amber",
  unavailable: "bg-signal-red",
  disabled: "bg-slate-500",
  unknown: "bg-slate-500",
};

function State({ value }: { value: string }) {
  return (
    <span className="flex items-center gap-2 text-[12px] text-slate-300">
      <span className={`h-2 w-2 rounded-full ${STATE_COLOR[value] ?? "bg-slate-500"}`} />
      {value}
    </span>
  );
}

export default function HealthPage() {
  const [health, setHealth] = useState<Record<string, any> | null>(null);
  const [memory, setMemory] = useState<Record<string, any> | null>(null);
  const [llm, setLlm] = useState<Record<string, any> | null>(null);
  const [hindsight, setHindsight] = useState<Record<string, any> | null>(null);
  const [database, setDatabase] = useState<Record<string, any> | null>(null);
  const [network, setNetwork] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async (probe = false) => {
    setBusy(true);
    setError(null);
    try {
      const [h, m, l, hs, db] = await Promise.all([
        api.health(),
        api.memoryHealth(),
        api.llmHealth(),
        api.hindsightHealth(probe),
        api.database(),
      ]);
      setHealth(h);
      setMemory(m);
      setLlm(l);
      setHindsight(hs);
      setDatabase(db);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    load(false);
  }, []);

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-white">System health centre</h1>
          <p className="text-[13px] text-slate-400">
            Providers, fallbacks and network diagnostics. A timeout here is explained, never fatal.
          </p>
        </div>
        <div className="flex gap-2">
          <button className="btn-ghost" disabled={busy} onClick={() => load(false)}>
            Refresh
          </button>
          <button className="btn-primary" disabled={busy} onClick={() => load(true)}>
            Diagnose memory endpoint
          </button>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Memory layer">
          {memory ? (
            <div className="space-y-2 text-[13px]">
              <div className="flex items-center justify-between">
                <span className="text-slate-400">state</span>
                <State value={memory.state} />
              </div>
              <div className="flex items-center justify-between">
                <span className="text-slate-400">mode</span>
                <span className="font-mono text-slate-200">{memory.mode}</span>
              </div>
              <p className="text-slate-300">{memory.mode_label}</p>
              <p className="text-slate-400">{memory.detail}</p>
              {memory.stats && (
                <pre className="overflow-x-auto rounded bg-ink-950 p-2 text-[11px] text-slate-400">
                  {JSON.stringify(memory.stats, null, 1)}
                </pre>
              )}
              {memory.fallbacks?.length > 0 && (
                <div className="rounded-lg bg-ink-950 p-2">
                  <div className="text-[11px] uppercase tracking-wider text-slate-500">fallback chain</div>
                  {memory.fallbacks.map((f: any) => (
                    <div key={f.provider} className="flex items-center justify-between text-[12px]">
                      <span className="font-mono text-slate-300">{f.provider}</span>
                      <State value={f.state} />
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <p className="text-[13px] text-slate-500">Loading…</p>
          )}
        </Panel>

        <Panel title="Hindsight">
          {hindsight ? (
            <div className="space-y-2 text-[13px]">
              <div className="flex items-center justify-between">
                <span className="text-slate-400">configured</span>
                <span className="text-slate-200">{String(hindsight.configured)}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-slate-400">endpoint</span>
                <span className="font-mono text-slate-200">{hindsight.endpoint ?? "-"}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-slate-400">bank</span>
                <span className="font-mono text-slate-200">{hindsight.bank_id}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-slate-400">api key</span>
                <span className="text-slate-200">{hindsight.api_key_configured ? "configured (never exposed)" : "not set"}</span>
              </div>
              {hindsight.health && <State value={hindsight.health.state} />}
              {hindsight.health?.detail && <p className="text-slate-400">{hindsight.health.detail}</p>}
              {hindsight.health?.error?.kind && (
                <div className="rounded-lg border border-signal-amber/40 bg-signal-amber/10 p-2">
                  <div className="text-[12px] font-semibold text-signal-amber">{hindsight.health.error.kind}</div>
                  <ul className="mt-1 list-disc pl-4 text-[11px] text-slate-200">
                    {(hindsight.health.hints ?? []).map((hint: string, i: number) => (
                      <li key={i}>{hint}</li>
                    ))}
                  </ul>
                </div>
              )}
              {network && (
                <div className="rounded-lg bg-ink-950 p-2 text-[11px]">
                  <div className="text-slate-400">network diagnosis: {network.verdict ?? (network.ok ? "ok" : "failed")}</div>
                  {(network.steps ?? []).map((s: any, i: number) => (
                    <div key={i} className="text-slate-500">
                      {s.step}: {s.ok ? "ok" : `fail (${s.kind ?? s.error ?? "?"})`}
                      {s.latency_ms ? ` · ${s.latency_ms}ms` : ""}
                    </div>
                  ))}
                  {(network.hints ?? []).slice(0, 4).map((h: string, i: number) => (
                    <div key={i} className="text-slate-400">• {h}</div>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <p className="text-[13px] text-slate-500">Loading…</p>
          )}
        </Panel>

        <Panel title="LLM">
          {llm ? (
            <div className="space-y-2 text-[13px]">
              <div className="flex items-center justify-between">
                <span className="text-slate-400">state</span>
                <State value={llm.state} />
              </div>
              <p className="text-slate-300">{llm.detail}</p>
              <dl className="kv">
                <dt>provider</dt>
                <dd className="font-mono">{llm.describe?.provider}</dd>
                <dt>model</dt>
                <dd className="font-mono">{llm.describe?.model}</dd>
                <dt>mode</dt>
                <dd>{llm.mode_label}</dd>
                <dt>needs api key</dt>
                <dd>{String(llm.describe?.requires_api_key)}</dd>
              </dl>
            </div>
          ) : (
            <p className="text-[13px] text-slate-500">Loading…</p>
          )}
        </Panel>

        <Panel title="Database + app">
          {database && (
            <div className="space-y-2 text-[13px]">
              <div className="flex items-center justify-between">
                <span className="text-slate-400">state</span>
                <State value={database.state} />
              </div>
              <p className="text-slate-400">{database.detail}</p>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(database.rows ?? {}).map(([table, count]) => (
                  <span key={table} className="chip border-ink-600 bg-ink-800 text-slate-300">
                    {table}: {String(count)}
                  </span>
                ))}
              </div>
              {health && (
                <pre className="overflow-x-auto rounded bg-ink-950 p-2 text-[11px] text-slate-400">
                  {JSON.stringify(health.config, null, 1)}
                </pre>
              )}
            </div>
          )}
        </Panel>
      </div>

      <Panel title="What each state means">
        <ul className="list-disc space-y-1 pl-5 text-[12px] text-slate-400">
          <li>
            <span className="text-signal-green">connected</span> — the provider answered a real request (Hindsight, or the LLM API when a key is set).
          </li>
          <li>
            <span className="text-signal-amber">degraded</span> — the primary path failed and RecallOps is answering from the documented fallback (local mirror / rule engine).
          </li>
          <li>
            <span className="text-signal-red">unavailable</span> — the provider is unreachable; incident analysis continues with current evidence only.
          </li>
          <li>Timeouts, DNS failures, refused connections, TLS errors, 401/403/404/429/5xx are classified separately and shown with concrete next steps.</li>
        </ul>
      </Panel>
    </div>
  );
}
