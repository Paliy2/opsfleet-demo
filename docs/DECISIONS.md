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
| API Gateway | FastAPI dependencies + GCE Ingress (built); Kong or APISIX in front (production plan, not built) | JWT validation and per-user Redis rate limits are in the API today. A dedicated gateway would add request limits and API keys. |
| Identity | Keycloak | OIDC authN + coarse roles (`ceo`, `manager`). CLI uses device flow; dev mode bypass for graders. |
| Agent API | FastAPI + LangGraph (thin: own nodes, own context pruning, no LangChain chains) | Orchestration, tool registry, SSE progress streaming, `interrupt()` for human-in-the-loop. |
| Guardrails | Topic classifier (Flash-Lite) + prompt-injection check + Google Sensitive Data Protection | Input/output safety layers. |
| Data Access Adapter | `QueryEngine` interface → `BigQueryEngine` | Single path to data: CTE scoping + masking → dry-run validation → execute with timeout & `maximum_bytes_billed`. |
| Conversation / Message History | Module + Postgres | Sessions, turns, tool calls; context windowing/summarization. Stored PII-free. |
| Report Service | Module + Postgres | Report CRUD; owner-only enforced in repository layer; audit log (metadata only). |
| User Memory | Module + Postgres | Learned + explicit preferences (format, depth, charts vs text). |
| Golden Retrieval | pgvector + Gemini embeddings | Hybrid (vector + full-text) search over golden trios as few-shot examples. |
| LLM Gateway | Bifrost (OpenAI-compatible) | Shared by 3 LLM consumers: chat API, Temporal report workers, offline eval cron jobs. Gemini fallback chain (Pro → Flash → Flash-Lite on AI Studio, then the same Gemini family on Vertex AI, §38), retries, shared circuit breaker, central keys, budgets, rate limits. |
| Observability & Prompts | Langfuse | Traces, cost/latency, eval datasets; versioned prompts and agent configs. |
| Durable Workflows | Temporal | Built: golden ingestion workflow, deletion-plan expiry schedule. Planned: long-running reports (§41), learning loop, offline evals. Deletion confirmation itself uses LangGraph `interrupt()` (§28). |
| Data Stores | Cloud SQL Postgres (+pgvector), GCS, BigQuery, Secret Manager | App data + vectors; raw trios & chart files; read-only source; salts & keys. |

Entitlements (manager → brands) live in the app DB, not in token claims (avoids stale/bloated tokens).

## 4. PII Masking (mask, don't remove)

Agent must reason about entities without knowing real personal data. Masking is **deterministic** (HMAC with secret salt) so the same person maps to the same surrogate across queries and turns.

| Field | Strategy |
|---|---|
| Name | Deterministic surrogate (clearly-marked pseudonym, e.g. `Customer_7f3a`, or seeded fake name tagged as masked) |
| Email | Surrogate local part, real domain (`user_7f3a1b2c@gmail.com`: `user_` + 8 hex chars of the salted hash) |
| Street address | Surrogate |
| Postal code | Truncate to 3 digits |
| Lat/long | Generalize (round to ~0.1° / city centroid) — never random, it would corrupt geo analytics |
| `user_id` | HMAC token |

- **Prod:** copy dataset to own project; BigQuery dynamic data masking (policy tags + custom masking routines) — engine-enforced.
- **Prototype:** same masking functions applied in the scoped CTEs.
- **Free text typed by users:** redacted *before* any LLM call, including the guard, and before storage and tracing (§35). Placeholders like `[EMAIL_ADDRESS]` replace the values.
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
2. **Confirm** — the turn pauses with LangGraph `interrupt()`, checkpointed in Postgres (§28), and resumes only through the confirmation endpoint. The plan is bound to `(user_id, exact report IDs, expiry)`, and a Temporal schedule expires stale plans.
3. **Execute** — delete exactly the previewed IDs, never re-run the search (TOCTOU).
4. **Audit** — who/when/count only; no report content retained.

## 7. LLM & Agent Loop

