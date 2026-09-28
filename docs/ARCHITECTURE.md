# Architecture

```
┌─────────────────────────────── Next.js 15 command centre (:4321) ───────────────────────────────┐
│  /  /incidents  /incidents/:id  /memory  /graph  /replay  /analytics  /runbooks  /postmortems  /settings/health │
└───────────────────────────────────────────────┬────────────────────────────────────────────────────────┘
                                                │  HTTP + SSE  (never a secret, never a provider call)
┌───────────────────────────────────────────────▼────────────────────────────────────────────────────────┐
│                                   FastAPI (recallops/api)                                        │
│  incidents · demo · insights · health  ──▶  ToolContext / ToolRegistry (validated)                 │
└───────────────────────────────────────────────┬────────────────────────────────────────────────────────┘
                                                │
┌───────────────────────────────────────────────▼────────────────────────────────────────────────────────┐
│                           IncidentOrchestrator  (recallops/agent/orchestrator.py)                    │
│                                                                                                    │
│   CurrentIncidentAnalyzer ──▶ EvidenceNormalizer ──▶ Signal/Cause engine ──▶ HypothesisEngine       │
│            │                                            │                        │               │
│            │                                   Hindsight Memory Adapter      LLM provider (grounded)  │
│            │                                            │                        │               │
│            ▼                                            ▼                        ▼               │
│     ScenarioSimulator                          MemoryPort (recall/retain/health)                    │
│     (deterministic world)                       │                  │                                 │
│            │                          Hindsight │                  │ LocalMemoryAdapter / DemoAdapter  │
│            ▼                                   ▼                  ▼                                 │
│      ActionPlanner ──▶ SafetyGate ──▶ human approval ──▶ outcome ──▶ PostmortemBuilder             │
│            │                                               │             RunbookGenerator              │
│            └───────────── blocked by memory ───────────────┘             MemoryComposer (quality)  │
└───────────────────────────────────────────────┬────────────────────────────────────────────────────────┘
                                                │
┌───────────────────────────────────────────────▼────────────────────────────────────────────────────────┐
│                        SQLite (recallops.db)  +  Hindsight bank (separate, durable)                  │
│  incidents · evidence_events · hypotheses · action_attempts · engineer_feedback · postmortems        │
│  memory_records (audit mirror) · deployments · metric_samples · log_events · service_dependencies    │
│  runbooks · incident_events (timeline) · simulation_runs                                            │
└─────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Layer contracts

**`MemoryPort`** (`recallops/memory/port.py`) is the only door to durable memory:
`recall(query, scope)`, `retain(items, scope)`, `health()`, plus optional
`graph()`, `list_memories()`, `reflect()`, `reset()`. No application module
imports `hindsight_client`. The fallback chain (`memory/factory.py`) is
`Hindsight → LocalMemoryAdapter → DemoMemoryAdapter` and always reports which
layer answered via `MemoryRecallResult.mode`.

**`SafetyGate`** (`recallops/agent/safety.py`) is stateless and independent of the
planner. It converts an `ActionDef` into a `SafetyVerdict`
(`allow` / `require_approval` / `refuse`) plus an `authorized` flag. Execution
checks `verdict.authorized`, so an approval that never happened cannot slip
through a code path that only inspects `decision`.

**`ScenarioSimulator`** (`recallops/agent/simulator.py`) owns world state for one
incident: the current stage, the live metric values, and the outcome table from
`scenarios/<ID>/actions.json`. `what_if()` and `apply_action()` read the same
table, so the what-if projection cannot disagree with the executed outcome.

**`EvidenceBundle`** (`recallops/agent/rca.py`) is the RCA engine's only input:
current evidence + scoped memory. Signals are matched by `Indicator`s (auditable
predicates over normalized evidence) and combined into causes by `CauseDef`.
Signal strength is the *weighted* fraction of indicators that fired, so a decisive
indicator (a saturated pool) outweighs a weak corroborating one (a recent deploy).

## Why ranking is deterministic

`blend_confidence()` is a pure function of (evidence support, contradicting
evidence, memory prior, evidence volume). An LLM, when configured, adds narrative
and a cross-vote that is blended in at 25% — but every vote is *grounded* against
real evidence ids and ungrounded votes are reported in
`GroundedAnalysis.rejected_votes`. This keeps the demo reproducible and makes
"memory changed the recommendation" a measurable statement.

## Where memory changes the answer

1. **Confidence** — `memory_prior_for_cause()` adds a prior (and subtracts one for
   a cause family that previously burned us). Capped: precedent alone cannot push
   a hypothesis into "proved" territory.
2. **Action selection** — `failed_actions_from_memory()` + `action_matches_memory()`
   mark a candidate `BLOCKED_BY_MEMORY` with the recalled lesson attached. The
   planner then recommends something else.
3. **Contradiction detection** — `detect_conflicts()` compares what a memory claims
   with the signals actually present; when they disagree it emits
   `MemoryConflict` and states that memory was used as context only.
4. **Postmortem → memory** — `memory_composer` writes episodes, action outcomes,
   runbook lessons, service patterns and postmortem lessons, each screened by
   `memory/quality.py`.

## Data flow for one analysis pass

```
POST /api/incidents/{id}/analyze
  → load incident + evidence (DB)                [persistence]
  → build EvidenceBundle                          [agent/rca]
  → build_memory_query(bundle)                    [agent/query]
  → MemoryPort.recall(...)                        [memory/*]  ← may degrade
  → HypothesisEngine.run(bundle, recall)          [agent/hypothesis]
      ├─ rule_based_votes (deterministic support)
      ├─ LLM cross-vote (optional, grounded)
      ├─ memory priors + links
      └─ conflicts
  → ActionPlanner.plan(..., memories)             [agent/action_planner]
  → SafetyGate per action                         [agent/safety]
  → persist hypotheses/actions/events             [persistence + services/events]
  → publish to the SSE bus                        [services/events]
```
