export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8765";

export type Severity = "SEV-1" | "SEV-2" | "SEV-3" | "SEV-4";

export type Evidence = {
  id: string;
  kind: string;
  source: string;
  title: string;
  detail: string;
  ts: string | null;
  stage: string | null;
  raw: Record<string, unknown>;
  redacted: boolean;
  signal_hints?: string[];
};

export type MemoryLink = {
  id: string;
  kind: string;
  text: string;
  source: string;
  score: number;
  incident_id: string | null;
  why: string;
  strategy_hits: string[];
};

export type Hypothesis = {
  id: string;
  cause: string;
  category: string;
  confidence: number;
  confidence_basis: string;
  rationale: string;
  next_diagnostic: string;
  supporting: Evidence[];
  contradicting: Evidence[];
  precedent: string[];
  memory_links: MemoryLink[];
  memory_contribution: number;
  signals: string[];
  confirmed: boolean;
  rejected: boolean;
  rejected_reason: string | null;
  rank: number;
  origin: string;
};

export type ActionResult = {
  outcome: string;
  verdict: string;
  detail: string;
  helped: boolean | null;
  lesson: string | null;
};

export type Action = {
  id: string;
  description: string;
  type: string;
  risk: string;
  status: string;
  reason: string;
  expected_signal: string;
  requires_confirmation: boolean;
  blocked_reason: string | null;
  memory_warnings: string[];
  safety_notes: string[];
  result: ActionResult | null;
  what_if: { predicted_outcome: string; predicted_detail: string; projection: Record<string, unknown>[]; notes: string[] } | null;
  hypothesis_id: string | null;
  step_index: number;
};

export type TimelineEvent = {
  id: string;
  seq: number;
  ts: string;
  phase: string;
  title: string;
  detail: string;
  actor: string;
  meta: Record<string, unknown>;
};

export type Conflict = {
  id: string;
  subject: string;
  historical_claim: string;
  current_evidence: string[];
  resolution: string;
  severity: string;
};

export type Incident = {
  id: string;
  scenario_id: string;
  service: string;
  title: string;
  severity: Severity;
  state: string;
  symptom: string;
  detected_at: string | null;
  resolved_at: string | null;
  root_cause: string;
  root_cause_id: string;
  resolution: string;
  step_count: number;
  memory_mode: string;
  memory_assisted: boolean;
  evidence: Evidence[];
  hypotheses: Hypothesis[];
  actions: Action[];
  events: TimelineEvent[];
  memories: Record<string, unknown>[];
  recalled_memory_ids: string[];
  conflicts: Conflict[];
  deployments: Record<string, unknown>[];
  metrics: Record<string, unknown>[];
  logs: Record<string, unknown>[];
  dependencies: Record<string, unknown>[];
  sim_metrics: Record<string, number>;
  stage_id: string;
  feedback: Record<string, unknown>[];
  severity_assessment: Record<string, unknown> | null;
  impact: Record<string, unknown> | null;
  scenario: Record<string, unknown> | null;
  postmortem: Record<string, unknown> | null;
  runbook: Record<string, unknown> | null;
};

export type Scenario = {
  id: string;
  title: string;
  service: string;
  role: string;
  severity_hint: string;
  summary: string;
  tags: string[];
  memory_expected: boolean;
  stages: string[];
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail = body?.detail ?? response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body as T;
}