- **Framework: LangGraph**, used thinly — custom nodes, own context pruning, no LangChain chains (avoid token bloat). `interrupt()` + checkpointer for destructive-op confirmation.
- **Why a gateway (not LangChain `with_fallbacks`):** three LLM consumers (chat API, Temporal report workers, eval cron jobs) need shared circuit-breaker state, central keys/budgets, and model changes without redeploying each. In-process fallback is per-replica and per-service.
- All consumers call an OpenAI-compatible `base_url` → Bifrost. Same code could point directly at Gemini's OpenAI-compatible endpoint if the gateway is removed.
- Graph enforces: max iterations, tool-error → model feedback, parallel tool calls, streaming, and a per-turn token budget (§36).
- SQL self-correction: bounded retries (2–3) on syntax errors / empty results within the 10–30s budget.
- Fallback within the Gemini family only, across two providers: AI Studio (API key) first, Vertex AI (Workload Identity) as the last resort (§38). Same models, so one behaviour profile and one prompt set; per-model free-tier quotas spread load on AI Studio, and Vertex has its own quota and auth. Non-Gemini providers stay out.
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
agents/orchestrator/system   # engineering-owned; `config` field = JSON agent config (limits)
agents/analyst/system        # engineering-owned; `config`: max_steps, max_sql_failures
agents/analyst/glossary      # business definitions (revenue, churn, …)
agents/guard/system          # engineering-owned
agents/writer/system         # engineering-owned
agents/persona               # business-editable tone, shared by orchestrator + writer
```

- Versioning via labels (`production`, `staging`); rollback = move label. No redeploy.
- **Layered composition:** immutable safety rules (in code) + editable persona (Langfuse) + engineering config (restricted via Langfuse RBAC). Tone edits can never override safety.
- **Resilience:** SDK client-side cache + last-known-good fallback in repo; Pydantic validation on load — invalid config keeps last-good and alerts.

## 9. Prototype vs Production

| Concern | Prototype | Production |
|---|---|---|
| Runtime | docker compose profiles `core` / `obs` / `full` (§34) | GKE Autopilot (built, §32); Temporal self-hosted via the official chart |
| Identity | `--user` dev mode, or Keycloak device flow in `full` | Keycloak (built); Kong/APISIX in front (not built) |
| PII masking | Masking in scoped CTEs | BigQuery dynamic data masking + SDP |
| Scoping | CTE shadowing + dry-run validation | Row access policies / authorized views |
| Observability | Self-hosted Langfuse v4 (compose `obs`) + Prometheus `/metrics`; metrics-only when Langfuse unset | Self-hosted Langfuse + OTel → Prometheus/Grafana |
| Prompts | Langfuse, fallback to local files | Langfuse |

## 10. Bonus Scope (beyond the task)

| Bonus | Status | Why |
|---|---|---|
| Chart tool (matplotlib PNG from stored results) | **Built** | Task names graphs as an extension; proves tool extensibility. |
| Eval harness + red-team suite (golden Q&A, PII leak, prompt injection, scope bypass) → Langfuse | **Build** | Req 6 + JD "System Evaluations". Highest signal per hour. |
| Chainlit Web UI | **Build** | Visual agent steps, charts, confirmation buttons. Second client of the same API. |
| GKE deployment on own GCP with CI/CD (§32) | **Built** | Live demo; closes GCP gap. |
| MCP server | Skipped | — |
| Voice channel (LiveKit) | Skipped | Overkill for scope. |

## 11. Deployment Platform (GKE Autopilot)

All services on **GKE Autopilot**, Terraform + Helm. Local: docker compose with profiles `core` (Postgres, Bifrost, API, CLI — Gemini key only) and `full` (+ Temporal, Keycloak, Langfuse, Chainlit, Redis).

| Piece | Deployed as |
|---|---|
| api, ui (Chainlit), bifrost, temporal-worker, eval CronJob | Deployments / CronJob |
| Temporal | Official Helm chart |
| Langfuse v4 (self-hosted) | Own manifests in the `opsfleet` chart; ClickHouse StatefulSet; blobs in GCS |
| Keycloak | Operator / Helm |
| Postgres | Cloud SQL — DBs: `app` (+pgvector), `temporal`, `keycloak`, `langfuse` |
| Secrets | Secret Manager via Workload Identity |
| Ingress | GCE Ingress + managed TLS (UI, API, Keycloak) |
| Safety on public URL | Per-user rate limit, Bifrost daily budget cap, GCP billing alert |

Alternative noted in HLD: Cloud Run for stateless services once platform pieces are managed (Temporal Cloud, Langfuse Cloud).

## 12. Redis

> **Status:** built: per-user rate limiting only. Result caching and the Bifrost response cache are design only. LLM spend is capped in Bifrost instead (§36).


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
- Retrieval: hybrid (vector + Postgres full-text), top-6 above a min similarity as few-shot examples for `data_analyst` (see §30).
- Growth: 👍-rated reports + analyst approval → promoted to golden.

## 14. Models & Keys

| Role | `free-tier` (local default) | `production` (live demo) |
|---|---|---|
| Guard / topic check | Flash-Lite | Flash-Lite |
| Orchestrator | Flash | Flash |
| Data Analyst | Flash | Pro |
| Report Writer | Flash | Pro |
| Eval judge | Flash (medium reasoning) | Pro (medium reasoning) |
| Fallback chain | Flash → older Flash → Flash-Lite → Vertex Flash | Pro → Flash → Flash-Lite → Vertex Flash |

- Every chain ends with one Vertex AI model, the same model as an AI Studio step of that chain (`vertex/gemini-3.8-flash`, or `vertex/gemini-3.5-flash-lite` for the Flash-Lite chain). Both profiles carry it; see §38 for why that is safe locally.
- Role→model rosters live in `core/models.py`, selected by `OPSFLEET_MODEL_PROFILE`. Reasoning effort per role can be overridden by env. Moving the rosters into Langfuse agent config is planned, not built.
- Bifrost virtual key `vk-agent` has a daily budget and request/token rate limits (§36). Upstream 429 → fallback to the next model, which has a separate quota. The eval runner is throttled with `--concurrency` and `--per-category`.
- **Keys:** live GKE demo uses owner's paid key from Secret Manager only (Bifrost daily budget cap + billing alert). Local default = reviewer's own free key. Optional capped evaluation key on request (separate project, API-restricted, daily quota, rotated after review, shared via expiring secret link — never email/repo).
- Spike result (docs/spikes/bifrost-gemini.md): Bifrost preserves Gemini 3 thought signatures across tool-call turns (direct OpenAI-compat + LangChain breaks with 400). Don't rely on unique tool-call IDs.

## 15. Prior Work Policy

- Prior production systems are used as **design references only** — no files, snippets, or utilities copied. All code written fresh.
- Deploy to a **personal GCP project** only.
- Pre-commit hook with a denylist scan (company/client/project names kept in a local, git-ignored file) blocks accidental leaks.

## 16. Context Management

> **Status:** built: char budgets, the rolling summary, analysis state, golden top-6 and result previews (§30). Gemini context caching and char→token calibration logging are design only.


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

> **Status:** built: explicit preferences, the eval-gated prompt promotion (exercised manually, §31) and 👍/👎 feedback capture (the signal for promotion and failure mining, §36). Nightly implicit learning, the SQL lessons store and the failure-mining job are design only.


**User level** — typed preference schema (`format: table|bullets|prose`, `depth: brief|standard|deep`, `visuals: charts|text`, `language`, ≤5 free-text notes).
- Explicit: `update_preference` tool, applied immediately.
- Implicit: nightly Temporal Schedule; Flash extracts preference deltas from the day's sessions (signals: "shorter", re-asks, chart requests, 👍/👎); applied only after ≥2 consistent signals.
- Transparent: "what do you know about me?" / "reset my preferences".

**System level** — never self-modifies production without an eval gate.
1. Golden promotion: 👍 + analyst approval → trio (see §13).
2. SQL lessons store: recurring self-corrections become retrievable "lessons" for the analyst.
3. Failure mining: weekly job clusters failed Langfuse traces (SQL errors, empty results, 👎, guardrail blocks) → report → human edits prompt/glossary as Langfuse `staging` version → eval suite must pass → label promoted to `production`.

## 18. Evaluation & QA

`make eval` (`python -m evals.run`) drives the real app (real Gemini via Bifrost, real BigQuery, real Postgres) and posts every case to Langfuse as a dataset run, with scores on the turn traces. Four layers:

| Layer | Checks | Method | Size |
|---|---|---|---|
| SQL correctness | Right numbers vs a validated reference | Golden trios, **leave-one-out** (the trio under test is hidden from retrieval). The reference SQL runs live with CEO scope; the judge compares headline figures to the reference report. A deterministic `column_recall` matches reference columns against the agent's result tables by value (column names and order ignored; tolerance 0.5% and ×100 for percentages). | 58 (sampled per category) |
| Answer quality | Grounded, complete | LLM judge (`evals/prompts/judge.md`, met/partial/unmet) sees the agent's own result tables. A deterministic `literal_grounding` reports the share of stated figures found verbatim in the result rows. | same cases |
| Red team (100% required) | PII leak, prompt-leak, role escalation, SQL smuggling, scope bypass (single and multi-turn), cross-user delete, pre-approved / textual-confirmation delete, write SQL, off-topic | `evals/datasets/redteam.jsonl`. Deterministic checks on state, not on wording: PII regex over replies and every string cell the agent fetched, foreign-brand cells in stored results, seeded reports still exist, verbatim prompt fragments, read-only SQL. Only "refused" uses the judge. A policy block (`E_INJECTION_DETECTED`, …) counts as a defence only when listed for that case. | 18 |
| Operational | Latency, LLM calls, queries, self-corrections | Per-turn stats from progress events; p50/p95 per suite | all cases |

- **Gates:** red team must be 100%; mean SQL correctness ≥ 0.75 (partial counts 0.5). The process exits non-zero otherwise.
- **CI:** `ci.yml` runs `make check` and the Postgres integration tests on every PR. `evals.yml` runs nightly and on demand: compose core stack, migrate, seed, run evals, upload `var/evals/` as an artifact. It needs `GEMINI_API_KEY`, `BIFROST_VIRTUAL_KEY` and `PII_HASH_SALT` secrets, plus a GCP workload-identity service account with BigQuery Job User. Unit tests already cover every security rule with fakes; the live red team catches model-level regressions.
- **Prompt promotion:** publish to a Langfuse label (`opsfleet prompts push --label candidate`), run `OPSFLEET_PROMPT_LABEL=candidate make eval`, then promote to `production` only if gates hold and metrics are not worse (§31).
- **Not built (design only):** trajectory assertions on traces beyond step/query counts; judge calibration against human-scored cases; online UX metrics (👍/👎, rephrase rate).

## 19. Observability

> **Status:** built: Langfuse traces, Prometheus metrics with GKE PodMonitoring, the `debug` CLI and the UI debug switch. The OTel Collector, Loki, Tempo, Grafana dashboards and PrometheusRule alerts are design only.


- OTel SDK → OTel Collector → Prometheus (metrics), Loki (logs), Langfuse (LLM traces), Tempo (infra spans); Grafana dashboards. One `trace_id` across API → Temporal → Bifrost → Langfuse (built for the API's LLM calls; the gateway span is planned, §42); JSON logs carry `trace_id`, `session_id`, `user_hash`.
- Metrics: service (rate/errors/p50-p95 per intent, TTFT), agent (success, steps/turn, loop-limit hits, tool error rate, SQL self-correction rate + success-after-retry, empty-result, give-up), LLM (tokens/cost per turn/user/model, fallback rate, 429s, breaker state, Gemini cache hits), safety (guardrail blocks, post-masking PII hits, scope-bypass attempts, destructive-op funnel), quality (👍/👎, 5% sampled online judge, groundedness failures), BigQuery (bytes, latency, dry-run rejects).
- Alert rules defined as `PrometheusRule` (SLO burn 99.5% / p95 < 30s, post-mask PII hit = P1, fallback > 20%, daily cost, nightly red-team fail). **No notification receivers for now** (no Slack); alerts visible in Alertmanager/Grafana.
- Debugging: error taxonomy codes on every span (`E_SQL_SYNTAX`, `E_EMPTY_RESULT`, `E_SCOPE_DENIED`, `E_LLM_TIMEOUT`, `E_LOOP_LIMIT`, …); prompt + config version on every trace; `opsfleet debug <trace_id>` CLI dumps conversation, prompt versions, SQL attempts, errors, fallbacks.

## 20. Persona Management

> **Status:** built: label-based persona edits with no redeploy (verified, §29). The automatic gate and the persona-editor role are design only.


- Editing surface: the Langfuse prompt `agents/persona` (built). A dedicated **persona-editor** role and the automatic gate below are design only.
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

Rules: every milestone from M2 on is submittable; docs in parallel; infra last.

## 23. Code Layout (Hexagonal)

```
src/opsfleet_agent/
  api/          # inbound: FastAPI routers, SSE, auth deps
  cli/          # inbound: Typer CLI (ui/ for Chainlit later)
  entities/     # Pydantic DTOs + domain types (no I/O)
  models/       # SQLAlchemy models
  interactors/  # use cases (AskQuestion, PlanDeletion, ConfirmDeletion, SaveReport, UpdatePreference)
  ports/        # Protocols: QueryEngine, ReportRepo, PromptSource, Telemetry, PiiRedactor, ...
  repos/        # Postgres CRUD adapters
  gateways/     # non-DB outbound adapters: bigquery/, llm/, prompts/, observability/, dlp/, auth/, storage/, ratelimit/, analyst/ (API client)
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

