"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type Incident } from "@/lib/api";
import { STREAM_STATUS_LABEL, type StreamStatus } from "@/lib/use-incident-stream";

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/incidents", label: "Incidents" },
  { href: "/incidents/INC-A1", label: "Command" },
  { href: "/memory", label: "Memory" },
  { href: "/graph", label: "Graph" },
  { href: "/replay", label: "Replay" },
  { href: "/analytics", label: "Analytics" },
  { href: "/runbooks", label: "Runbooks" },
  { href: "/postmortems", label: "Postmortems" },
  { href: "/settings/health", label: "Health" },
];

export function Nav() {
  const path = usePathname();
  return (
    <nav className="flex flex-wrap items-center gap-1">
      {NAV.map((item) => {
        const active = item.href === "/" ? path === "/" : path.startsWith(item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            className={`rounded-md px-2.5 py-1 text-[13px] transition ${
              active
                ? "bg-signal-cyan/15 text-signal-cyan ring-1 ring-inset ring-signal-cyan/40"
                : "text-slate-400 hover:bg-ink-800 hover:text-slate-200"
            }`}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

export function MemoryBadge({ mode }: { mode?: string | null }) {
  const label =
    mode === "hindsight"
      ? "Hindsight"
      : mode === "local_hindsight"
      ? "Local mirror"
      : mode === "demo_fallback"
      ? "Demo store"
      : mode === "disabled"
      ? "Memory OFF"
      : "Memory n/a";
  const tone =
    mode === "hindsight"
      ? "border-signal-green/50 bg-signal-green/15 text-signal-green"
      : mode === "local_hindsight"
      ? "border-signal-amber/50 bg-signal-amber/15 text-signal-amber"
      : "border-ink-600 bg-ink-800 text-slate-400";
  return <span className={`chip ${tone}`}>{label}</span>;
}

export function StreamStatus({ status, onRetry }: { status: StreamStatus; onRetry?: () => void }) {
  const tone =
    status === "live"
      ? "border-signal-green/50 bg-signal-green/15 text-signal-green"
      : status === "reconnecting"
        ? "border-signal-amber/50 bg-signal-amber/15 text-signal-amber"
        : status === "offline"
          ? "border-signal-red/50 bg-signal-red/15 text-signal-red"
          : "border-ink-600 bg-ink-800 text-slate-300";
  const dot =
    status === "live" ? "bg-signal-green" : status === "reconnecting" ? "bg-signal-amber" : status === "offline" ? "bg-signal-red" : "bg-slate-400";
  const label = STREAM_STATUS_LABEL[status];
  return (
    <span className={`chip ${tone}`} title="Incident timeline stream status">
      <span className={`inline-block h-1.5 w-1.5 rounded-full ${dot} ${status === "reconnecting" ? "animate-pulse" : ""}`} />
      {label}
      {status === "offline" && onRetry ? (
        <button className="ml-1 underline underline-offset-2 hover:opacity-80" onClick={onRetry}>
          retry
        </button>
      ) : null}
    </span>
  );
}

export function ModeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setMode] = useState<string | null>(null);
  useEffect(() => {
    api
      .memoryHealth()
      .then((h) => setMode(h.mode))
      .catch(() => setMode(null));
  }, []);
  return (
    <div className="flex items-center gap-2">
      <MemoryBadge mode={mode} />
      {children}
    </div>
  );
}

export function IncidentHeader({ incident }: { incident: Incident }) {
  const started = incident.detected_at ? new Date(incident.detected_at).getTime() : Date.now();
  const end = incident.resolved_at ? new Date(incident.resolved_at).getTime() : Date.now();
  const minutes = Math.max(0, Math.round((end - started) / 60000));
  return (
    <div className="panel px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="text-lg font-semibold tracking-tight text-white">RecallOps</span>
          <span className="chip border-ink-600 bg-ink-800 font-mono text-slate-200">{incident.id}</span>
          <span className="chip border-signal-cyan/40 bg-signal-cyan/10 text-signal-cyan">{incident.service}</span>
          <span
            className={`chip ${
              incident.severity === "SEV-1"
                ? "border-signal-red/50 bg-signal-red/15 text-signal-red"
                : "border-signal-amber/50 bg-signal-amber/15 text-signal-amber"
            }`}
          >
            {incident.severity}
          </span>
          <span className="text-[13px] text-slate-400">
            stage <span className="font-mono text-slate-200">{incident.stage_id || "-"}</span>
          </span>
        </div>
        <div className="flex items-center gap-3 text-[13px]">
          <span className="text-slate-400">
            state <span className={incident.state === "LEARNED" ? "text-signal-green" : "text-signal-cyan"}>{incident.state}</span>
          </span>
          <span className="text-slate-400">
            elapsed <span className="text-slate-200">{minutes}m</span>
          </span>
          <span className="text-slate-400">
            steps <span className="text-slate-200">{incident.step_count}</span>
          </span>
          <MemoryBadge mode={incident.memory_mode} />
        </div>
      </div>
    </div>
  );
}

export function Panel({
  title,
  right,
  children,
  className = "",
}: {
  title: string;
  right?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <div className="panel-title">
        <span>{title}</span>
        {right}
      </div>
      <div className="p-3">{children}</div>
    </section>
  );
}

export function Stat({ label, value, tone = "text-white" }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="panel px-3 py-2">
      <div className="text-[10px] uppercase tracking-[0.18em] text-slate-500">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${tone}`}>{value}</div>
    </div>
  );
}

export function Bar({ value, max = 1, tone = "bg-signal-cyan" }: { value: number; max?: number; tone?: string }) {
  const pct = Math.max(0, Math.min(100, (value / (max || 1)) * 100));
  return (
    <div className="bar">
      <span className={tone} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="rounded-lg border border-signal-red/40 bg-signal-red/10 px-3 py-2 text-[13px] text-signal-red">
      {error}
    </div>
  );
}
