"use client";

import { useMemo } from "react";

/**
 * Operational timeline: the incident lifecycle spine with phase-grouped events,
 * provenance links, and safety-gate verdicts.
 *
 * An SRE reading a postmortem needs to answer "what did we know, when, and why
 * did we act" - not just see a flat event log. This component renders the
 * canonical lifecycle as a spine (so unreached stages are visible), groups
 * events by phase, and surfaces the evidence→hypothesis→action provenance
 * chain that the flat list hides.
 */

export interface TimelineEventItem {
  id: string;
  seq: number;
  ts: string;
  phase: string;
  title: string;
  detail: string;
  actor: string;
  meta: Record<string, unknown>;
}

export interface EvidenceRef {
  id: string;
  title?: string;
  kind?: string;
}

export interface HypothesisItem {
  id: string;
  cause: string;
  confidence: number;
  rank: number;
  confirmed: boolean;
  rejected: boolean;
  supporting: EvidenceRef[];
  contradicting: EvidenceRef[];
  memory_contribution: number;
  origin: string;
}

export interface TimelinePanelProps {
  events: TimelineEventItem[];
  hypotheses?: HypothesisItem[];
  lifecycle?: [string, string][];
}

const PHASE_TONE: Record<string, string> = {
  incident: "text-signal-red border-signal-red/40 bg-signal-red/10",
  evidence: "text-signal-cyan border-signal-cyan/40 bg-signal-cyan/10",
  memory_recall: "text-signal-violet border-signal-violet/40 bg-signal-violet/10",
  hypothesis: "text-signal-amber border-signal-amber/40 bg-signal-amber/10",
  investigation: "text-signal-amber border-signal-amber/40 bg-signal-amber/10",
  recommendation: "text-signal-green border-signal-green/40 bg-signal-green/10",
  safety_gate: "text-signal-orange border-signal-orange/40 bg-signal-orange/10",
  approval: "text-signal-green border-signal-green/40 bg-signal-green/10",
  action: "text-signal-cyan border-signal-cyan/40 bg-signal-cyan/10",
  outcome: "text-signal-green border-signal-green/40 bg-signal-green/10",
  resolution: "text-signal-green border-signal-green/40 bg-signal-green/10",
  memory: "text-signal-violet border-signal-violet/40 bg-signal-violet/10",
  system: "text-slate-400 border-ink-600 bg-ink-800",
};

const DEFAULT_LIFECYCLE: [string, string][] = [
  ["incident", "Detected"],
  ["evidence", "Evidence"],
  ["memory_recall", "Memory"],
  ["hypothesis", "Hypothesis"],
  ["investigation", "Re-analysis"],
  ["recommendation", "Recommended"],
  ["safety_gate", "Safety gate"],
  ["approval", "Approval"],
  ["action", "Action"],
  ["outcome", "Outcome"],
  ["resolution", "Resolved"],
  ["memory", "Learned"],
];

function phaseTone(phase: string): string {
  return PHASE_TONE[phase] ?? "text-slate-400 border-ink-600 bg-ink-800";
}