export const api = {
  health: () => request<Record<string, any>>("/health"),
  memoryHealth: () => request<Record<string, any>>("/health/memory"),
  llmHealth: () => request<Record<string, any>>("/health/llm"),
  hindsightHealth: (probe = false) =>
    request<Record<string, any>>(`/health/hindsight?probe=${probe}`),
  network: (url?: string) =>
    request<Record<string, any>>(`/health/network${url ? `?url=${encodeURIComponent(url)}` : ""}`),
  database: () => request<Record<string, any>>("/health/database"),

  scenarios: () => request<{ scenarios: Scenario[] }>("/api/demo/scenarios"),
  demoState: () => request<Record<string, any>>("/api/demo/state"),
  resetDemo: (wipeMemory = true) =>
    request<Record<string, any>>("/api/demo/reset", {
      method: "POST",
      body: JSON.stringify({ wipe_memory: wipeMemory }),
    }),
  startSimulation: (scenarioId: string, autoAdvance = true) =>
    request<Record<string, any>>("/api/demo/start", {
      method: "POST",
      body: JSON.stringify({ scenario_id: scenarioId, auto_advance: autoAdvance, auto_analyze: true }),
    }),
  pauseSimulation: () => request<Record<string, any>>("/api/demo/pause", { method: "POST" }),
  resumeSimulation: () => request<Record<string, any>>("/api/demo/resume", { method: "POST" }),
  advanceScenario: (scenarioId: string, steps = 1) =>
    request<Record<string, any>>(`/api/demo/scenarios/${scenarioId}/advance`, {
      method: "POST",
      body: JSON.stringify({ steps, analyze: true }),
    }),
  runDemo: () =>
    request<Record<string, any>>("/api/demo/run", {
      method: "POST",
      body: JSON.stringify({ scenarios: ["INC-A1", "INC-A2"], run_comparison: true }),
    }),

  incidents: () => request<{ incidents: any[] }>("/api/incidents"),
  incident: (id: string) => request<Incident>(`/api/incidents/${id}`),
  createIncident: (scenarioId: string) =>
    request<{ incident: Incident; created: boolean }>("/api/incidents", {
      method: "POST",
      body: JSON.stringify({ scenario_id: scenarioId }),
    }),
  analyze: (id: string, memoryEnabled = true) =>
    request<Record<string, any>>(`/api/incidents/${id}/analyze`, {
      method: "POST",
      body: JSON.stringify({ memory_enabled: memoryEnabled }),
    }),
  advance: (id: string, steps = 1) =>
    request<Record<string, any>>(`/api/incidents/${id}/advance?steps=${steps}&analyze=true`, {
      method: "POST",
    }),
  approve: (id: string, actionId: string, who = "oncall") =>
    request<Record<string, any>>(`/api/incidents/${id}/actions/${actionId}/approve`, {
      method: "POST",
      body: JSON.stringify({ approved_by: who }),
    }),
  reject: (id: string, actionId: string, reason = "not now") =>
    request<Record<string, any>>(`/api/incidents/${id}/actions/${actionId}/reject`, {
      method: "POST",
      body: JSON.stringify({ rejected_by: "oncall", reason }),
    }),
  execute: (id: string, actionId: string, who = "oncall") =>
    request<Record<string, any>>(`/api/incidents/${id}/actions/${actionId}/execute`, {
      method: "POST",
      body: JSON.stringify({ approved_by: who }),
    }),
  whatIf: (id: string, action: string) =>
    request<Record<string, any>>(`/api/incidents/${id}/what-if`, {
      method: "POST",
      body: JSON.stringify({ action }),
    }),
  resolve: (id: string) =>
    request<Record<string, any>>(`/api/incidents/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ resolved_by: "incident-commander" }),
    }),
  feedback: (id: string, payload: Record<string, unknown>) =>
    request<Record<string, any>>(`/api/incidents/${id}/feedback`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  replay: (id: string) => request<Record<string, any>>(`/api/incidents/${id}/replay`),
  memories: (id: string) => request<Record<string, any>>(`/api/incidents/${id}/memories`),
  catalog: () => request<Record<string, any>>("/api/actions/catalog"),
  tools: () => request<Record<string, any>>("/api/tools"),

  memory: () => request<Record<string, any>>("/api/memory"),
  memorySearch: (q: string) => request<Record<string, any>>(`/api/memory/search?q=${encodeURIComponent(q)}`),
  memoryGraph: (root?: string) =>
    request<Record<string, any>>(`/api/memory/graph${root ? `?root_incident_id=${root}` : ""}`),
  memoryQuality: () => request<Record<string, any>>("/api/memory/quality"),

  analytics: () => request<Record<string, any>>("/api/analytics"),
  patterns: () => request<{ patterns: any[] }>("/api/analytics/patterns"),
  runbooks: () => request<Record<string, any>>("/api/runbooks"),
  postmortems: () => request<Record<string, any>>("/api/postmortems"),
  comparison: (scenario: string) => request<Record<string, any>>(`/api/comparison/${scenario}`),
  runComparison: (scenario: string) =>
    request<Record<string, any>>(`/api/comparison/${scenario}/run`, {
      method: "POST",
      body: JSON.stringify({ persist: true }),
    }),
};

export function severityColor(severity: string) {
  switch (severity) {
    case "SEV-1":
      return "border-signal-red/50 bg-signal-red/15 text-signal-red";
    case "SEV-2":
      return "border-signal-amber/50 bg-signal-amber/15 text-signal-amber";
    case "SEV-3":
      return "border-signal-cyan/40 bg-signal-cyan/10 text-signal-cyan";
    default:
      return "border-ink-600 bg-ink-800 text-slate-300";
  }
}

export function riskColor(risk: string) {
  if (risk === "HIGH_RISK") return "border-signal-red/50 bg-signal-red/15 text-signal-red";
  if (risk === "REVERSIBLE") return "border-signal-amber/50 bg-signal-amber/15 text-signal-amber";
  return "border-signal-green/40 bg-signal-green/10 text-signal-green";
}

export function stateColor(state: string) {
  if (["RESOLVED", "CLOSED", "LEARNED"].includes(state)) return "text-signal-green";
  if (state === "MITIGATED" || state === "MONITORING") return "text-signal-amber";
  return "text-signal-cyan";
}
