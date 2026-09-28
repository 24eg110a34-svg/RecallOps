# Hindsight integration

RecallOps uses the **official** Python client (`pip install hindsight-client`,
verified against `hindsight-client 0.10.1`) and only documented methods:

| Purpose | SDK call |
|---------|----------|
| connectivity / version | `Hindsight.aget_version()` |
| bank existence | `aget_bank_config(bank_id=...)`, then `acreate_bank(bank_id=...)` |
| store learning | `aretain_batch(bank_id=..., items=[...], document_id=...)` |
| multi-strategy recall | `arecall(bank_id=..., query=..., budget=..., max_tokens=..., types=[...])` |
| memory-grounded synthesis | `areflect(bank_id=..., query=..., budget=...)` |
| browser / listing | `alist_memories(bank_id=..., limit=...)` |
| entity graph | `client.entities.*` |
| destructive reset (opt-in) | `delete_bank(bank_id=...)` |

Source: <https://hindsight.vectorize.io/sdks/python> and
<https://hindsight.vectorize.io/developer/api/recall>.

## What we store per memory

`MemoryItem.to_hindsight_item()` produces:

* `content` — natural-language text built for fact extraction (incident, service,
  symptoms, confirmed cause, resolution, action, expected signal, actual outcome,
  reusable lesson, tags). **Redacted and injection-hardened first.**
* `context` — e.g. `recallops action_outcome for service payment-api`
* `metadata` — flat string map: `memory_kind`, `service`, `incident_id`,
  `cause_id`, `durability`, `action_id`, `outcome`, `helped`, `entities`
* `tags` — service slug + scenario tags
* `entities` — `[{text, type: "CONCEPT"}]` for service / incident / cause
* `document_id` — `recallops:<kind>:<incident>:<id>` so re-retaining the same
  lesson **upserts** instead of duplicating

Because the structured fields are in `metadata`, a recalled fact can be
re-assembled into a first-class `MemoryItem` — which is what the planner needs to
decide whether a memory describes *this* action.

## Recall strategies

Hindsight recall is multi-strategy (semantic, keyword/BM25, graph, temporal)
then reranked. The adapter maps the returned `scores` into human-readable tags
(`hindsight:semantic`, `hindsight:keyword`, `hindsight:graph`, `hindsight:temporal`,
`hindsight:final-fusion`) and the UI shows which strategies contributed to each
recalled memory.

The local mirror uses an honest, explainable proxy for the same idea and labels
itself accordingly (`local:keyword-bm25`, `local:semantic-overlap`,
`local:entity-graph`, `local:recency`).

## Fallback chain

```
hindsight (real)  →  local_hindsight (SQLite mirror of the same items)  →  demo_fallback
```

* `HINDSIGHT_ENABLED=0` forces the local mirror.
* `HINDSIGHT_BASE_URL` unset ⇒ the mirror is used from the start.
* A failed recall is **never** fatal: the incident continues with current evidence
  and a visible reason (`MemoryRecallResult.degraded_reason`).
* The mode is always reported: `/health/memory`, `incident.memory_mode`, and the
  badge in the UI. The demo store is never presented as Hindsight.

## Running Hindsight locally

```bash
docker compose up -d hindsight      # API on :8888
```

```dotenv
HINDSIGHT_BASE_URL=http://localhost:8888
HINDSIGHT_API_KEY=                  # optional for a local server
HINDSIGHT_BANK_ID=recallops-org
HINDSIGHT_TIMEOUT=6.0
```

Hindsight needs an LLM key of its own to extract facts during `retain`
(`OPENAI_API_KEY` in the compose file). Without a key, RecallOps still works — it
just stays on the local mirror, and says so.

Hindsight Cloud uses a different host and requires authentication; a 401 is
reported as `auth_failure` with a hint, not as a generic failure.

## Why a bank per organisation

The bank holds **durable organisational learning** (confirmed causes, failed and
successful actions, service patterns, runbook lessons, engineer corrections) —
not incident state. Incident state lives in the application database, so a reset
of the demo never destroys the memory bank unless you explicitly ask for it
(`HINDSIGHT` bank deletion is opt-in via `scripts/reset_demo.py --keep-memory`).
