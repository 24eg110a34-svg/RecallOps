# Demo script (≈60 seconds)

Everything below is a real API call. Paste into a terminal with the API running on
`:8765`, or click through the equivalent buttons in the UI.

## 0. Reset (repeatable)

```bash
curl -sX POST http://127.0.0.1:8765/api/demo/reset -H "content-type: application/json" -d '{"wipe_memory":true}'
```

## 1. INC-A1 — the seed incident, no memory yet

```bash
curl -sX POST http://127.0.0.1:8765/api/incidents -H "content-type: application/json" -d '{"scenario_id":"INC-A1"}'
```

> SEV-1, payment-api, HTTP 503 0.4% → 31%, v2.17.4 deployed 14 minutes earlier.
> Severity is derived, not hard-coded: error rate + error multiple + criticality
> + customer impact + latency + blast radius.

```bash
curl -sX POST http://127.0.0.1:8765/api/incidents/INC-A1/analyze -H "content-type: application/json" -d '{}'
```

> `memory.relevant_count = 0` — cold start. Leading hypothesis:
> **Redis connection-pool exhaustion (≈0.87)** with supporting evidence ids,
> a next diagnostic, and no precedent.

```bash
curl -sX POST http://127.0.0.1:8765/api/demo/scenarios/INC-A1/advance -H "content-type: application/json" -d '{"steps":1,"analyze":true}'
```

> New evidence: pool `active=100 max=100 waiting=37`, `RedisTimeout`,
> and the distractor “Redis CPU 11% (healthy)”. The recommendation becomes the
> per-worker Redis connection diagnostic.

## 2. The trap: a restart

```bash
# executing without approval is refused
curl -sX POST http://127.0.0.1:8765/api/incidents/INC-A1/actions/<restart-id>/execute -d '{}' -H "content-type: application/json"
# -> {"executed": false, "requires_approval": true}

curl -sX POST http://127.0.0.1:8765/api/incidents/INC-A1/actions/<restart-id>/approve -H "content-type: application/json" -d '{"approved_by":"oncall","note":"per runbook"}'
curl -sX POST http://127.0.0.1:8765/api/incidents/INC-A1/actions/<restart-id>/execute -H "content-type: application/json" -d '{"approved_by":"oncall"}'
```

> Outcome `temporary_improvement`: 31% → 12% → back to 31% in ~48s, and a
> **reusable lesson** is captured. The incident regresses.

## 3. Resolution + learning

```bash
curl -sX POST http://127.0.0.1:8765/api/incidents/INC-A1/actions/<rollback-id>/approve -H "content-type: application/json" -d '{"approved_by":"oncall"}'
curl -sX POST http://127.0.0.1:8765/api/incidents/INC-A1/actions/<rollback-id>/execute -H "content-type: application/json" -d '{"approved_by":"oncall"}'
curl -sX POST http://127.0.0.1:8765/api/incidents/INC-A1/resolve -H "content-type: application/json" -d '{"resolved_by":"ic"}'
```

> Postmortem (12 sections) + runbook generated, **9 durable memories retained**,
> state → `LEARNED`, timeline ends with a “New organisational memory created” event.

## 4. INC-A2 — the repeat incident (mutated evidence)

Different wording (“cache acquisition timed out”), different version (v2.18.1),
different timing (6 min), mixed 502/503 — same underlying cause.

```bash
curl -sX POST http://127.0.0.1:8765/api/incidents -H "content-type: application/json" -d '{"scenario_id":"INC-A2"}'
curl -sX POST http://127.0.0.1:8765/api/incidents/INC-A2/analyze -H "content-type: application/json" -d '{}'
curl -sX POST http://127.0.0.1:8765/api/demo/scenarios/INC-A2/advance -H "content-type: application/json" -d '{"steps":1,"analyze":true}'
```

> `memories_recalled = 12`, `precedent = [INC-A1]`, memory contributes to
> confidence, a **LEARNED LESSON** warning appears immediately, and after the
> advance `restart_api_pool` appears in `blocked_actions` with the reason
> “Organisational memory records this action failing in a similar incident”.

## 5. The distractor: INC-B1 (memory must be contradicted)

```bash
curl -sX POST http://127.0.0.1:8765/api/incidents -H "content-type: application/json" -d '{"scenario_id":"INC-B1"}'
curl -sX POST http://127.0.0.1:8765/api/demo/scenarios/INC-B1/advance -H "content-type: application/json" -d '{"steps":1,"analyze":true}'
```

> Same symptom shape (timeouts, saturated “pool”, a recent deploy) but the
> evidence is a **database** problem: `PostgreSQL connection saturation` leads,
> `redis_connections_active` is 41/100, and the API returns a
> `HISTORICAL MEMORY CONFLICT` explaining that the recalled Redis precedent does
> not match and was not used as the recommendation.

## 6. Memory OFF vs ON (measured)

```bash
curl -sX POST http://127.0.0.1:8765/api/comparison/INC-A2/run -H "content-type: application/json" -d '{"persist":true}'
```

Typical measured result for this simulator:

| metric | memory OFF | memory ON |
|--------|-----------|-----------|
| memories recalled | 0 | 12 |
| similar incident surfaced | no | yes |
| failed-action warning | no | **yes** |
| repeated the known-bad action | **yes** | no |
| actions executed | 5 | **2** |
| failed remediations | 1 | **0** |

Or run the whole thing in one call:

```bash
curl -sX POST http://127.0.0.1:8765/api/demo/run -H "content-type: application/json" -d '{"scenarios":["INC-A1","INC-A2"],"run_comparison":true}'
```

## 7. If something looks broken

Open <http://127.0.0.1:4321/settings/health>:

* `Hindsight` shows whether an endpoint is configured, its live diagnosis and
  concrete next steps;
* `Memory layer` names the mode that actually answered;
* `LLM` says whether a real model is in use or the deterministic rule engine is.