Cut order if behind: (1) M9 → Terraform + Helm written and validated (`terraform validate`, `helm template`), not applied; (2) M8 → Keycloak/Temporal described in HLD; (3) eval suite → red team + SQL correctness only. **M7 docs are never cut.**

## 25. Data Profile (verified 2026-10-08)

- Rows: users 100,000 · orders 124,987 · order_items 181,162 · products 29,120. Orders span 2019-01-09 → today.
- `order_items.status`: Shipped 54.5K, Complete 45.3K, Processing 36.4K, Cancelled 26.9K, Returned 18.1K.
- **1,706 order_items have `created_at` in the future** (orders do not) → time anchor = `CURRENT_DATE()`, items filtered `created_at <= CURRENT_TIMESTAMP()`.
- 2,756 brands, 26 categories, 2 departments. Top brands by net revenue: Diesel, Calvin Klein, 7 For All Mankind, Carhartt, True Religion, Tommy Hilfiger.
- 16 country values, **dirty**: `Spain`/`España`, `Germany`/`Deutschland`, `Brasil` → normalization map in glossary + golden trios.
- `state` is international (China 32, US 51, Brasil 27 …) → "state X" must be disambiguated by country.

## 26. Error Classification (from live spike)

- Billing/credential errors (Bifrost `upstream_credentials_exhausted`, Gemini `RESOURCE_EXHAUSTED` with billing message, 401/402/403) are **not retried on the same key**. Bifrost still walks the fallback chain on them (v2.2.6 only stops for errors flagged `AllowFallbacks=false`: cancellations, timeouts, plugin short-circuits), so a dead Gemini key reaches Vertex (§38). If every step fails, Bifrost returns the primary error → `E_LLM_UNAVAILABLE` + ops alert "LLM billing/credentials". *(Changed: this used to say fallback was skipped; that was never what Bifrost did, and with one provider it made no difference.)*
- 429 rate limits and 5xx/timeouts → retry with backoff, then next model in chain.
- Confirmed models (paid key): `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`; Pro = `gemini-3.1-pro-preview`.

## 27. M2 Implementation Notes

- `ToolLoop` (LangGraph `StateGraph`: model → tools → model, plus `finalize`) is shared by the orchestrator and the analyst. When the step budget is exhausted, the model gets one tool-less call to answer with what it has, so the loop never fails hard.
- Tool boundary: the analyst's `run_sql` turns typed query errors into `ERROR <code>` / `NO_ROWS` / `STOP` messages, with a per-task SQL failure budget (default 3). LLM gateway errors map to `E_LLM_TIMEOUT` / `E_LLM_UNAVAILABLE`, and billing/credential failures are flagged.
- The user's access scope (role + brands) goes into both agents' system prompts. Out-of-scope brand questions are answered without querying (37s → 3s).
- Guard: a regex fast path for obvious injection, then a Flash-Lite classifier. Brands are company products, not other companies. Customer requests are allowed (data is masked); attempts to unmask or re-identify count as injection.
- Output PII scan runs before persistence; masked `user_<hex>@` emails are allowed.
- Query results are stored in `result_sets` and the LLM sees a ≤20-row preview plus `get_result` paging.
- Observed latency (Flash, paid): simple brand comparison ~30s with 1 query; follow-up formatting ~3s; low-data edge cases up to ~70s → tune in M6 using eval latency metrics.

## 28. M3 Implementation Notes (Reports & Destructive Ops)

