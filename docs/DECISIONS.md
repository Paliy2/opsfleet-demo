# Design Decisions

Agreed decisions for the OpsFleet Data Analysis Chat Assistant. Source of truth for the HLD and prototype.

## 1. Client Clarifications (OpsFleet answers)

| # | Topic | Answer |
|---|---|---|
| 1 | Data access | Mock access table: manager → one or more brands. CEO sees everything. Customer/order data scoped only through in-scope products. |
| 2 | PII scope | Name, email, street address, postal code, exact coordinates never in output. City, state, country, age, gender, traffic source OK in aggregates. Stable pseudonymous ID OK for multi-turn follow-ups. |
| 3 | PII & LLM | PII must never reach the model — masked before data is sent to the LLM. |
| 4 | Report deletion | Hard delete, no recovery window. |
| 5 | Report ownership | Only the author can view, modify, or delete their reports. No admin override. |
| 6 | LLM | Gemini preferred (graders use free Gemini key). Fallback between Gemini models only; other providers in config, off by default. Document trade-off. |
| 7 | Load & latency | Tens–few hundred execs, ~100 active/day × ~10 questions, low concurrency. Simple answers 10–30s, reports 1–2 min. |

## 2. Architecture Style

**Modular monolith**: one Python API service + Temporal workers, with strict internal module boundaries (each module splittable later). Microservices are unjustified at ~1K questions/day.

## 3. Core Components

| Component | Technology | Responsibility |
|---|---|---|
| Clients | CLI (prototype); Slack/Web later | Thin clients; no business logic. |
| API Gateway | Kong or APISIX | JWT validation, per-user rate limits, request limits. Portable (no cloud lock-in). |
| Identity | Keycloak | OIDC authN + coarse roles (`ceo`, `manager`). CLI uses device flow; dev mode bypass for graders. |
| Agent API | FastAPI + LangGraph (thin: own nodes, own context pruning, no LangChain chains) | Orchestration, tool registry, SSE progress streaming, `interrupt()` for human-in-the-loop. |
| Guardrails | Topic classifier (Flash-Lite) + prompt-injection check + Google Sensitive Data Protection | Input/output safety layers. |
| Data Access Adapter | `QueryEngine` interface → `BigQueryEngine` | Single path to data: CTE scoping + masking → dry-run validation → execute with timeout & `maximum_bytes_billed`. |
| Conversation / Message History | Module + Postgres | Sessions, turns, tool calls; context windowing/summarization. Stored PII-free. |
| Report Service | Module + Postgres | Report CRUD; owner-only enforced in repository layer; audit log (metadata only). |
| User Memory | Module + Postgres | Learned + explicit preferences (format, depth, charts vs text). |
| Golden Retrieval | pgvector + Gemini embeddings | Hybrid (vector + full-text) search over golden trios as few-shot examples. |
| LLM Gateway | Bifrost (OpenAI-compatible) | Shared by 3 LLM consumers: chat API, Temporal report workers, offline eval cron jobs. Gemini fallback chain (Pro → Flash → Flash-Lite), retries, shared circuit breaker, central keys, budgets, rate limits. |
| Observability & Prompts | Langfuse | Traces, cost/latency, eval datasets; versioned prompts and agent configs. |
| Durable Workflows | Temporal | Long reports, destructive-op confirmation, golden ingestion, learning loop, offline evals. |
| Data Stores | Cloud SQL Postgres (+pgvector), GCS, BigQuery, Secret Manager | App data + vectors; raw trios & chart files; read-only source; salts & keys. |

Entitlements (manager → brands) live in the app DB, not in token claims (avoids stale/bloated tokens).

## 4. PII Masking (mask, don't remove)

Agent must reason about entities without knowing real personal data. Masking is **deterministic** (HMAC with secret salt) so the same person maps to the same surrogate across queries and turns.

| Field | Strategy |
|---|---|
| Name | Deterministic surrogate (clearly-marked pseudonym, e.g. `Customer_7f3a`, or seeded fake name tagged as masked) |
| Email | Surrogate local part, real domain (`u_7f3a@gmail.com`) |
| Street address | Surrogate |
| Postal code | Truncate to 3 digits |
| Lat/long | Generalize (round to ~0.1° / city centroid) — never random, it would corrupt geo analytics |
| `user_id` | HMAC token |

