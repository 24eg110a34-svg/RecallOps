# RecallOps — AI Incident Intelligence with Hindsight memory

> **Incident → Evidence → Memory recall → Hypotheses → Root cause → Planned action →
> Safety gate → Human approval → Simulated outcome → Postmortem → Durable memory → Better next time**

A production-shaped incident response agent for SRE/DevOps teams. The point of the
project is the **learning loop**: a repeat incident is investigated faster and
safer because the organisation remembers what happened last time — including which
actions *failed*.

Everything is real: a FastAPI backend, a SQLite database, a real
[Hindsight](https://hindsight.vectorize.io) adapter, a validated tool layer, an
LLM provider abstraction, and a deterministic simulator that makes the whole demo
repeatable. The Memory OFF / Memory ON numbers are **measured**, never staged.

---

## 1. Quick start (2 terminals)

### 1.1 Backend (port **8765**)

```bash
# Windows
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env          # optional: everything works without it
.\.venv\Scripts\python -m uvicorn recallops.main:app --host 127.0.0.1 --port 8765
```

```bash
# macOS / Linux
python3 -m venv .venv
pip install -r requirements.txt
cp .env.example .env
./.venv/bin/python -m uvicorn recallops.main:app --host 127.0.0.1 --port 8765
```

API docs: <http://127.0.0.1:8765/docs> · health: <http://127.0.0.1:8765/health>

### 1.2 Frontend (port **4321**)

```bash
cd apps/web
npm install
npm run dev        # http://127.0.0.1:4321
```

> Ports are deliberately **8765** (API) and **4321** (web). Override with
> `API_PORT` / `WEB_PORT` and `NEXT_PUBLIC_API_URL`.

### 1.3 The 60-second demo

Open <http://127.0.0.1:4321> and press **“▶ Run full demo (A1 → A2 → comparison)”**, or do it
by hand:

| # | Action | What you should see |
|---|--------|---------------------|
| 1 | `POST /api/incidents {"scenario_id":"INC-A1"}` | SEV-1 payment-api incident, severity explained factor by factor |
| 2 | `POST /api/incidents/INC-A1/analyze` | **No memory recalled** (cold start) → Redis pool exhaustion leads, confidence ~0.87 |
| 3 | `POST /api/demo/scenarios/INC-A1/advance` | pool pinned at 100/100, RedisTimeout, “Redis CPU is normal” (a distractor) |
| 4 | approve + execute `restart_api_pool` | approval required → **temporary improvement, then regression**; a reusable lesson is captured |
| 5 | approve + execute `rollback_recent_deploy` | incident resolved (5xx 31% → 0.4%) |
| 6 | `POST /api/incidents/INC-A1/resolve` | postmortem, runbook, **9 durable memories retained**, state `LEARNED` |
| 7 | `POST /api/incidents {"scenario_id":"INC-A2"}` + analyze | **12 memories recalled**, `INC-A1` cited as precedent, ⚠ *“restarting API pods produced only temporary improvement and regression”* |
| 8 | advance, then look at the recommendation | `restart_api_pool` is **blocked by memory**; the agent goes to the per-worker connection diagnostic instead |
| 9 | `POST /api/comparison/INC-A2/run` | the OFF/ON board: **5 actions vs 2**, failed action repeated **yes vs no** |

---

## 2. What is real, and what falls back

| Layer | Real integration | Fallback (clearly labelled, never faked) |
|-------|------------------|-------------------------------------------|
| Memory | `hindsight-client` → `aretain` / `arecall` / `areflect` / `alist_memories` / `aget_version` | `local_hindsight` — the same items with the same document ids in the app SQLite, shown as **“Local Hindsight mirror (Hindsight unreachable)”** |
| LLM | any OpenAI-compatible endpoint (OpenAI / Groq / OpenRouter / vLLM) | `local_heuristic` — the deterministic RCA engine, shown as **“deterministic rule engine”** |
| Incident data | — | `scenarios/` — a deterministic simulator with alerts, logs, metrics, deployments, dependencies, stages, action outcomes and ground truth |
| Data | SQLite (`recallops.db`) | — |

`GET /health/memory` always reports the mode that actually answered, and
`GET /health/hindsight` reports whether Hindsight is configured, reachable and
diagnosed. **The demo store is never presented as Hindsight.**

### Enable real Hindsight

```bash
docker compose up -d hindsight          # local Hindsight on :8888
# or point at Hindsight Cloud and set the key
```

```dotenv
HINDSIGHT_BASE_URL=http://localhost:8888
HINDSIGHT_API_KEY=                     # optional for a local server
HINDSIGHT_BANK_ID=recallops-org
```

### Enable a real LLM

```dotenv
LLM_PROVIDER=openai_compatible
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
LLM_BASE_URL=https://api.openai.com/v1
```

The agent works without both. What changes with a real LLM: hypothesis narrative
and cross-voting. What does **not** change: grounding, ranking, safety, or the
memory effect — those are deterministic by design and covered by tests.

---

## 3. The learning loop in code

```
recallops/
├── domain/        enums, state machine, evidence DTOs, signal/cause knowledge base, explainable severity & confidence
├── agent/         scenarios loader · evidence normalizer · RCA engine · hypothesis engine
│                  action planner · safety gate · deterministic simulator · postmortem & runbook
│                  memory composer · pattern detection · orchestrator
├── memory/        port.py (MemoryPort) · hindsight_adapter.py (real SDK) · local_adapter.py (mirror)
│                  models.py · quality.py (durable-memory filter) · graph.py · factory.py (fallback chain)
├── llm/           provider abstraction, OpenAI-compatible + deterministic local engine
├── tools/         validated tool registry (9 required tools + get_metrics/get_dependencies)
├── services/      resilience (retry/breaker/diagnostics) · providers · demo · comparison · analytics · events (SSE)
├── persistence/   SQLAlchemy models, session handling, reset
├── api/           FastAPI routers
└── main.py        app factory
```

### Memory categories written

`incident_episode` · `action_outcome` (with `helped: yes/no` and a reusable lesson) ·
`runbook_lesson` (useful / unnecessary / dangerous / missing) · `service_pattern` ·
`postmortem_lesson` · `engineer_correction`

Every candidate passes `memory/quality.py`. Rejections are reported with a reason
(the UI shows them) — e.g. *“Engineer restarted the API”* is **not** stored, while
*“Restarting payment-api pods did not resolve Redis connection-pool exhaustion”* is.

### Historical memory is precedent, not proof

* memory contributes a **prior** to confidence and a **warning** on actions;
* a memory alone can never certify a cause (`blend_confidence` caps confidence
  when evidence support is weak — see `tests/unit/test_evidence_and_state.py`);
* when current evidence contradicts a memory, `rca.detect_conflicts` emits a
  **HISTORICAL MEMORY CONFLICT** and the current evidence wins
  (`tests/integration/test_incident_flow.py::test_contradiction_between_memory_and_evidence`).

---

## 4. Safety model

| Tier | Behaviour |
|------|-----------|
| `READ_ONLY` | may run automatically in the simulator |
| `REVERSIBLE` | requires an explicit human approval that names a person |
| `HIGH_RISK` | **never executed** — shown with warnings; approval is recorded for audit only |

Risk is *derived* from action properties (`read_only`, `reversible`,
`data_loss_risk`, `production_impact`) rather than hand-labelled, and the safety
gate is independent of the planner, so a planner bug cannot promote a destructive
action. The LLM never touches infrastructure: it proposes a tool call, the backend
validates and executes it.

---

## 5. Failure handling (a timeout is a diagnosis, not a crash)

```
classify error → retry with backoff+jitter → circuit breaker → fallback chain → visible degraded state
```

* errors are classified (`connect_timeout`, `dns_failure`, `connection_refused`,
  `tls_failure`, `401/403/404/429/5xx`, `malformed_response`, …);
* only retryable classes retry; 401 never does;
* after `N` failures the circuit opens, so a dead provider costs one fast failure;
* `GET /health/network` runs DNS + TCP + TLS + readiness probes and returns
  concrete next steps (IPv6 routing, proxy, DNS, firewall, provider status);
* the incident screen keeps working: analysis continues from current evidence with
  an explicit “memory unavailable” note
  (`tests/integration/test_incident_flow.py::test_hindsight_timeout_does_not_crash_incident_ui`).

---

## 6. Security

* secrets only in `.env` (never committed); `GET /health` exposes a redacted
  config snapshot and `/health/llm` reports only whether a key exists;
* `security.py` redacts credentials (`api_key=…`, `sk-…`, JWTs, connection URIs)
  **before** anything reaches the database, an LLM provider or Hindsight;
* logs, alerts and dependency notes are untrusted input: they are
  injection-hardened (`ignore all previous instructions` → `[neutralized:…]`) and
  fenced when they reach a prompt;
* tool parameters are validated with Pydantic; unknown tools/ids fail closed;
* high-risk actions require approval and are refused by the executor.

---

## 7. API surface

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/incidents` | open a seeded incident |
| GET | `/api/incidents/{id}` | full incident view (evidence, hypotheses, actions, memory, postmortem) |
| GET | `/api/incidents/{id}/stream` | SSE live timeline |
| POST | `/api/incidents/{id}/analyze` | run the analysis loop |
| POST | `/api/incidents/{id}/advance` | advance the deterministic simulator |
| POST | `/api/incidents/{id}/actions/{action_id}/approve` | approve (names a human) |
| POST | `/api/incidents/{id}/actions/{action_id}/reject` | reject with a reason |
| POST | `/api/incidents/{id}/actions/{action_id}/execute` | execute in the simulator |
| POST | `/api/incidents/{id}/what-if` | predicted projection for any action |
| POST | `/api/incidents/{id}/resolve` | postmortem + runbook + memory retention |
| GET | `/api/incidents/{id}/memories` | recalled + retained memory for the incident |
| POST | `/api/incidents/{id}/feedback` | engineer feedback → possible correction memory |
| GET | `/api/incidents/{id}/replay` | step-by-step replay |
| GET/POST | `/api/demo/reset` · `/start` · `/pause` · `/resume` · `/run` | deterministic demo control |
| POST | `/api/demo/scenarios/{id}/advance` | reveal the next evidence stage |
| POST | `/api/comparison/{scenario}/run` | measured memory OFF vs ON |
| GET | `/api/memory` · `/memory/search` · `/memory/graph` · `/memory/quality` | memory browser, search, graph, quality |
| GET | `/api/analytics` · `/analytics/patterns` | learning analytics, recurring patterns |
| GET | `/api/tools` · POST `/api/tools/{name}/invoke` | tool layer |
| GET | `/health` · `/health/memory` · `/health/hindsight` · `/health/llm` · `/health/database` · `/health/network` | provider health & diagnostics |

---

## 8. Tools

`get_incident_context` · `get_recent_deployments` · `query_logs` ·
`get_service_health` · `recall_memory` · `record_action` · `execute_demo_action` ·
`record_feedback` · `retain_memory` · `get_metrics` · `get_dependencies`

All validated (`recallops/tools/base.py`), redacted on the way out, and callable
from the UI (`/api/tools/{name}/invoke`) for inspection.

---

## 9. Scenarios

| ID | Service | Shape | Purpose |
|----|---------|-------|---------|
| `INC-A1` | payment-api | Redis pool saturation after a deploy | the seed: cold start, failed restart, rollback, learning |
| `INC-A2` | payment-api | same cause, mutated wording/timing/version | proves memory changes the response |
| `INC-B1` | orders-api | Postgres connection saturation + slow query | distractor: recalled Redis memory must be **contradicted** |
| `INC-C1` | checkout-web | third-party provider timeout | degraded mode beats restarts/rollbacks |
| `INC-D1` | catalog-api | N+1 query CPU spike | restarts and scale-out are both wrong |

Each directory holds `alert.json`, `logs.ndjson`, `deployments.json`,
`metrics.json`, `dependencies.json`, `stages.json`, `actions.json`,
`ground_truth.json` and `scenario.json`. Timestamps come from `base_time` + an
offset, so runs are byte-identical.

---

## 10. Tests

```bash
python -m pytest -q                 # 72 tests
python -m pytest tests/unit -q
python -m pytest tests/integration -q
python scripts/run_eval.py          # the memory OFF vs ON table, from real runs
```

Required behaviours are covered by named tests:

| Behaviour | Test |
|-----------|------|
| repeat incident uses memory | `test_repeat_incident_uses_memory` |
| failed action is not recommended as safe | `test_failed_action_is_not_recommended_as_safe` |
| no memory falls back to current evidence | `test_no_memory_falls_back_to_current_evidence` (as `test_analysis_without_memory_falls_back_to_current_evidence`) |
| resolution creates postmortem + memories | `test_resolution_creates_postmortem_and_memories` |
| demo reset restores a known state | `test_demo_reset_restores_known_state` |
| Hindsight timeout does not break the UI | `test_hindsight_timeout_does_not_crash_incident_ui` |
| state-changing action requires confirmation | `test_state_changing_action_requires_confirmation` |

Plus: normalization, state transitions, action risk classification, memory
composition and quality, contradiction detection, tool validation, provider
retry/circuit-breaker, secret redaction, postmortem/runbook generation, replay,
analytics and the full e2e story (`tests/e2e/test_demo_story.py`).

---

## 11. Scripts

```bash
python scripts/seed_demo.py --all   # seed every scenario and run the full story
python scripts/reset_demo.py        # known state again (optionally keep memory)
python scripts/run_eval.py          # memory OFF vs ON table, saved with --json
```

---

## 12. Docker

```bash
docker compose up -d hindsight      # real Hindsight on :8888
docker compose up -d                # Hindsight + API on :8765 + frontend on :4321
```

Both the API and frontend containers use `restart: unless-stopped` and have
healthchecks configured. The API healthcheck hits `/health/live` (liveness only,
so a Hindsight outage does not trigger a restart loop).

---

## 13. Running 24/7 on Windows

RecallOps is designed to run continuously. Two practical methods:

### Option A: Windows Task Scheduler (recommended)

```powershell
# 1. Install the auto-start task (run as Administrator)
powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1

# 2. Start immediately
schtasks /run /tn "RecallOps-24x7"

# 3. Check health
powershell -ExecutionPolicy Bypass -File scripts\health-check.ps1
```

The task starts at system boot, runs as SYSTEM, and restarts up to 3 times on
failure (1-minute interval). Logs are written to `.runtime\api.log` and
`.runtime\web.log`.

### Option B: Manual start/stop

```powershell
# Start (background, survives the current session)
powershell -ExecutionPolicy Bypass -File scripts\start-24x7.ps1

# Stop
powershell -ExecutionPolicy Bypass -File scripts\stop-24x7.ps1
```

### Health endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health/live` | Liveness: is the process up? Never touches the database. |
| `GET /health/ready` | Readiness: can it serve traffic? Checks DB read + write + schema. Returns 503 if not. |
| `GET /health` | Full component report with runtime identity (instance ID, uptime, PID). |

The `instance_id` in every health response and SSE `ready` frame lets you
detect a backend restart. The frontend uses it to resync automatically.

---

## 14. Configuration

All variables live in `.env.example`. The ones that matter:

| Variable | Default | Meaning |
|----------|---------|---------|
| `HINDSIGHT_BASE_URL` | *(empty)* | real Hindsight endpoint; empty ⇒ local mirror |
| `HINDSIGHT_API_KEY` | *(empty)* | bearer token for Hindsight Cloud |
| `HINDSIGHT_BANK_ID` | `recallops-org` | memory bank holding organisational learning |
| `HINDSIGHT_TIMEOUT` | `6.0` | per-request timeout (seconds) |
| `LLM_PROVIDER` | `local_heuristic` | `openai_compatible` or `local_heuristic` |
| `LLM_API_KEY` / `LLM_MODEL` / `LLM_BASE_URL` | – | LLM credentials and model |
| `DATABASE_URL` | `sqlite:///./recallops.db` | application database |
| `DEMO_MODE` | `true` | demo affordances enabled |
| `SCENARIOS_DIR` | `./scenarios` | scenario data |
| `API_PORT` / `WEB_PORT` | `8765` / `4321` | ports |

---

## 15. Honest limitations

* The RCA engine is a curated knowledge base of failure families plus an LLM
  narrative. It is explainable and reproducible by design — it is not a trained
  model, and it will not diagnose a failure family it has never seen.
* Actions run in a **simulator**. There is no production integration, and none is
  faked: `execute_demo_action` is labelled as such everywhere.
* The Memory OFF/ON comparison measures this simulator, not a real fleet.
* With `LLM_PROVIDER=local_heuristic` the "LLM" is the rule engine; the UI says so
  on the health page.

Further reading: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
[`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) ·
[`docs/HINDSIGHT.md`](docs/HINDSIGHT.md)
