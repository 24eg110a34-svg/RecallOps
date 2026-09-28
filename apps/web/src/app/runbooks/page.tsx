"use client";

import { useEffect, useState } from "react";
import { ErrorNote, Panel } from "@/components/ui";
import { api } from "@/lib/api";

export default function RunbooksPage() {
  const [runbooks, setRunbooks] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .runbooks()
      .then((r) => setRunbooks(r.runbooks ?? []))
      .catch((e) => setError((e as Error).message));
  }, []);

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div>
        <h1 className="text-lg font-semibold text-white">Generated runbooks</h1>
        <p className="text-[13px] text-slate-400">
          Written from confirmed resolutions — including the steps that are known to be dangerous.
        </p>
      </div>

      {runbooks.length === 0 && (
        <Panel title="Runbooks">
          <p className="text-[13px] text-slate-500">No runbook yet — resolve an incident to generate one.</p>
        </Panel>
      )}

      {runbooks.map((r) => (
        <Panel key={r.id} title={`${r.title}`} right={<span className="text-[10px] text-slate-500">{r.service}</span>}>
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <h3 className="text-[11px] uppercase tracking-wider text-slate-500">Symptoms</h3>
              <ul className="text-[12px] text-slate-300">
                {(r.symptoms ?? []).map((s: string, i: number) => (
                  <li key={i}>• {s}</li>
                ))}
              </ul>
              <h3 className="mt-2 text-[11px] uppercase tracking-wider text-signal-cyan">Check first</h3>
              <ol className="text-[12px] text-slate-300">
                {(r.first_checks ?? []).map((s: string, i: number) => (
                  <li key={i}>{i + 1}. {s}</li>
                ))}
              </ol>
            </div>
            <div>
              <h3 className="text-[11px] uppercase tracking-wider text-signal-red">Avoid (learned the hard way)</h3>
              <ul className="text-[12px] text-slate-300">
                {(r.known_failed_actions ?? []).map((s: string, i: number) => (
                  <li key={i}>✗ {s}</li>
                ))}
                {(r.known_failed_actions ?? []).length === 0 && <li className="text-slate-500">none recorded</li>}
              </ul>
              <h3 className="mt-2 text-[11px] uppercase tracking-wider text-signal-green">Recommended actions</h3>
              <ul className="text-[12px] text-slate-300">
                {(r.recommended_actions ?? []).map((s: string, i: number) => (
                  <li key={i}>✓ {s}</li>
                ))}
              </ul>
              <h3 className="mt-2 text-[11px] uppercase tracking-wider text-slate-500">Verification</h3>
              <ul className="text-[12px] text-slate-300">
                {(r.verification ?? []).map((s: string, i: number) => (
                  <li key={i}>• {s}</li>
                ))}
              </ul>
              <h3 className="mt-2 text-[11px] uppercase tracking-wider text-slate-500">Prevention</h3>
              <ul className="text-[12px] text-slate-300">
                {(r.prevention ?? []).map((s: string, i: number) => (
                  <li key={i}>• {s}</li>
                ))}
              </ul>
            </div>
          </div>
        </Panel>
      ))}
    </div>
  );
}