- **Prod:** copy dataset to own project; BigQuery dynamic data masking (policy tags + custom masking routines) — engine-enforced.
- **Prototype:** same masking functions applied in the scoped CTEs.
- **Free text** (user messages, golden trios): Google Sensitive Data Protection de-identify (deterministic crypto surrogates, date shifting, bucketing).
- Langfuse traces and logs contain only masked data.

## 5. Data Access Enforcement (no SQL parser)

1. **CTE shadowing** — adapter prepends `WITH users AS (<scoped+masked>), orders AS (...), order_items AS (...), products AS (...)`. LLM writes unqualified table names only.
2. **BigQuery dry-run validation** — `statement_type == SELECT`, `referenced_tables` ⊆ allowed 4, bytes estimate under limit.
3. **Reject qualified references** (dotted names / dataset name) to prevent CTE bypass.
4. **Read-only service account** (`bigquery.jobUser` + `dataViewer`) as last line of defense.

Order data scoped to in-scope `order_items` only — mixed-brand orders count in-scope items only (no leakage of other brands' revenue).

**Prod:** row access policies + authorized views on own dataset copy.

## 6. Destructive Ops (hard delete)

No undo → confirmation carries all risk:

1. **Plan** — resolve matching reports (owner-scoped), show count + titles + dates.
2. **Confirm** — Temporal workflow waits for a `confirm` signal with timeout; token bound to `(user_id, exact report IDs, expiry)`.
3. **Execute** — delete exactly the previewed IDs, never re-run the search (TOCTOU).
4. **Audit** — who/when/count only; no report content retained.

## 7. LLM & Agent Loop

- **Framework: LangGraph**, used thinly — custom nodes, own context pruning, no LangChain chains (avoid token bloat). `interrupt()` + checkpointer for destructive-op confirmation.
- **Why a gateway (not LangChain `with_fallbacks`):** three LLM consumers (chat API, Temporal report workers, eval cron jobs) need shared circuit-breaker state, central keys/budgets, and model changes without redeploying each. In-process fallback is per-replica and per-service.
- All consumers call an OpenAI-compatible `base_url` → Bifrost. Same code could point directly at Gemini's OpenAI-compatible endpoint if the gateway is removed.
- Graph enforces: max iterations, tool-error → model feedback, parallel tool calls, streaming, per-turn token/cost budget.
- SQL self-correction: bounded retries (2–3) on syntax errors / empty results within the 10–30s budget.
- Fallback within Gemini only. Trade-off: one SDK, consistent behavior, per-model free-tier quotas spread load; but provider-wide outage = full outage. Cross-provider fallback configurable, off by default.
- **To verify first (spike):** Gemini function calling (schemas, parallel calls, streaming) through Bifrost.

## 7a. Agent Topology (Orchestrator + agents-as-tools)

One **Orchestrator** agent loop (LangGraph) owns the conversation and calls specialists as tools:

| Tool | Kind | Notes |
|---|---|---|
| `data_analyst` | Sub-agent (own loop) | Schema + golden retrieval, SQL generation, bounded self-correction. Returns aggregates/summary + result handle, never raw rows to the orchestrator. |
| `report_writer` | Sub-agent | Persona + user prefs + action items. Sees only aggregates. Saves via Report Service. |
| `manage_reports` | **Deterministic workflow** exposed as a tool | LLM only supplies the filter; search → preview → `interrupt()` → delete exact IDs. Orchestrator cannot bypass confirmation. |
| `describe_schema` | Plain tool | Answers "what data is available". |

Rules:
- Input guard (topic + injection + PII redaction) runs **before** the orchestrator — not as an LLM tool.
- Sub-agents get isolated, minimal context (task brief, not full history) → token efficiency.
- Loop limits at both levels (orchestrator steps, sub-agent steps) + per-turn cost budget.
- Destructive actions are never decided by an agent — enforced inside the tool.
- A2A = documented extension point (e.g. external web-trends agent), not built.

## 8. Prompts & Configs (Langfuse)

Folder-style naming:

```
agents/analyst/system    # engineering-owned
agents/analyst/persona   # business-editable (CEO tone)
agents/analyst/config    # JSON config (prompt `config` field): model chain, limits
```

- Versioning via labels (`production`, `staging`); rollback = move label. No redeploy.
- **Layered composition:** immutable safety rules (in code) + editable persona (Langfuse) + engineering config (restricted via Langfuse RBAC). Tone edits can never override safety.
- **Resilience:** SDK client-side cache + last-known-good fallback in repo; Pydantic validation on load — invalid config keeps last-good and alerts.

## 9. Prototype vs Production

| Concern | Prototype | Production |
|---|---|---|
| Runtime | docker compose (Postgres+pgvector, Bifrost, Temporal dev server, API, CLI) | Cloud Run / GKE, Temporal Cloud or self-hosted |
| Identity | `--user` dev mode (Keycloak optional) | Keycloak + Kong/APISIX |
| PII masking | Masking in scoped CTEs | BigQuery dynamic data masking + SDP |
| Scoping | CTE shadowing + dry-run validation | Row access policies / authorized views |
| Observability | Langfuse Cloud if keys set, else local JSON traces | Langfuse (self-hosted or cloud) |
| Prompts | Langfuse, fallback to local files | Langfuse |

## 10. Bonus Scope (beyond the task)

| Bonus | Status | Why |
|---|---|---|
| Chart tool (Plotly → PNG/HTML in reports) | **Build** | Task names graphs as an extension; proves tool extensibility. |
| Eval harness + red-team suite (golden Q&A, PII leak, prompt injection, scope bypass) → Langfuse | **Build** | Req 6 + JD "System Evaluations". Highest signal per hour. |
| Chainlit Web UI | **Build** | Visual agent steps, charts, confirmation buttons. Second client of the same API. |
| Cloud Run deployment on own GCP (with documented steps) | **Build** | Live demo; closes GCP gap. |
| MCP server | Skipped | — |
| Voice channel (LiveKit) | Skipped | Overkill for scope. |

## 11. Deployment Platform (GKE Autopilot)

All services on **GKE Autopilot**, Terraform + Helm. Local: docker compose with profiles `core` (Postgres, Bifrost, API, CLI — Gemini key only) and `full` (+ Temporal, Keycloak, Langfuse, Chainlit, Redis).

| Piece | Deployed as |
|---|---|
| api, ui (Chainlit), bifrost, temporal-worker, eval CronJob | Deployments / CronJob |
| Temporal | Official Helm chart |
| Langfuse v3 (self-hosted) | Official Helm chart; ClickHouse StatefulSet; blobs in GCS |
| Keycloak | Operator / Helm |
| Postgres | Cloud SQL — DBs: `app` (+pgvector), `temporal`, `keycloak`, `langfuse` |
| Secrets | Secret Manager via Workload Identity |
| Ingress | GCE Ingress + managed TLS (UI, API, Keycloak) |
| Safety on public URL | Per-user rate limit, Bifrost daily budget cap, GCP billing alert |

Alternative noted in HLD: Cloud Run for stateless services once platform pieces are managed (Temporal Cloud, Langfuse Cloud).

## 12. Redis

Two instances — eviction policies conflict:

| Instance | Policy | Used for |
|---|---|---|
| `redis-langfuse` (in-cluster) | `noeviction` (Langfuse requirement) | Langfuse queues/cache only |
| `redis-app` (Memorystore or in-cluster) | `allkeys-lru` | Per-user rate limiting; BigQuery result cache; Bifrost exact-match response cache; prompt/config cache (L2 behind SDK in-memory) |

Rules:
- **Cache keys include scope:** `hash(normalized_sql, brand_scope, masking_version)` — never share cached results across differing entitlements.
- Only masked results are cached.
- BQ result TTL tied to source freshness (table `last_modified_time` in key) — dataset regenerates daily.
- **No semantic LLM cache** — "Q1 2025" vs "Q1 2026" are semantically near but different answers. Exact-match only.
- Not in Redis: LangGraph checkpoints, confirmation state, history → Postgres (durable).

## 13. Golden Bucket (seed data)

- **Option B:** LLM-generate ~50 trios (Question → SQL → Analyst Report); **execute every SQL on BigQuery** and keep only those that run and return non-empty results.
- Generator prompt embeds a business glossary (revenue excludes Cancelled/Returned, churn = no order in 90 days, underspending = AOV below state median) so trios carry analyst interpretation.
- Storage: raw JSON in GCS `golden/{id}.json` → Temporal ingestion workflow (SDP scrub → embed question + report summary → pgvector).
- Retrieval: hybrid (vector + Postgres full-text on table/metric names), top-3 as few-shot examples for `data_analyst`.
- Growth: 👍-rated reports + analyst approval → promoted to golden.

## 14. Models & Keys

| Role | `free-tier` (local default) | `production` (live demo) |
|---|---|---|
| Guard / topic check | Flash-Lite | Flash-Lite |
| Orchestrator | Flash | Flash |
| Data Analyst | Flash | Pro |
| Report Writer | Flash | Pro |
| Eval judge | Flash | Pro |
| Fallback chain | Flash → Flash-Lite | Pro → Flash → Flash-Lite |

- Role→model map lives in Langfuse agent config; model IDs never in code.
- Bifrost per-model token-bucket limits; 429 → fallback to next model (separate quota). Throttled eval runner with `--sample N`.
- **Keys:** live GKE demo uses owner's paid key from Secret Manager only (Bifrost daily budget cap + billing alert). Local default = reviewer's own free key. Optional capped evaluation key on request (separate project, API-restricted, daily quota, rotated after review, shared via expiring secret link — never email/repo).
- Spike result (docs/spikes/bifrost-gemini.md): Bifrost preserves Gemini 3 thought signatures across tool-call turns (direct OpenAI-compat + LangChain breaks with 400). Don't rely on unique tool-call IDs.

## 15. Prior Work Policy

- Prior production systems are used as **design references only** — no files, snippets, or utilities copied. All code written fresh.
- Deploy to a **personal GCP project** only.
- Pre-commit hook with a denylist scan (company/client/project names kept in a local, git-ignored file) blocks accidental leaks.

## 16. Context Management

Budgets are **char-based** (tokens can't be known before the call; ~4 chars/token proxy, conservative for SQL/tables).

| Layer | Contents | Budget |
|---|---|---|
| Fixed prefix | Safety rules + schema + glossary — Gemini context cache | ~12K chars |
| Persona + user prefs | Langfuse + memory service | ~2K chars |
| Rolling summary | Turns older than window, Flash-Lite, incremental, Postgres | ~3K chars |
| Recent turns | Newest-first, **up to K=20 turns OR ~16–20K chars**, whichever hits first | ~20K chars |
| Analysis state | Structured: metric, filters, period, entities, result refs | ~1K chars |
| Golden trios (analyst only) | **Top-6** above a min similarity score; each trio's report trimmed to summary | ~10K chars |

- Query results never inlined: stored as `result_id` artifacts, LLM sees ≤20-row preview + column stats, `get_result(result_id, page)` for more.
- Sub-agents receive a brief (`task, analysis_state, constraints`), not full history.
- "Reports made in this conversation" resolved via `reports.session_id`, not LLM memory.
- Char→token ratio calibrated from actual `usage.prompt_tokens` returned by each call; per-layer sizes logged to Langfuse.

## 17. Learning Loop

**User level** — typed preference schema (`format: table|bullets|prose`, `depth: brief|standard|deep`, `visuals: charts|text`, `language`, ≤5 free-text notes).
- Explicit: `update_preference` tool, applied immediately.
- Implicit: nightly Temporal Schedule; Flash extracts preference deltas from the day's sessions (signals: "shorter", re-asks, chart requests, 👍/👎); applied only after ≥2 consistent signals.
- Transparent: "what do you know about me?" / "reset my preferences".

**System level** — never self-modifies production without an eval gate.
1. Golden promotion: 👍 + analyst approval → trio (see §13).
2. SQL lessons store: recurring self-corrections become retrievable "lessons" for the analyst.
3. Failure mining: weekly job clusters failed Langfuse traces (SQL errors, empty results, 👎, guardrail blocks) → report → human edits prompt/glossary as Langfuse `staging` version → eval suite must pass → label promoted to `production`.

## 18. Evaluation & QA

Langfuse datasets + experiments, four layers:

| Layer | Checks | Method | Size |
|---|---|---|---|
| SQL correctness | Right numbers | Execution-result comparison vs reference SQL (tolerance), not SQL text | ~30 |
| Trajectory / tool use | Right tools, order, step budget; `manage_reports` goes preview → interrupt | Deterministic assertions on trace | ~15 |
| Report quality / intent | Answers the ask, grounded, actionable, persona + prefs | LLM-as-judge rubric (1–5) + groundedness: every number must appear in tool results | ~15 |
| Red team (100% required) | PII leak, prompt injection, scope bypass (other brand, CTE bypass), cross-user delete, off-topic, unconfirmed destructive op | Deterministic: output scan, row-scope check, DB state | ~25 |

- Gates: PR CI = layers 1, 2, 4 (sampled); prompt label promotion = full suite; nightly = full + drift report. Any red-team failure blocks release.
- Judge on a different model tier than the writer; calibrated against 10 human-scored cases, agreement rate reported.
- UX: online (👍/👎, rephrase rate, abandonment, TTFT, correction rate) collected by prototype; offline task-based exec study = design only.

## 19. Observability

- OTel SDK → OTel Collector → Prometheus (metrics), Loki (logs), Langfuse (LLM traces), Tempo (infra spans); Grafana dashboards. One `trace_id` across API → Temporal → Bifrost → Langfuse; JSON logs carry `trace_id`, `session_id`, `user_hash`.
- Metrics: service (rate/errors/p50-p95 per intent, TTFT), agent (success, steps/turn, loop-limit hits, tool error rate, SQL self-correction rate + success-after-retry, empty-result, give-up), LLM (tokens/cost per turn/user/model, fallback rate, 429s, breaker state, Gemini cache hits), safety (guardrail blocks, post-masking PII hits, scope-bypass attempts, destructive-op funnel), quality (👍/👎, 5% sampled online judge, groundedness failures), BigQuery (bytes, latency, dry-run rejects).
- Alert rules defined as `PrometheusRule` (SLO burn 99.5% / p95 < 30s, post-mask PII hit = P1, fallback > 20%, daily cost, nightly red-team fail). **No notification receivers for now** (no Slack); alerts visible in Alertmanager/Grafana.
- Debugging: error taxonomy codes on every span (`E_SQL_SYNTAX`, `E_EMPTY_RESULT`, `E_SCOPE_DENIED`, `E_LLM_TIMEOUT`, `E_LOOP_LIMIT`, …); prompt + config version on every trace; `opsfleet debug <trace_id>` CLI dumps conversation, prompt versions, SQL attempts, errors, fallbacks.

## 20. Persona Management

- Editing surface: Langfuse `agents/analyst/persona`; dedicated **persona-editor** role sees only persona prompts.
- New version → automatic gate (Langfuse prompt-change webhook or polling): length cap, config Pydantic validation, fast red-team subset (~5 injection cases, < 2 min).
- Pass → `production` label auto-promoted; fail → stays `staging`, reason shown to editor.
- Propagation via SDK cache TTL 60s — no redeploy. Rollback = move label. Persona version tagged on every trace.
- Safety rules live in code and are composed first; the gate proves tone edits cannot weaken them.

## 21. Demo Identities & Auth

| User | Role | Scope | Demonstrates |
|---|---|---|---|
| `ceo` | ceo | all brands | Full picture, company-wide reports |
| `mgr_a` | manager | 3 high-revenue brands | Normal scoped analysis |
| `mgr_b` | manager | 3 other brands, one shared with `mgr_a` | Overlap; same question → different totals |
| `mgr_empty` | manager | low-data brand | Graceful empty-result handling |

- Brands chosen by a seed script from BigQuery revenue ranking → `entitlements` table in Postgres.
- Auth: CLI `--user` dev mode (`core`, `ENV=local` only) / Keycloak device flow (`full`, GKE). Chainlit via Keycloak OAuth. API validates Keycloak JWT (JWKS); roles from token, brands from DB.
- Scripted demo: cross-brand refusal → masked top customers → "delete reports we made today" with preview + confirm → `mgr_b` cannot see `mgr_a`'s reports.

## 22. Build Order

| # | Milestone | Covers |
|---|---|---|
| **H0 (human)** | Personal GCP project + billing, separate `gcloud` configuration set as active, ADC login, enable APIs | Prerequisite |
| **M0** | `CLAUDE.md` with code-quality rules, repo skeleton (hexagonal layout, §23), `uv`, ruff/mypy/pytest, pre-commit + denylist, compose `core`, re-run Bifrost spike with real key | Foundation |
| M1 | Data adapter: CTE scoping, masking, dry-run validation, cost caps + unit/red-team tests | Req 2 |
| M2 | LangGraph orchestrator + `data_analyst` self-correction + CLI + streaming | Req 5, core |
| M3 | Report service + `report_writer` + `manage_reports` with `interrupt()` + charts | Req 3 (all 4 prototype reqs done) |
| M4 | Langfuse tracing + prompts/configs, error taxonomy, `/metrics`, `debug` CLI | Req 7, 8 |
| M5 | Golden trios + hybrid retrieval + context layers + preferences | Req 1, 4 |
| M6 | Eval harness (4 layers) + red-team suite in CI | Req 6 |
| M7 | HLD diagrams + technical write-up + README — drafted in parallel from M2 | Deliverables 1, 2 |
| M8 | Compose `full`: Temporal workers, Keycloak, Chainlit | Production |
| M9 | Terraform + Helm on GKE (personal project), live URL | Deployment |

Rules: every milestone from M2 on is submittable; docs in parallel; infra last; in-process `WorkflowRunner` until M8 swaps in Temporal.

## 23. Code Layout (Hexagonal)

```
src/opsfleet_agent/
  api/          # inbound: FastAPI routers, SSE, auth deps
  cli/          # inbound: Typer CLI (ui/ for Chainlit later)
  entities/     # Pydantic DTOs + domain types (no I/O)
  models/       # SQLAlchemy models
  interactors/  # use cases (AskQuestion, PlanDeletion, ConfirmDeletion, SaveReport, UpdatePreference)
  ports/        # Protocols: QueryEngine, LLMClient, ReportRepo, WorkflowRunner, ...
  repos/        # Postgres CRUD adapters
  gateways/     # non-DB outbound adapters: bigquery/, llm/, langfuse/, temporal/, dlp/
  agents/       # LangGraph graphs + thin tools that call interactors
  core/         # config, logging, telemetry, error taxonomy
workers/        # Temporal worker entrypoints
evals/          # datasets, runners, red-team
infra/          # terraform/, helm/, compose
docs/
```

- Dependencies point inward: `interactors` import only `ports` + `entities`. No SQLAlchemy/LangGraph/BigQuery there.
- Business rules (ownership, deletion confirmation, scoping) live in `interactors`, never in agent tools.
- Enforced in CI via import-linter contracts.

## 24. Schedule (deadline: Sunday 2026-10-11)

| Day | Scope | Exit criterion |
|---|---|---|
| Thu 10-08 | H0, M0, M1 | `make check` green; scoped + masked queries proven by red-team tests |
| Fri 10-09 | M2, M3; HLD diagrams drafted | All 4 prototype requirements demoable in CLI (**submittable floor**) |
| Sat 10-10 | M4, M5, M6; write-up drafted | Langfuse traces/prompts, golden retrieval, eval + red-team suite passing |
| Sun 10-11 AM | M7 final docs + README + demo recording | Submission-ready by noon |
| Sun 10-11 PM | M8 / M9 only if time remains | Submit by evening with buffer |

Cut order if behind: (1) M9 → Terraform + Helm written and validated (`terraform validate`, `helm template`), not applied; (2) M8 → Keycloak/Temporal described in HLD, `WorkflowRunner` stays in-process; (3) eval suite → red team + SQL correctness only. **M7 docs are never cut.**

## 25. Data Profile (verified 2026-10-08)

- Rows: users 100,000 · orders 124,987 · order_items 181,162 · products 29,120. Orders span 2019-01-09 → today.
- `order_items.status`: Shipped 54.5K, Complete 45.3K, Processing 36.4K, Cancelled 26.9K, Returned 18.1K.
- **1,706 order_items have `created_at` in the future** (orders do not) → time anchor = `CURRENT_DATE()`, items filtered `created_at <= CURRENT_TIMESTAMP()`.
- 2,756 brands, 26 categories, 2 departments. Top brands by net revenue: Diesel, Calvin Klein, 7 For All Mankind, Carhartt, True Religion, Tommy Hilfiger.
- 16 country values, **dirty**: `Spain`/`España`, `Germany`/`Deutschland`, `Brasil` → normalization map in glossary + golden trios.
- `state` is international (China 32, US 51, Brasil 27 …) → "state X" must be disambiguated by country.

## 26. Error Classification (from live spike)

- Billing/credential errors (Bifrost `upstream_credentials_exhausted`, Gemini `RESOURCE_EXHAUSTED` with billing message, 401/402/403) are **non-retryable and skip fallback** → `E_LLM_UNAVAILABLE` + ops alert "LLM billing/credentials". All models share project billing.
- 429 rate limits and 5xx/timeouts → retry with backoff, then next model in chain.
- Confirmed models (paid key): `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`; Pro = `gemini-3.1-pro-preview`.