export function TimelinePanel({ events, hypotheses = [], lifecycle = DEFAULT_LIFECYCLE }: TimelinePanelProps) {
  const reachedPhases = useMemo(() => new Set(events.map((e) => e.phase)), [events]);

  const grouped = useMemo(() => {
    const map = new Map<string, TimelineEventItem[]>();
    for (const e of events) {
      const list = map.get(e.phase) ?? [];
      list.push(e);
      map.set(e.phase, list);
    }
    return map;
  }, [events]);

  const provenance = useMemo(() => {
    const links: { hypothesis: string; hypothesisId: string; supporting: EvidenceRef[]; contradicting: EvidenceRef[]; confidence: number }[] = [];
    for (const h of hypotheses) {
      if (h.supporting.length > 0 || h.contradicting.length > 0) {
        links.push({
          hypothesis: h.cause,
          hypothesisId: h.id,
          supporting: h.supporting,
          contradicting: h.contradicting,
          confidence: h.confidence,
        });
      }
    }
    return links;
  }, [hypotheses]);

  const safetyEvents = useMemo(
    () => events.filter((e) => e.phase === "safety_gate"),
    [events],
  );

  return (
    <div className="space-y-4">
      {/* ------------------------------------------------ lifecycle spine */}
      <div className="panel px-3 py-2.5">
        <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
          Incident lifecycle
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {lifecycle.map(([phase, label], i) => {
            const reached = reachedPhases.has(phase);
            const isCurrent = reached && (i === lifecycle.length - 1 || !reachedPhases.has(lifecycle[i + 1]?.[0] ?? ""));
            return (
              <div key={phase} className="flex items-center gap-1">
                <span
                  className={`chip text-[10px] ${
                    isCurrent
                      ? "border-signal-cyan/60 bg-signal-cyan/15 text-signal-cyan"
                      : reached
                        ? "border-ink-500 bg-ink-700 text-slate-300"
                        : "border-ink-700 bg-ink-900 text-slate-600"
                  }`}
                >
                  {label}
                </span>
                {i < lifecycle.length - 1 && <span className="text-ink-600">→</span>}
              </div>
            );
          })}
        </div>
      </div>

      {/* ------------------------------------------------ safety gate verdicts */}
      {safetyEvents.length > 0 && (
        <div className="panel px-3 py-2.5">
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            Safety gate
          </div>
          <ul className="space-y-1.5">
            {safetyEvents.map((e) => {
              const risk = (e.meta as Record<string, unknown>)?.risk as string | undefined;
              const authorized = (e.meta as Record<string, unknown>)?.authorized as boolean | undefined;
              const tone =
                risk === "READ_ONLY"
                  ? "border-signal-green/40 bg-signal-green/10 text-signal-green"
                  : risk === "REVERSIBLE"
                    ? "border-signal-amber/40 bg-signal-amber/10 text-signal-amber"
                    : "border-signal-red/40 bg-signal-red/10 text-signal-red";
              return (
                <li key={e.id} className={`rounded-lg border px-2.5 py-1.5 ${tone}`}>
                  <div className="flex items-center gap-2 text-[11px]">
                    <span className="font-mono uppercase">{risk ?? "unknown"}</span>
                    {authorized !== undefined && (
                      <span className="text-[10px] opacity-75">{authorized ? "authorised" : "blocked"}</span>
                    )}
                  </div>
                  <div className="text-[12px] text-slate-200">{e.title}</div>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {/* ------------------------------------------------ provenance */}
      {provenance.length > 0 && (
        <div className="panel px-3 py-2.5">
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            Evidence → hypothesis provenance
          </div>
          <ul className="space-y-2">
            {provenance.map((p) => (
              <li key={p.hypothesisId} className="rounded-lg bg-ink-850 px-2.5 py-1.5">
                <div className="flex items-center gap-2">
                  <span className="text-[12px] text-slate-200">{p.hypothesis}</span>
                  <span className="chip border-ink-600 bg-ink-800 text-[10px] text-slate-400">
                    {Math.round(p.confidence * 100)}%
                  </span>
                </div>
                {p.supporting.length > 0 && (
                  <div className="mt-1 flex flex-wrap items-center gap-1">
                    <span className="text-[10px] text-slate-500">supported by:</span>
                    {p.supporting.map((ev) => (
                      <span key={ev.id} className="chip border-signal-green/30 bg-signal-green/10 text-[10px] text-signal-green">
                        {ev.kind ?? "evidence"}
                      </span>
                    ))}
                  </div>
                )}
                {p.contradicting.length > 0 && (
                  <div className="mt-1 flex flex-wrap items-center gap-1">
                    <span className="text-[10px] text-slate-500">contradicted by:</span>
                    {p.contradicting.map((ev) => (
                      <span key={ev.id} className="chip border-signal-red/30 bg-signal-red/10 text-[10px] text-signal-red">
                        {ev.kind ?? "evidence"}
                      </span>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ------------------------------------------------ event stream */}
      <div className="panel px-3 py-2.5">
        <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
          Timeline ({events.length} events)
        </div>
        <ol className="max-h-[24rem] space-y-1.5 overflow-y-auto pr-1">
          {events.map((e) => (
            <li key={e.id} className={`rounded-lg border px-2.5 py-1.5 ${phaseTone(e.phase)}`}>
              <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider">
                <span className="font-semibold">{e.phase.replace(/_/g, " ")}</span>
                <span className="text-slate-500">#{e.seq}</span>
                <span className="text-slate-600">{new Date(e.ts).toISOString().slice(11, 19)}</span>
                <span className="text-slate-600">{e.actor}</span>
              </div>
              <div className="text-[12px] text-slate-200">{e.title}</div>
              {e.detail && <p className="line-clamp-2 text-[11px] text-slate-500">{e.detail}</p>}
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}