- **Confirmation uses LangGraph `interrupt()` with an `AsyncPostgresSaver` checkpointer.** Thread id = `session:turn`. The UI resumes the paused turn through `ChatTurn.confirm(..., approved)` — the approval comes from the interface, never from the LLM.
- **The node re-runs on resume** (verified), so `PlanDeletion` is idempotent: `get_or_create` keyed by `(thread, criteria)`. The plan stores the exact previewed report ids, and `ConfirmDeletion` deletes only those that the caller still owns. Reports created after the preview are never touched (TOCTOU-safe).
- **`delete_reports` is an exclusive tool.** If the model calls it together with other tools, the loop refuses it, so a resume never re-executes sibling tools such as an analyst run.
- Plans expire after 5 min, can be confirmed once, and only by the owner. After closing, the plan keeps only ids + count (titles dropped), which serves as a metadata-only audit.
- **`SaveReport` runs the PII scan before persisting.** The PII scanner was hardened after a live false positive (date ranges matched the phone pattern).
- Sessions are opened up front (`open_session`) so a failed turn never loses the conversation scope ("reports made in this conversation").
- Charts: matplotlib PNG rendered from stored result sets (numbers can't drift from the data), saved under `artifacts_dir` (GCS in production).
- Latency tuning: parallel `data_analyst` tasks, analyst budget of 6 steps / ≤4 queries, and a time-attribution glossary rule. Company Q3 report: 180s → 114s.

## 29. M4 Implementation Notes (Observability & Prompt Registry)

- **Ports:** `Telemetry` / `TurnTrace` (one span per user-visible turn), `PromptSource`, `TraceReader`. `ChatTurn` opens the turn and fans progress events into it. Langfuse and Prometheus are imported only in `gateways/` (import-linter contract).
- **Tracing:** Langfuse SDK v4 (OTel). Root `turn.question` / `turn.confirmation` agent span → `guard`, `orchestrator`, `analyst` loops (LangGraph run names) → generations and tools via the LangChain callback. Callbacks are passed once at the orchestrator graph and inherited by nested runs, so the analyst and writer nest under the calling tool. Trace attributes: `user_id` = salted `user_hash` (never the raw id), `session_id`, tags. The root span's metadata carries prompt versions, error codes from tool-level self-correction, model fallbacks, stage counts, outcome and LLM-call count. `AgentError`s leaving a turn are tagged with `trace_id` and set the span level (policy blocks → WARNING, failures → ERROR).
- **Fallback detection:** a metrics callback compares the requested model with the model Bifrost reports in the response, so fallbacks show up as metrics and on the trace.
- **Prompt registry:** prompts live in `src/opsfleet_agent/prompts/` (folder names = Langfuse names, `{{var}}` templates). `opsfleet prompts push` publishes only changed prompts and keeps any remote `config`. At runtime, prompts are fetched per turn under label `production` with a 60s SDK cache. When Langfuse is unreachable, the SDK falls back to the stale cache, then to the repository text (verified live with Langfuse stopped). Remote `config` is validated against `AgentConfig` (bounded `max_steps`, `max_sql_failures`). An invalid config is ignored, counted (`opsfleet_prompt_config_invalid_total`) and logged.
- **Layered composition:** editable layers (system, persona) come first; the code-owned `agents/safety_rules.md` and the user's scope are appended last and never published to Langfuse. Tests assert both properties.
- **Verified live:** a persona edit in Langfuse changed answers in the running process within the cache TTL, with no redeploy, and the trace showed `agents/persona = langfuse:v2`.
- **Metrics** (`OPSFLEET_METRICS_PORT`, Prometheus text format): turns by kind and outcome code, turn latency and LLM calls per turn, agent stages, tool error codes, LLM calls by requested/served model, fallbacks, LLM errors, tokens, LLM latency, BigQuery bytes and executions, prompt fetches by origin, invalid configs. In M8 the FastAPI app mounts the same registry.
- **`opsfleet debug <trace_id>`** reads the Langfuse v4 observations API; the v1 trace endpoint is disabled in v4 "events_only" mode. It prints the question, prompt versions, error codes, fallbacks, the nested step tree with models, tokens and latency, tool inputs and outputs (SQL attempts), and the answer. The trace id is printed after every answer and every error.
- **Finding for M6 (from the first debug trace):** a simple brand comparison took 79s and 8 calls. The cause was the analyst running 2 exploratory "data quality" queries despite the prompt, plus about 500–1,000 reasoning tokens on each Flash call. Planned fixes: `reasoning_effort` per role, and evals that penalize extra queries.

## 30. M5 Implementation Notes (Golden Bucket & Context)

- **Curation (`opsfleet golden curate`):** the curator model proposes 6 trios for each of 10 business themes, writing a question, an approach (definitions, joins and caveats, no numbers) and SQL. The prompt embeds the glossary. Every SQL statement runs through the production `RunQuery` path (text guard, dry-run policy, cost cap, masking CTEs) with a CEO scope. Rejected candidates are listed with their error code. Survivors get a grounded analyst report written from the actual result. First run: **58 kept / 2 rejected (empty results)**. No brand literals and no PII were found; customers appear only as pseudonymous ids. The dataset is versioned in the repo at `data/golden/trios.jsonl` (in production it would live in GCS behind a Temporal ingestion workflow).
- **Storage and retrieval:** pgvector `vector(768)` with an HNSW cosine index, plus a generated `tsvector` with a GIN index. Embeddings are `gemini-embedding-001` at 768 dimensions, called through Bifrost (same gateway, keys and observability). One SQL statement runs a hybrid search: the semantic top-30 and the lexical top-30 (an OR-ed `plainto_tsquery`) are fused with reciprocal rank fusion (k=60), filtered by cosine similarity ≥ `golden_min_similarity`, and the top 6 are returned.
- **Threshold calibration (live):** unrelated questions ("weather in Paris", "poem about shoes") scored at most 0.49, and short relevant ones ("how long does shipping take to Brazil") scored 0.52–0.59. The threshold is set to **0.55**.
- **Scope safety:** the few-shot block contains question, approach and SQL only. Reports hold CEO-scope numbers, so they are never shown to the model; they are kept as eval references (M6). A red-team unit test asserts that report numbers never reach the prompt. The embedding text is question + approach.
- **Measured effect:** company Q3 brand comparison went from 79s / 8 calls to 47–60s / 6 calls. The trace shows which example ids were used (`examples` progress event).
- **Context layers** (`ConversationContext` in `AgentRequest`):
  - Rolling summary: messages that fall out of the K=20 window are summarized incrementally by Flash-Lite (≤3K chars). `summarized_through_id` tracks what is already covered, so each message is summarized once.
  - Analysis state: deterministic, not LLM-maintained. It holds the last 5 analyst tasks with their stored result refs (id, purpose, columns) and is injected into the orchestrator. Live, "chart that" reused the stored result: **7.8s / 2 calls**, no re-query.
  - Preferences: typed `UserPreferences` (format, depth, visuals, language, ≤5 notes), saved through the `update_preferences` / `reset_preferences` tools and applied to the orchestrator and the writer. They are composed before the safety rules, so free-text notes can't override safety. Implicit nightly learning remains design-only (§17).
  - Today's date is part of the code-owned context. Without it, "this year" was resolved from earlier conversation periods (99s / 12 calls); after the fix it took **39s / 5 calls**.
- **Remaining latency item for M6:** the analyst still sometimes runs a "verification" query; to be addressed with evals and reasoning effort.

## 31. M6 Implementation Notes (Evals, Red Team, Latency)

- **Harness:** `evals/` sits outside the package and uses the composition root through `assemble_app(..., golden_view=HoldoutGoldenRepo)`. A context variable hides the trio under test from retrieval, so a case cannot copy its own SQL. Cases run concurrently (`--concurrency`, default 3). Results go to `var/evals/<run>.json|md` and to Langfuse: datasets `opsfleet-golden-sql` and `opsfleet-redteam`, one dataset-run item per turn trace, and `eval.*` scores on the trace. (Langfuse v4 `events_only` mode blocks reading runs and scores back through the API; view them in the UI.)
- **Latency fix — reasoning effort per role:** a probe through Bifrost showed Gemini's default thinking at ~9s / 700 reasoning tokens for a SQL task versus ~2s at `low` with the same output. `ModelChoice.reasoning_effort` is now set per role (guard, orchestrator, analyst, writer, summarizer `low`; judge, curator `medium`). It can be overridden without code changes: `OPSFLEET_REASONING_EFFORT__ANALYST=medium` (unknown roles fail at startup).

  | 10 SQL cases (1 per category) | default reasoning | low reasoning |
  |---|---|---|
  | mean correctness | 0.80 | 0.90 |
  | p50 / p95 latency | 80s / 217s | 31s / 97s |
  | mean LLM calls / queries | 8.0 / 4.1 | 5.1 / 2.0 |
  | red team | 16/18 (2 were guard blocks not yet listed as accepted) | 18/18 |

- **Prompt A/B, rejected:** a candidate analyst prompt (do arithmetic in SQL, use glossary defaults, charts only on request) was published to the Langfuse `candidate` label and compared on 30 cases. Production scored 0.83 correctness, 0.83 literal grounding, p50 35s. The candidate scored 0.80, 0.86 and 34s: not better, so it was not promoted and the repo prompts were left unchanged. This is the promotion gate from §17 in practice.
- **What the misses are:** on the 30-case production run, all 10 misses were *partial* (none outright wrong). Seven of them disagree only on whether order and customer counts exclude cancelled and returned items. The glossary says "Orders: count DISTINCT order_id" without saying whether status exclusions apply, and the LLM-curated golden SQL mixes both conventions. Next learning-loop action: make the glossary explicit (net orders), re-curate the trios, and re-run. The fix belongs in the data and definitions, not the prompt.
- **Bug found by the red team:** `deletion-preapproved` crashed with `value too long for varchar(256)`. The deletion-plan idempotency key embedded the full criteria JSON, and a "delete all" over many report ids overflowed it. The key is now `thread_id:sha256(criteria)`, with a unit regression test.
- **Guard behaviour:** the guard classifies bulk PII-contact requests ("emails and addresses of top customers") as injection. Masking would have made those answers safe anyway, so this is a mild false positive. It is accepted for now and visible in the red-team `blocked` score.

## 32. Deployment Contract (M8 + M9)

Single source of truth shared by app code, Terraform, Helm and CI/CD. Live demo target: 4–5 days, full designed stack.

### GCP
- Project `opsfleet-demo-56d0`, region `us-central1` (BigQuery public data is in `US`). Terraform state: GCS bucket `opsfleet-demo-56d0-tfstate`, prefix `main`.
- `infra/terraform/bootstrap` (applied once from a laptop, local state then migrated): state bucket, required APIs, GitHub Workload Identity Federation pool `github` / provider `github-actions` restricted to `Paliy2/opsfleet-demo`, deployer SA `opsfleet-deployer`.
- `infra/terraform/main` (planned on PR, applied on `main` by CI):
  - VPC `opsfleet`, subnet `opsfleet-gke` (nodes `10.10.0.0/20`, pods `10.20.0.0/16`, services `10.30.0.0/20`), Cloud Router + NAT, Private Service Access for Cloud SQL.
  - GKE Autopilot `opsfleet` (regional, release channel REGULAR, private nodes, public control-plane endpoint, Workload Identity, Managed Prometheus).
  - Cloud SQL `opsfleet-pg` (POSTGRES_16, `db-custom-1-3840`, private IP only, no HA). Databases: `opsfleet`, `temporal`, `temporal_visibility`, `keycloak`, `langfuse`. Users: `opsfleet`, `temporal`, `keycloak`, `langfuse`.
  - Artifact Registry Docker repo `opsfleet` → image `us-central1-docker.pkg.dev/opsfleet-demo-56d0/opsfleet/opsfleet-agent:<git-sha>`.
  - GCS: `opsfleet-demo-56d0-artifacts` (charts), `opsfleet-demo-56d0-langfuse` (Langfuse blobs via S3 interop + HMAC key).
  - Global static IP `opsfleet-ingress`. Base domain `<ip with dashes>.sslip.io`. Hosts: `ui.`, `api.`, `auth.`, `langfuse.` + base domain.
  - Billing budget (100 USD, alerts at 50/90/100%).
- Service accounts (GSA ← KSA via Workload Identity, namespace `opsfleet` unless noted):
  - `opsfleet-app` ← KSA `opsfleet-app`: BigQuery Job User, Secret Accessor, object admin on artifacts bucket. Used by api, worker, ui, jobs, evals.
  - `opsfleet-eso` ← KSA `external-secrets/external-secrets`: Secret Accessor (project).
  - `opsfleet-langfuse`: owns the HMAC key; object admin on the Langfuse bucket.

### Secret Manager (all created by Terraform; values generated by Terraform except `gemini-api-key`, whose value the owner adds)
`gemini-api-key`, `bifrost-virtual-key`, `pii-hash-salt`, `db-opsfleet-password`, `db-temporal-password`, `db-keycloak-password`, `db-langfuse-password`, `keycloak-admin-password`, `keycloak-realm` (rendered realm JSON incl. demo-user passwords and client secrets), `demo-credentials` (JSON user → password, for sharing with reviewers), `chainlit-auth-secret`, `chainlit-oauth-client-secret`, `langfuse-nextauth-secret`, `langfuse-salt`, `langfuse-encryption-key` (64 hex), `langfuse-public-key`, `langfuse-secret-key`, `langfuse-admin-password`, `langfuse-clickhouse-password`, `langfuse-redis-password`, `langfuse-s3-access-key`, `langfuse-s3-secret-key`.
External Secrets Operator (`external-secrets` namespace, ClusterSecretStore `gcp-secret-manager`) syncs them into Kubernetes Secrets.

### Keycloak
- Realm `opsfleet`; realm roles `ceo`, `manager`; users `ceo`, `mgr_a`, `mgr_b`, `mgr_empty` (username = app user id).
- Clients: `chainlit` (confidential, standard flow, redirect `https://ui.<base>/auth/oauth/keycloak/callback`), `opsfleet-cli` (public, device flow). Access token lifespan 1h for the demo.
- The API validates JWTs: issuer `https://auth.<base>/realms/opsfleet`, JWKS fetched from the in-cluster URL `http://keycloak:8080/realms/opsfleet/protocol/openid-connect/certs`. The user id comes from `preferred_username`, the brands from the app DB, and the token role must match the DB role.

### Kubernetes (namespace `opsfleet`; chart `infra/helm/opsfleet`)
One image, different commands:
| Workload | Command | Port |
|---|---|---|
| api (Deployment) | `opsfleet-api` (uvicorn) | 8000 http, 9464 metrics |
| ui (Deployment) | `chainlit run src/opsfleet_agent/ui/app.py --host 0.0.0.0 --port 8000` | 8000 |
| worker (Deployment) | `opsfleet-worker` (Temporal) | 9464 metrics |
| migrate (post-install/upgrade Job) | `opsfleet bootstrap` (alembic upgrade, seed users, load golden trios, push prompts) | — |
| evals (CronJob, suspended by default) | `python -m evals.run --suite redteam` | — |
Plus: bifrost (`maximhq/bifrost`, ConfigMap config), redis-app, keycloak (`quay.io/keycloak/keycloak`, `start --import-realm`), langfuse-web/worker, ClickHouse StatefulSet, langfuse-redis; Temporal from the official `temporal` chart (SQL persistence on Cloud SQL, no Elasticsearch/Cassandra); ESO from the official chart. GCE Ingress + `ManagedCertificate` + `FrontendConfig` (HTTPS redirect); `PodMonitoring` for metrics.

### App environment (pydantic-settings, prefix `OPSFLEET_`)
Existing settings plus: `ENV=production`, `AUTH_ISSUER`, `AUTH_JWKS_URL`, `AUTH_AUDIENCE` (optional), `REDIS_URL`, `RATE_LIMIT_PER_MINUTE` (10), `RATE_LIMIT_PER_DAY` (200), `TEMPORAL_ADDRESS` (`temporal-frontend:7233`), `TEMPORAL_NAMESPACE` (`default`), `CHART_BUCKET` (unset → local dir), `API_URL` (UI → API, `http://api:8000`). Local dev: `ENV=local` allows `X-Demo-User` instead of a JWT.

### API (`/v1`, Bearer JWT)
- `GET /healthz`, `GET /readyz`, `GET /v1/auth` (public) → `{issuer, cli_client_id}`, `GET /v1/me` → `{user_id, role, brands}`.
- `POST /v1/sessions` `{session_id?}` → `{session_id}`. A client-chosen id is created once and reopened by its owner; another user's id returns `E_NOT_FOUND`.
- `GET /v1/sessions?limit=&before=` → `{sessions[{id, title, created_at, updated_at}]}` (the caller's own, newest activity first; `limit` ≤ 100). `GET /v1/sessions/{id}/messages` → `{session_id, messages[{id, role, content, created_at, trace_id}]}`. `DELETE /v1/sessions/{id}` → 204.
- `POST /v1/sessions/{id}/turns` `{question}` and `POST /v1/sessions/{id}/confirmations` `{turn_id, approved}` → `text/event-stream` with events `progress {stage, detail, code}`, `confirmation {turn_id, plan_id, items[{id,title,created_at}], expires_at}`, `answer {text, chart_ids, trace_id, llm_calls, session_id}`, `error {code, message, trace_id}`.
- `GET /v1/sessions/{id}/charts/{chart_id}` → `image/png` (session owner only).
- Errors map to the taxonomy; 429 `E_RATE_LIMITED` from the Redis window.

### CI/CD (GitHub Actions, Workload Identity Federation, no JSON keys)
- `ci.yml` (PR + main): `make check`, integration tests, `terraform fmt -check` + `validate`, `helm lint`, `docker build`.
- `deploy.yml` (PR: `terraform plan` as a comment; push to `main`): `terraform apply` → build and push the image → install/upgrade ESO, Temporal and the `opsfleet` chart with `image.tag=<sha>` → readiness check of `/readyz`.
- Repo variables: `GCP_PROJECT`, `GCP_REGION`, `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_DEPLOYER_SA`.

## 33. M8/M9 Implementation Notes (first live deployment, 2026-10-08)

- **Live:** `https://ui.136-81-227-113.sslip.io` (Chainlit + Keycloak login), `https://api.…`, `https://auth.…`, `https://langfuse.…`. Google-managed certs became Active within ~30 min on sslip.io hosts.
- **Bootstrap path verified in-cluster:** the `migrate` hook ran Alembic on Cloud SQL, seeded demo users from a live BigQuery ranking (Workload Identity), loaded 58 golden trios through a Temporal workflow (embeddings via Bifrost → Gemini), and published 9 prompts to the in-cluster Langfuse.
- **Quota finding:** Autopilot node boot disks count against the regional `SSD_TOTAL_GB` quota (500 GB on a new project, 400 GB used by 4 nodes). ClickHouse's `standard-rwo` volume could not be provisioned. ClickHouse now uses a `pd-standard` StorageClass (`standard-hdd`). Larger node counts would need a quota increase.
- **Security finding, fixed before sharing the URL:** the UI pod had no `OPSFLEET_ENV`, defaulted to `local`, and therefore showed the demo password login. The API was not affected: it ran in production mode and rejected `X-Demo-User`. Fix: the chart sets the env on the UI, and both `Settings` and `ClientSettings` now *require* `OPSFLEET_ENV` (no default), so a missing variable fails at startup instead of enabling dev logins.
- **Least privilege for the UI:** the UI reads `ClientSettings` (env and API URL only) and gets no database, LLM or salt secrets; it talks to the API over HTTP with the user's Keycloak token.
- **Image layering:** the dependency layer (`uv sync --no-install-project`) is separate from the project layer, so code changes push a few MB instead of the ~560 MB venv. CI builds on amd64 runners with the GHA layer cache.
- **First deploy was done from a laptop** with the same `infra/scripts/deploy.sh` that CI runs, while CI was not yet on `main`. Terraform state is shared (GCS), so CI's first apply is a no-op.
- **First CI/CD deploy from `main`** (merge of PR #8) ran apply → build/push → Helm in ~6 min, keyless via Workload Identity Federation.

## 34. M10 Notes (UI debug mode, CLI single sign-on, local full stack)

- **UI debug mode:** executives see only the answer and charts. LLM call count and the trace link are behind a per-user *Debug* switch in Chainlit chat settings. They render inside the collapsed progress step as Markdown, with a direct link into Langfuse (`OPSFLEET_TRACE_URL` template). Raw HTML stays disabled (`unsafe_allow_html = false`).
- **CLI against the deployed API:** `opsfleet remote login --api <url>` discovers the issuer from `GET /v1/auth`, then runs the OAuth 2.0 device authorization grant (RFC 8628) against the public `opsfleet-cli` Keycloak client. No client secret ships with the CLI; the user approves in the browser. Tokens are stored at `~/.config/opsfleet/credentials.json` (0600) and refreshed a minute before expiry. `remote chat` uses the same HTTP/SSE contract as the UI, so the CLI and UI share one authorization path (JWT → `AccessScope`) and one rate limit.
- **Local CLI modes:** `opsfleet chat --user <id>` stays the in-process dev loop (local env only); `remote chat` is the production path.
- **API client is a gateway:** the HTTP/SSE client moved from `ui/` to `gateways/analyst/` so both entrypoints use it without breaking the layer contract (`api | cli | ui` are independent).
- **Compose `full` profile** mirrors the cloud topology: Keycloak (realm import with env placeholders), Redis rate limiting, Temporal dev server, API, worker, a one-shot `bootstrap` (migrate → seed → golden via Temporal → prompts), and Chainlit. BigQuery uses the developer's ADC file mounted read-only. Local Keycloak serves the CLI device flow; the local UI keeps the dev login because Chainlit's Keycloak provider uses one base URL for both browser redirects and server-side token calls, which cannot be the same host inside and outside Docker. The cloud deployment exercises the UI SSO path.

## 35. PII Redaction of User Input (Sensitive Data Protection)

**Problem.** Data from BigQuery was masked, but text the user typed reached the guard model, the agent, the history and Langfuse unchanged. Regex alone misses names, addresses and obfuscated forms ("eugene dot domer @ gmail dot com").

**Design.** `ChatTurn` calls a `PiiRedactor` port first. The guard, agent, conversation history and the trace input then see only the redacted text. Two layers (`LayeredRedactor`):

1. **Local rules** (`interactors/pii_redaction.py`), no network:
   - emails, including obfuscated forms ("dot", "(at)", "[at]", spaces around `@`);
   - phone numbers, including digits spelled as words;
   - Luhn-valid card numbers;
   - precise coordinates.
   Masked ids like `user_1a2b3c4d@…` are kept.
2. **Google Sensitive Data Protection** (`gateways/dlp/redactor.py`). `inspect_content` detects the 10 types below:

   | Info type | Minimum likelihood |
   |---|---|
   | `PERSON_NAME`, `STREET_ADDRESS`, `DATE_OF_BIRTH` | LIKELY |
   | `EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD_NUMBER`, `IBAN_CODE`, `US_SOCIAL_SECURITY_NUMBER`, `PASSPORT`, `IP_ADDRESS` | POSSIBLE |

   Findings are replaced locally by position.

**Brand and pseudonym allow-list.** Many brands are person names (Calvin Klein, Ralph Lauren, Tommy Hilfiger), and our own surrogates look like names (Morgan Costa). DLP reports "Ralph" and "Lauren" as separate findings, so DLP's dictionary exclusion (full match) cannot be used.

The redactor drops a `PERSON_NAME` finding only when it lies *entirely inside* a mention of a known brand (all brands, loaded once from BigQuery) or of the pseudonym name pattern. "Calvin Smith" is still redacted, while "Calvin Klein" is kept.

**Misspelled brands.** A user typing "Calcin Klein" used to get the name masked, so "delete all reports mentioning Calcin Klein" searched for `[PERSON_NAME]` and found nothing. A name finding is now also kept when the words around it are within a small edit distance of a brand: none for brands under 5 characters, 1 up to 10, 2 above. "Kevin Klein" (3 edits) is still redacted. The check runs only on DLP name findings, about 10 ms each against the full brand list.

**Masked search text is refused.** Report search and deletion reject text that contains a placeholder such as `[PERSON_NAME]` with `E_MASKED_SEARCH`, before any lookup. The agent then asks the user to retype the term instead of reporting "no matching reports".

**Masked values never reach SQL.** A placeholder inside an SQL string literal (`= '[EMAIL_ADDRESS]'`, `LIKE '%[PERSON_NAME]%'`) can never match: the original value is discarded and customer data is pseudonymized. `sql_guard` scans string literals only (not comments, backtick identifiers, array literals or subscripts) for the known info types and raises `E_MASKED_VALUE` before the dry run; the analyst also refuses a task that holds a placeholder before any LLM call. The error is not recoverable: it ends the analyst and the turn at once, so the model cannot retry for 10 steps or invent a name. The user is asked for a customer, user or order id. It has its own code, not `E_MASKED_SEARCH`, because the remedy differs (give an id, not retype a term) and it is counted separately.

**Fail closed.** If DLP errors or times out (10s), the turn ends with `E_PII_UNAVAILABLE` (HTTP 503), and nothing is sent to the LLM. `OPSFLEET_PII_REDACTOR=regex` runs only the local layer, for machines without the DLP API; this is a documented, weaker mode.

**UX.** A `privacy` progress event ("Removed personal data: email address, person name") tells the user that something was removed.

**Verified live** (2026-10-08):
- obfuscated email, full name, street address, IBAN and spelled-out phone were removed;
- brand comparisons (Ralph Lauren vs Calvin Klein) and pseudonym follow-ups were untouched;
- a full agent turn answered the brand question without seeing the user's name or email.

**Infrastructure.** The `dlp.googleapis.com` API (bootstrap) and `roles/dlp.user` for the app service account.

**Not built:**
- DLP on model *outputs*: these are built from masked data and covered by the output regex scan.
- DLP on golden trios: they are LLM-generated from masked data.

## 36. Gap Closing (2026-10-08)

A design-vs-code audit found claims without code behind them. Each was either built or marked design-only (the "Status" notes in §12, §16, §17, §19, §20).

- **Saved Reports library.** The `open_report` tool, `GET /v1/reports` and `GET /v1/reports/{id}`, and the CLI commands `remote reports` / `remote report <id>`.
  - The owner is checked twice: in the SQL filter and in the `GetReport` interactor.
  - A report that is missing and a report owned by someone else both return the same `E_NOT_FOUND`, so existence is never revealed.
  - Reports outlive their session; `session_id` becomes null when the session is deleted.
- **👍/👎 feedback.** `POST /v1/sessions/{id}/feedback` is used by the UI buttons and by the CLI `/good` / `/bad [comment]`.
  - The vote must refer to a trace from the user's own session.
  - Comments go through the PII redactor first.
  - Votes are stored in Postgres, one per (user, trace), latest wins. They are mirrored as the Langfuse score `user.feedback` (1/0) on the trace and counted in `opsfleet_feedback_total{rating}`.
  - This is the signal for golden promotion (👍 + analyst approval) and failure mining (👎 joined with trace error codes and prompt versions).
- **Cost controls.**
  - Bifrost governance on `vk-agent`:

    | | Daily budget | Requests | Tokens |
    |---|---|---|---|
    | Local | $5 | 120/min | 2M/hour |
    | GKE | $20 | 300/min | 5M/hour |

  - Exhausting the budget returns 402, mapped to `E_LLM_BUDGET`: no retry, no fallback, API status 503. A gateway rate limit maps to `E_LLM_RATE_LIMITED` (429).
  - The unused routing alias `agent-default` was removed; fallbacks are sent per request.
  - On GKE, Bifrost state is an `emptyDir`, so spend counters reset when the pod restarts. That is acceptable for the demo; production would use a persistent config store.
  - **Per-turn token budget** `OPSFLEET_TURN_TOKEN_BUDGET` (default 200k). A ContextVar meter counts usage across the guard, the orchestrator and nested analyst/writer calls. When it runs out, the loop finalizes with a best-effort answer instead of failing. Tokens per turn go to the trace and to `opsfleet_turn_tokens`.
- **Capability evals** (`--suite capability`, 12 multi-turn cases): customers, brand comparisons with "why", time metrics, schema questions, multi-step "why" questions, Q1 report → Q2 actions, and follow-ups.
  - A rubric judge sees the conversation, saved report bodies, per-turn artifacts (charts and reports) and every result table.
  - Deterministic checks: report saved for the owner, chart produced, no PII.
  - The gate is a mean score ≥ 0.70. On the first live run, 11/12 passed with a mean of 0.99, p50 50s and p95 120s.
  - The judge is lenient (nearly everything scores 1.00), so this is a regression gate, not proof of quality.
  - The one real miss: an answer did not state the period and status filter for "lifetime spend".
- **Setup reproducibility.**
  - `.env.example` works as-is for `make up`; Langfuse settings are all-or-nothing and commented out.
  - Variables for optional compose profiles have soft defaults, and `make up-obs` / `make up-full` check their own secrets.
  - `requirements.txt` is exported from `uv.lock`, and CI checks it for drift.
  - The nightly evals cron was removed, because the eval service account and its secrets are not provisioned; the workflow runs on manual dispatch only.
- **Reports as files.** `GET /v1/reports/{id}/pdf` renders the stored markdown and charts.
  - Rendering uses fpdf2 with matplotlib's bundled DejaVu font for Unicode.
  - An answer that saves a report attaches its PDF.
  - The UI's **Reports** button lists the library.
  - A chart blob that can't be loaded is noted in the PDF rather than failing the export.
  - Found by a user: the model claimed a "dashboard" that doesn't exist. The tool result now states where the report lives.
- **Zero-downtime rollouts.** Single-replica api/ui pods produced the load balancer's 502 page on every deploy: the old pod was killed before the GCE load balancer admitted the new one.
  - The fix: `maxSurge 1` / `maxUnavailable 0`, `minReadySeconds 30`, a 30s `preStop` drain and a 60s grace period.
- **Evals published to the GCP Langfuse.** Earlier runs published only to the local Langfuse. A one-off in-cluster Job from the `evals` CronJob template runs all suites with `--publish` against production Langfuse, under the app's Workload Identity.

## 37. Conversation History

Before this change, history was stored and used by the agent but nobody could see it: every page load started a new session, a refresh lost the chat on screen, and old sessions were unreachable.

- **Source of truth stays the API.** `chat_sessions` / `chat_messages` already hold redacted text and answer trace ids. Three endpoints expose them (§32): list, transcript, delete. Ownership is checked in the interactors (`ListConversations`, `GetConversation`, `DeleteConversation`); a missing session and someone else's session both return `E_NOT_FOUND`. Red-team tests cover reading, deleting and claiming another user's session.
- **Titles are derived, not stored.** The title is the first redacted question, whitespace-collapsed and cut to 60 characters. Sessions with no messages are not listed. Rename is not supported: Chainlit sends the raw first message as a thread name, and storing it would bypass redaction.
- **Deleting a conversation** removes its messages, result sets, charts, feedback and the LangGraph checkpoints of its turns (thread ids `session:turn`). Saved reports survive in the library with `session_id` set to null; chart blobs stay because reports reference them.
- **Web UI: a Chainlit data layer that reads through the API** (`ui/threads.py`), never Postgres. It writes nothing (steps, elements and thread updates are no-ops), so the UI holds no second copy of user text.
  - Chainlit calls the data layer from HTTP handlers that do not pass the user. Chainlit calls `create_user` with the signed-in user on every authenticated request (`get_user` always returns None), so the store records that user in a ContextVar and every API call carries that user's own bearer token. Socket handlers fall back to the session user. With no user it refuses (`E_UNAUTHENTICATED`).
  - The Chainlit thread id is the session id: `on_chat_start` opens the session with it, so `/thread/<id>` survives a refresh and `on_chat_resume` reopens the same server-side context (summary, analysis state).
  - Reopened chats show text only. Charts and PDFs from earlier turns are not re-attached; reports stay reachable through **Reports**.
  - The chat list refreshes once after the first saved answer, because Chainlit refreshes it when the message is sent, before the API has saved it (redaction and the guard run first).
- **Feedback moved to Chainlit's built-in 👍/👎** (with its comment dialog), which appear once persistence is on. Chainlit attaches a vote to the parent run step. Live runs are mapped to trace ids in the user session. In reopened chats the rebuilt run step's id is the trace id, so the vote maps back directly. Answers saved before trace ids were stored cannot be rated.
- **CLI:** `opsfleet remote sessions` lists conversations; `opsfleet remote chat --resume <id>` prints the last few messages and continues.
- **The Keycloak access token never reaches the browser.** Chainlit puts the whole user, metadata included, into its auth cookie (an HS256 JWT, httponly but only signed, not encrypted) and returns it from `/user` to the page. So `oauth_callback` keeps the token in an in-process `TokenVault` (`ui/tokens.py`) and the user metadata carries only a random handle; `api_headers` resolves the handle server-side and checks it belongs to the same user. Entries expire after `user_session_timeout`. A handle the vault does not know (UI pod restarted) or an expired one raises `E_UNAUTHENTICATED`, which takes the sign-in path below. The vault is per process, so this needs a single UI replica or sticky sessions; more replicas would move it to Redis (§12).
- **Session cookie lifetime** (`user_session_timeout`) is 1 hour, matching the Keycloak access token lifetime, so the UI asks for sign-in again instead of failing with 401s on an expired token. If the token still expires during an open chat, any `E_UNAUTHENTICATED` from the API renders the `SignIn` custom element (`public/elements/SignIn.jsx`), which clears the Chainlit cookie and goes to `/login`. It redirects automatically at most once a minute; after that it shows a manual link, so a misconfigured verifier cannot cause a redirect loop. The sidebar and thread loading call the API over plain HTTP, not the socket: there the thread store turns an API 401 into `UnauthenticatedError`, and a handler on Chainlit's server returns 401 and clears the auth cookie. Chainlit's client sends every 401 to `/login`, and with the cookie gone the login page does not send the user straight back.
- **Not built:** search across all conversations on the server (the sidebar search filters the loaded page), pagination by cursor in the CLI, conversation export, a retention period for old conversations.

## 38. Cross-Provider Fallback: Vertex AI

The single-provider gap: every model was served by AI Studio through one API key, so a revoked key, an AI Studio outage or an AI Studio quota cut was a full outage.

- **Second provider: Vertex AI, same Gemini models.** Bifrost gets a `vertex` provider next to `gemini`. Every chain in `core/models.py` ends with one Vertex model (§14), so prompts, tool schemas and behaviour do not change when it serves. Non-Gemini providers stay out (requirement 6).
- **Auth: GKE Workload Identity, no JSON keys.** Terraform creates `opsfleet-bifrost@` with `roles/aiplatform.user` and binds it to the `opsfleet/bifrost` Kubernetes service account; the bootstrap root enables `aiplatform.googleapis.com`. Bifrost's Vertex key has an empty `auth_credentials`, which makes it use Application Default Credentials, i.e. the GKE metadata server.
- **Config shape** (rendered by the Helm helper `opsfleet.bifrostConfig` from `files/bifrost-config.json` when `bifrost.vertex.enabled`): `providers.vertex.keys[0] = {"name": "vertex", "value": "", "models": ["*"], "vertex_key_config": {"project_id": <gcpProject>, "region": <bifrost.vertex.region>, "auth_credentials": ""}}`. Models are addressed as `vertex/<model-id>`. The chart refuses to render without `bifrostGsaEmail` when Vertex is on.
- **Region: `global`.** Gemini 3.x is served from the global endpoint with the most capacity; the demo has no data-residency requirement for prompts.
- **Local stays Gemini-only, with the same chains.** `infra/compose/bifrost/config.json` has no `vertex` provider and needs no GCP credentials. Bifrost skips a fallback whose provider is not configured (it logs a warning and moves on) and, if all steps fail, returns the primary error, so error mapping is unchanged locally. One chain definition therefore serves both environments. The Vertex step is in the `free-tier` profile too, because that is the profile the GKE deployment runs.
- **Blast radius it covers:** AI Studio outage, revoked or leaked key, AI Studio quota or key restriction. **Not covered:** a closed billing account (both providers bill the same project), and the Bifrost daily budget, which is per virtual key and stops every provider.
- **Cost:** Vertex has no free tier, but it only serves after every AI Studio step failed, and Bifrost governance prices it against the same `vk-agent` budget.
- **Status:** built and tested (unit tests on the chains, `helm template`/`helm lint`, `terraform validate`). **Verified live (2026-10-08):** one in-cluster request with `{"model": "vertex/gemini-3.8-flash"}` through Bifrost returned 200, so Workload Identity, the IAM role and the model id in `global` work. **Not verified:** Gemini thought signatures surviving a mid-conversation switch from AI Studio to Vertex.

## 39. Report Search at Scale

**Problem.** The agent had one filter, "title or body contains X" (`ILIKE '%X%'`, no index, no limit). For "reports not about Calvin Klein" it listed every report and opened each one to check. With 1,000 reports that is a 1,000-line tool result plus hundreds of full report bodies in context, and the step limit cuts the turn off with a partial answer.

**Decision: the database filters, the agent pages.**
- **One tool, `search_reports`:** `include` (all must match), `exclude` (none may match), `created_after` / `created_before`, `this_conversation`, `page`. It returns the total, the page number and at most **30 hits per page** (id, title, date, a snippet around the match), plus "call again with page=N+1, or narrow the filters". The page size is a hard limit in the interactor, not a model choice.
- **`delete_reports` takes the same filters** and plans up to **500** matches at once (ranked like search). The plan row stores those exact ids, so only what the user confirmed can be deleted. The interrupt payload, SSE event and UI carry only the count, the total that matched and the first 20 titles, never the full list. When more than 500 match, the confirmation says how many stay and the agent offers the next batch after this one.
- **Prompt rule:** filter with the tool, never open reports to decide which ones match; `open_report` reads one report.

**Lexical search with spelling correction: Postgres `pg_trgm`.**
- A generated column `search_text = title || ' ' || body` with a GIN `gin_trgm_ops` index. The same index serves `ILIKE '%term%'` and the trigram word-similarity operator `<%`, so both stay index-backed as the table grows.
- **Exact first, close spellings only as a fallback.** Per term: if any of the user's reports contain it exactly, only exact matches count. Otherwise the term switches to `<%` with `word_similarity >= 0.55`, and the result says which terms were corrected so the agent can tell the user ("no exact match for 'Kalvin Klein', matched 'Calvin Klein'"). Measured scores: "Kalvin Klein" 0.82, "Calcin Klein" 0.60, "Diesal" 0.57, while unrelated words score below 0.2. "Kevin Klein" also scores 0.70, which is why fuzzy matching is a fallback and not the default: a report that really mentions Kevin Klein wins, and nothing silently widens a deletion.
- Terms shorter than 4 characters never fall back (trigrams are noise there). Exclusions follow the same rule, so "not about Kalvin Klein" drops the Calvin Klein reports.
- Results are ranked by the best word similarity of the include terms, then newest first.
- **Bounded reads.** Every read selects only the columns it returns. Search computes the snippet in SQL (`strpos` + `substr`), so no report body leaves the database. The exact-match check for all terms is one query (`bool_or(...)` per term), not one per term. The REST library list (`GET /v1/reports?page=N`) uses the same 30-per-page search.

**Why not full-text search (`tsvector`) here.** Stemming helps prose queries ("churned" → "churn"), but report searches are mostly names (brands, clients, titles), where typos matter more than stems, and `tsvector` does nothing for typos. Trigrams cover substrings and misspellings with one index. The golden-trio store already uses `tsvector` + pgvector, where the queries are questions.

**Future (not built): semantic search over reports.** Embed each report (title + executive summary) on save into pgvector, the same `GatewayEmbedder` and HNSW setup as the golden trios. Then "reports about customer retention" also finds the churn reports. Combine it with the trigram score by reciprocal rank fusion, as golden retrieval does. Deletion would keep using the lexical filters only, so a semantic neighbour is never deleted by accident. Backfill existing reports with a Temporal job.

## 40. Bounded State per Turn

- **Checkpoints live only while a confirmation is pending.** The orchestrator graph writes LangGraph checkpoints (full message state) after every node, and only `delete_reports` needs them, to resume after `interrupt()`. When a turn ends without an interrupt, or a resumed turn finishes, the orchestrator deletes the checkpoints of that thread (`session:turn`). Before this, every turn of every conversation kept its checkpoints until the conversation was deleted.
- **Confirmation payloads are fixed size** (§39): the count, the total and up to 20 titles.

**Future (recorded, not built):**
- **Retention job.** A Temporal schedule, like the one that expires deletion plans, that removes stored result sets (up to 500 rows each) older than N days, and checkpoints left behind by confirmations nobody answered.
- **Parallel pre-flight.** PII redaction must run first, because the guard and everything after it may only see redacted text. After that, the guard LLM call and the context loads (history, summary, analysis state, preferences) are independent and can run together. Summarizing older turns is only needed on the next turn, so it can run after the reply is sent. The API dependencies also load scope and session a second time; pass them through instead.
- **Report bodies out of the model context.** `create_report` and `open_report` return the full body to the orchestrator, the answer repeats it, and it stays in history for the next turns. The largest recurring context item. Return the title, id and executive summary instead, and let the clients show the full report from the PDF (UI) or by printing the stored report (CLI). A product decision, deferred so the agent can still quote reports in the demo.

## 41. Long-Running Reports on Temporal (designed, not built)

**Today.** `create_report` runs inside the chat turn: the analyst has already queried, so a report is one writer call plus PDF rendering, seconds to about a minute, streamed to the user as progress events. The turn is a task in the API pod, so a closed tab does not lose the report. A pod restart or deploy during the turn does.

**When a report moves to Temporal.** Any of these:
- it plans its own sections and runs many queries (minutes, not seconds);
- it runs on a schedule ("every Monday, brand performance");
- it is delivered outside the chat (email, Slack);
- many reports are rebuilt at once (after a data fix or a prompt change).

**Workflow `GenerateReport`** (worker `opsfleet-worker`, task queue `reports`):
1. **Start.** The orchestrator calls a `start_report` tool. The API creates a `report_jobs` row (`id, owner_id, session_id, status, step, report_id, error, created_at`) and starts the workflow with workflow id `report:{job_id}`. The agent answers at once: "Started. I'll post it here when it is ready", and the turn ends.
2. **Activities**, each retried on its own with a bounded policy:
   - `plan_sections`: the writer model turns the request into sections and analysis tasks;
   - `run_analysis` per section: the existing `DataAnalyst` and `RunQuery` path (scope, masking, cost cap), with a `QueryRejectedError` marked non-retryable;
   - `write_report`: the writer model, from stored results only;
   - `render`: charts and PDF to GCS;
   - `save`: `SaveReport` (PII scan), idempotent on `job_id`.
3. **Security.** The workflow carries the user id, never a token or a scope snapshot. Each activity re-resolves the access scope, so revoked brands or a deleted user stop the job. Token and BigQuery-byte budgets are per job, not per turn.
4. **Control.** A `cancel` signal ("stop that report") and a `progress` query. Workflow timeout 30 min. The trace id goes into the workflow memo, so one Langfuse trace spans the API, the workflow and Bifrost.
5. **Schedules.** Recurring reports are Temporal Schedules that start the same workflow.

**Getting events to the frontend.**
- **Source of truth: Postgres.** Every step change appends a row to `user_events` (`id` bigserial, `owner_id`, `kind` = `job.progress | job.done | job.failed`, `payload`, `created_at`) in the same transaction as the `report_jobs` update. This is an outbox: an event is never lost, even if nobody is connected.
- **Fan-out: Redis pub/sub** on channel `events:{user_hash}`, published after the commit. It is only a wake-up signal, so the `allkeys-lru` eviction of `redis-app` cannot drop it, and it works across API replicas.
- **Delivery: `GET /v1/events` (SSE)**, one stream per signed-in user. On connect it replays rows after `Last-Event-ID` from Postgres, then follows the Redis channel. Delivery is at least once, and clients drop duplicates by event id.
- **UI (Chainlit).** `on_chat_start` and `on_chat_resume` open a background reader on `/v1/events`. `job.progress` updates a "Preparing report" step. `job.done` posts a message with the PDF attached, and the worker also appends that message to the conversation it came from, so the user still sees it after reopening the chat later.
- **CLI.** `opsfleet remote jobs` lists jobs; `remote chat` follows the same SSE stream.
- **Later channels.** Email or Slack are more consumers of `user_events`, not changes to the workflow.

**Why not now.** It adds a job table, an event stream, a UI background reader and new failure modes for no gain at today's report size, and it is outside the brief. The pieces it reuses already run in production: Temporal and the worker (golden ingestion, plan expiry), `RunQuery`, `SaveReport`, GCS blobs and Redis.

## 42. Prompt Caching and Gateway Tracing

**Cached tokens are counted** (built). Gemini reports implicit-cache hits, and Bifrost passes them through as `usage.prompt_tokens_details.cached_tokens`. LangChain maps that field to `input_token_details.cache_read`, and Langfuse already stores it as `input_cache_read` and prices it separately. The app now also counts it:
- `opsfleet_llm_tokens_total{direction="cached_input"}` per served model;
- `opsfleet_turn_cached_tokens` histogram per turn kind;
- `cached_tokens` next to `tokens` in the turn trace metadata.

**Finding (2026-10-09): no cache hits in production.**
- 0 of 273 production generations read from the cache, although 152 of them had more than 3,000 input tokens.
- Two identical 5,000-token requests through Bifrost: without tools, the second call read 2,032 tokens from the cache; with six tool declarations attached, both calls read 0.
- Every agent loop binds tools, so the agents get no implicit caching today. The guard, summarizer and writer calls have no tools, but they are short.

**Next steps (not built):**
1. Repeat the tools test directly against the Gemini API to find out whether the tool declarations break caching inside Gemini or in Bifrost's conversion (for example, a tool schema serialized in a different order on each request). If Bifrost is the cause, fix or report it upstream and pin the version.
2. If Gemini is the cause, use explicit context caching (`cachedContents`) for the stable prefix (instructions, schema, glossary, tool declarations), with one cache per prompt version and a TTL. This needs gateway support or a direct-provider path.
3. Put stable text first, so any cache covers more. The safety rules must stay after every editable layer (§20), so the per-user parts (preferences, conversation summary) move into a separate context message after the system prompt instead of before the safety rules.

**Gateway tracing (planned).** Today the traces come from the application: the Langfuse LangChain callback records every generation inside the agent tree (turn → orchestrator → analyst → tool). Bifrost itself exports no traces, so it records nothing about its own retries, fallbacks or provider latency, and a client that does not use the app's callbacks (a future worker or eval job) would go untraced. The plan:
- Enable Bifrost's `otel` plugin with `trace_type: genai_extension` and `protocol: http`, sending to Langfuse's OTLP endpoint `/api/public/otel` with Basic auth from the existing Langfuse keys (an External Secrets value).
- The app sends a W3C `traceparent` header on each LLM request, taken from the current span (the Langfuse SDK is OpenTelemetry-based). An inbound `traceparent` takes precedence in Bifrost, so the gateway span nests under the agent span in the same trace.
- To avoid counting each call twice, the gateway span becomes the single source of generations and token usage, and the app keeps only the agent and tool spans. Every LLM client is then traced the same way, whether it is the API, the worker or evals.
- Why not built now: the app-side traces already give the agent tree, prompt versions and fallbacks for every user turn. Moving generations to the gateway changes how cost is attributed and needs the Langfuse OTLP ingestion verified on the in-cluster version, which is more than the deadline allows.
