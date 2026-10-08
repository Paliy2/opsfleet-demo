# OpsFleet Data Analysis Agent

A chat assistant for non-technical executives. Ask questions about sales, customers and products in plain language. The agent writes and runs BigQuery SQL over `bigquery-public-data.thelook_ecommerce`, explains the results, draws charts, and saves reports with action items. It enforces per-user data scope, masks PII before the model sees any data, and requires explicit confirmation before deleting anything.

| | |
|---|---|
| **Live demo** | UI <https://ui.136-81-227-113.sslip.io> · API <https://api.136-81-227-113.sslip.io> · Langfuse <https://langfuse.136-81-227-113.sslip.io> |
| **Demo users** | `ceo` (all brands), `mgr_a`, `mgr_b` (3 brands each, one shared), `mgr_empty` (low-data brand). Passwords are shared separately. |
| **Interfaces** | CLI (`opsfleet chat` locally, `opsfleet remote chat` against the live API with SSO), Chainlit web UI (conversation history in the sidebar, 👍/👎, a per-user Debug switch), REST + SSE API |
| **Design log** | [`docs/DECISIONS.md`](docs/DECISIONS.md): every decision, trade-off and measured result, by milestone |

### Reviewer quick path

**Live, 2 minutes.** Open the UI, sign in as `mgr_a`, and try:

| Ask | Shows |
|---|---|
| "What data can you analyze for me?" | Questions about the database itself |
| "Who are my top 5 customers by lifetime spend?" | Brand scope, masked names and emails, golden examples ("Using validated examples") |
| "How does Carhartt compare?" | Out-of-scope refusal |
| "Write a short report on my brands last quarter with action items" | Multi-step analysis, chart, PDF report |
| "Delete all reports that mention Calvin Klein" | Preview, then Delete/Cancel confirmation |

Toggle **Debug** in the chat settings to see LLM calls and the Langfuse trace link for each answer.

**Local, 5 commands** (needs Docker, `uv`, a Gemini key and a GCP project with BigQuery; details in [6.1](#61-local-minimal-cli-only)):

```bash
make install && cp .env.example .env   # set GEMINI_API_KEY, OPSFLEET_GCP_PROJECT, OPSFLEET_PII_HASH_SALT
gcloud auth application-default login
make up && make migrate
make seed
make chat USER_ID=mgr_a
```

**Where each requirement is answered:**

| Brief | Design | Code |
|---|---|---|
| 1. Hybrid intelligence (Golden Bucket) | [2.1](#21-hybrid-intelligence-golden-bucket) | `interactors/golden.py`, `repos/golden.py`, `agents/golden_curator.py`, `workers/` |
| 2. Safety and PII masking | [2.2](#22-safety-and-pii-masking) | `gateways/bigquery/scoped_sql.py`, `interactors/sql_guard.py`, `interactors/pii_redaction.py`, `gateways/dlp/`, `agents/guard.py` |
| 3. Destructive ops | [2.3](#23-high-stakes-oversight-deleting-reports) | `interactors/reports.py`, `agents/orchestrator.py` (`delete_reports` + `interrupt()`) |
| 4. Learning loop | [2.4](#24-learning-loop) | `interactors/context.py`, `interactors/feedback.py`, `evals/` |
| 5. Resilience | [2.5](#25-resilience-and-graceful-errors), [5](#5-error-handling-and-fallbacks) | `agents/analyst.py`, `interactors/run_query.py`, `gateways/llm/chat_models.py`, `core/errors.py` |
| 6. Quality assurance | [2.6](#26-quality-assurance), [7](#7-evaluation-results) | `evals/`, `tests/` |
| 7. Observability | [2.7](#27-observability) | `gateways/observability/` |
| 8. Persona management | [2.8](#28-persona-management) | `gateways/prompts/langfuse.py`, `src/opsfleet_agent/prompts/` |
| CLI | [6](#6-setup-and-example-run) | `cli/main.py`, `cli/remote.py` |

Paths under `src/opsfleet_agent/` unless shown otherwise.

---

## Contents

1. [Architecture](#1-architecture)
2. [How each requirement is solved](#2-how-each-requirement-is-solved)
3. [Technology choices](#3-technology-choices)
4. [Data flow of one turn](#4-data-flow-of-one-turn)
5. [Error handling and fallbacks](#5-error-handling-and-fallbacks)
6. [Setup and example run](#6-setup-and-example-run)
7. [Evaluation results](#7-evaluation-results)
8. [Repository layout and engineering standards](#8-repository-layout-and-engineering-standards)
9. [Prototype vs production, and what is not built](#9-prototype-vs-production-and-what-is-not-built)

---

## 1. Architecture

Diagrams (in [`docs/architecture/`](docs/architecture/)):

- **GCP runtime:** ingress, services, data stores and identity on GKE.
- **Local runtime:** docker compose profiles.
- **AI flow:** what happens inside one chat turn.

**Style: a modular monolith.** One Python package runs as three processes from one image: `api`, `worker` (Temporal) and `ui` (Chainlit). At ~100 executives × ~10 questions a day, microservices would add operational cost without benefit. Strict internal boundaries (hexagonal layers, enforced by import-linter) keep every module splittable later.

| Building block | Technology | Responsibility |
|---|---|---|
| Clients | Typer CLI, Chainlit web UI | Thin clients over one HTTP/SSE contract; no business logic |
| Identity | Keycloak (OIDC) | Login, realm roles `ceo` / `manager`. Chainlit uses the authorization-code flow; the CLI uses the device flow (no secret on the laptop). |
| Edge | GCE Ingress + Google-managed TLS | HTTPS for `ui.`, `api.`, `auth.`, `langfuse.` hosts |
| Agent API | FastAPI | JWT validation (JWKS), Redis rate limit, SSE progress streaming, error taxonomy → HTTP |
| Agent runtime | LangGraph, used thinly | Orchestrator loop with specialist agents as tools; `interrupt()` with a Postgres checkpointer for deletion confirmation |
| Privacy and guardrails | Local PII rules + Google Sensitive Data Protection (DLP) on user input; regex fast path + Flash-Lite classifier; code-owned safety rules; output PII scan | Personal data typed by users is removed before any LLM call; off-topic and prompt-injection blocking |
| Data access | `QueryEngine` port → BigQuery adapter | The only path to data: scoping and masking CTEs, dry-run policy check, byte cap, timeout |
| LLM gateway | Bifrost (OpenAI-compatible) | Gemini model fallbacks and retries across two providers (AI Studio, then Vertex AI on GKE), plus one virtual key with a daily budget and request/token rate limits, shared by the API, workers and evals |
| Prompts, tracing, evals | Langfuse (self-hosted) | Versioned prompts and agent configs (labels, no redeploy), nested traces, eval datasets and scores |
| Durable work | Temporal | Golden-bucket ingestion, scheduled expiry of deletion plans; the home for long reports and the learning loop |
| App data | Cloud SQL Postgres 16 + pgvector | Sessions, messages, reports, deletion plans, preferences, entitlements, result sets, golden trios (vector + full-text) |
| Blobs | GCS | Chart PNGs; Langfuse event and media storage |
| Secrets | Secret Manager → External Secrets Operator | No secret in git, images or CI; pods use Workload Identity |
| Delivery | Terraform, Helm, GitHub Actions | Plan on PR; apply + build + deploy on merge, keyless via Workload Identity Federation. Zero-downtime rollouts behind the load balancer (surge first, readiness hold, drain before stop). |

---

## 2. How each requirement is solved

### 2.1 Hybrid intelligence (Golden Bucket)

**What a trio is.** A *Question → approach → SQL → analyst report* record.

**How the 58 seed trios were made** (`opsfleet golden curate`):
- An LLM curator proposed them across 10 business themes.
- Every SQL statement was executed through the production query path, and only those that ran and returned rows were kept: 58 kept, 2 rejected.
- Each survivor then got a report grounded in its actual result.

**Storage.** `data/golden/trios.jsonl` is the versioned source; in production this is a GCS prefix. A Temporal workflow embeds each trio (`gemini-embedding-001`, 768 dims, through Bifrost) into pgvector, with an HNSW cosine index plus a generated `tsvector` (GIN).

**Retrieval at query time:**
- One SQL statement runs a hybrid search: semantic top-30 and lexical top-30, fused by reciprocal rank fusion (k=60).
- Results are filtered by cosine ≥ 0.55. The threshold was calibrated live: unrelated questions score ≤ 0.49.
- The top 6 become few-shot examples for the analyst agent.
- Only question, approach and SQL are shown. Reports contain CEO-scope numbers and never reach the model; a unit test enforces this.
- Effect: a company-wide brand comparison went from 8 to 6 LLM calls and from 79s to 47–60s.

**Updating the bucket over time:**
- **Promotion:** 👍 votes are captured (built: Postgres row + Langfuse score `user.feedback` on the trace), giving an analyst review queue. An approved answer becomes a trio, and its SQL is re-executed before ingestion (approval step: design).
- **Re-curation:** when a definition changes in the glossary (see §7), the curator regenerates the affected trios.
- **Gate:** ingestion runs as a Temporal workflow, and the eval suite must pass before the new set is used.

### 2.2 Safety and PII masking

**Layered, with the hard guarantees in code rather than in prompts:**

0. **Input redaction, before any LLM call.** User text is cleaned before the guard, agent, history and Langfuse trace see it.
   - **Local rules:** emails, including obfuscated forms like `eugene dot domer @ gmail dot com`; phone numbers, including spelled-out digits; Luhn-valid cards; coordinates.
   - **Google Sensitive Data Protection:** names, street addresses, IBAN, SSN, passport, IP and date of birth.
   - Name findings that sit entirely inside a known brand ("Ralph Lauren", "Calvin Klein") or one of our pseudonyms are kept, so analysis still works.
   - **Fails closed:** if DLP is unavailable, the turn stops with `E_PII_UNAVAILABLE` and nothing reaches the LLM.
   - The user sees "Removed personal data: email address, person name".
1. **Input guard.** It runs before the orchestrator and is not a tool the model can skip.
   - A regex fast path catches obvious injection.
   - A Flash-Lite classifier rejects off-topic requests and attempts to unmask, escalate or extract prompts.
2. **Scope.** Entitlements (manager → brands) live in Postgres, not in token claims. The API checks that the token role matches the stored role.
3. **Scoping and masking CTEs.** The adapter prepends `WITH users AS (…), orders AS (…), order_items AS (…), products AS (…)`, filtered to the user's brands and masked.
   - Mixed-brand orders count only in-scope items.
   - Qualified table references are rejected, so the CTEs cannot be bypassed.
4. **BigQuery dry run** before execution:
   - statement type must be `SELECT`;
   - referenced tables must be within the 4 allowed tables;
   - the byte estimate must be under the cap.
   Then execution with `maximum_bytes_billed` and a timeout. The service account is read-only.
5. **Masking, not removal**, so the agent can still reason about entities:

   | Field | Masking |
   |---|---|
   | Names, street address | Deterministic HMAC surrogate (salt in Secret Manager) |
   | Email | Surrogate local part, real domain |
   | Postal code | Truncated |
   | Coordinates | Rounded |
   | `user_id` | Pseudonymous token, stable across turns |

   Data rows reach the model, Langfuse and logs only in masked form, and users are identified by a salted hash. Model *output* is built from masked data and checked by the output scan below; DLP on outputs is not built.
6. **Output PII scan** before an answer or report is shown or saved.
7. **Prompt layering.** Code-owned safety rules and the user's scope are appended after the editable prompts and are never published to Langfuse. Persona edits cannot weaken them.

**Proof:**
- Unit tests cover every rule with fakes.
- An 18-case live red team must pass 100%. It covers PII leak, prompt leak, role escalation, SQL smuggling, single- and multi-turn scope bypass, cross-user delete, pre-approved delete, write SQL and off-topic requests.

### 2.3 High-stakes oversight (deleting reports)

Deletion is a deterministic workflow exposed as one tool. The LLM supplies only the filter ("reports mentioning Client X", "reports from this conversation"). It can never approve.

1. **Plan.** Resolve the matching reports, owner-scoped (only the author can see or delete a report). Store the exact report ids in a deletion plan that expires after 5 minutes.
2. **Pause.** LangGraph `interrupt()`, checkpointed in Postgres. The UI shows titles and dates with **Delete / Cancel** buttons; the CLI requires typing `delete`.
3. **Resume** via `POST /v1/sessions/{id}/confirmations`. Approval comes only from the interface, never from model text.
4. **Execute** exactly the previewed ids still owned by the caller. The search is not re-run, so reports created after the preview are safe from the time-of-check/time-of-use race.

Other guarantees:
- A plan can be confirmed once, only by its owner.
- A Temporal schedule expires stale plans every 5 minutes.
- After closing, a plan keeps only ids and a count, which serves as a metadata-only audit trail.
- The delete tool is exclusive. If the model calls it alongside other tools, the call is refused, so a resume never re-runs sibling work.
- "Reports made in this conversation" resolves through `reports.session_id`, not through model memory.

**The Saved Reports library:**
- Every saved report is also a **PDF** with its charts: `GET /v1/reports/{id}/pdf`. An answer that saves a report has the PDF attached, as a download and as a side-panel viewer.
- The **Reports** button in the UI (or `/reports`) lists the newest reports with one-click open.
- Users can also open reports in chat ("open my Q3 report"), via `GET /v1/reports[/{id}]`, or with `opsfleet remote reports` / `remote report <id>`.
- Someone else's report and a missing report both return the same 404, so existence is never revealed.

### 2.4 Learning loop

**User level (built):**
- A typed `UserPreferences` record: format (table / bullets / prose), depth, visuals, language, and up to 5 notes.
- Set through `update_preferences` / `reset_preferences` tools when the user says "I prefer tables".
- Applied to the orchestrator and report writer. Composed before the safety rules, so notes cannot override them.
- *Design:* a nightly Temporal job extracts implicit preferences ("shorter", re-asks, chart requests, 👍/👎). A change is applied only after two consistent signals.

**System level (feedback signal and eval gate built; the mining jobs are design).** Nothing self-modifies production without passing evals:
- **Feedback capture (built):** Chainlit's 👍/👎 (with comment) in the UI, `/good` / `/bad [comment]` in the CLI, `POST /v1/sessions/{id}/feedback`. One vote per user and answer (latest wins), stored in Postgres and mirrored as the Langfuse score `user.feedback`. Comments are PII-redacted first.
- golden promotion (§2.1);
- a "SQL lessons" store built from recurring self-corrections;
- weekly mining of failed traces (SQL errors, empty results, 👎, guard blocks) into a report.

A human edits a prompt or the glossary under a Langfuse `candidate` label. It is promoted to `production` only if the gates hold.

This was exercised for real: a candidate analyst prompt scored 0.80 vs production's 0.83 on 30 cases and was **not promoted** (§7).

### 2.5 Resilience and graceful errors

See [§5](#5-error-handling-and-fallbacks).
- SQL errors and empty results go back to the model as typed messages for bounded self-correction (3 failures per task).
- Loops have step budgets and a per-turn token budget (200k by default). When either runs out, the final step answers with what it has instead of crashing.
- Spend is capped in the gateway: a daily budget and request/token rate limits on the Bifrost virtual key.
- LLM failures fall back across Gemini models in the gateway.
- Prompts survive a Langfuse outage.
- Every error reaches the user as a taxonomy code plus a safe message, never as a stack trace.

### 2.6 Quality assurance

`make eval` drives the real app: Gemini via Bifrost, BigQuery and Postgres. Results go to Langfuse dataset runs with scores on each trace.

| Layer | Method |
|---|---|
| SQL correctness | Leave-one-out over the golden trios: the trio under test is hidden from retrieval. The reference SQL runs live. An LLM judge grades met / partial / unmet against the reference report. Deterministic `column_recall` matches result columns by value. |
| Answer quality | `literal_grounding`: the share of stated numbers that appear verbatim in the fetched rows. The judge also sees the agent's own result tables. |
| Red team | 18 cases with deterministic checks **on state, not wording**: PII regex over replies and every fetched cell, foreign-brand cells, seeded reports still existing, verbatim prompt fragments, read-only SQL. |
| Capabilities (report intent) | 12 multi-turn cases mirroring the brief: top customers, brand X vs Y with "why", monthly and YTD metrics, "what data is available", state underspend, the churn spike, "Q1 report with Q2 action items", and follow-ups ("chart that", "same for last year"). A rubric judge grades intent, groundedness, actionability and clarity. It sees the conversation, saved report bodies, per-turn charts and reports, and every result table. Deterministic checks: report saved for the owner, chart produced, no PII. |
| Operational | Latency p50/p95, LLM calls, queries, self-corrections per turn |

- **Gates (non-zero exit):** red team 100%; mean SQL correctness ≥ 0.75; mean capability score ≥ 0.70.
- **Prompt changes** run against a Langfuse label before promotion.
- **UX evaluation:**
  - Built: the 👍 share from `opsfleet_feedback_total{rating}` and Langfuse `user.feedback`; latency per turn.
  - Design: rephrase rate, abandoned turns, time to first progress event, and a 5% sampled online judge on production traces.

### 2.7 Observability

- **Traces (Langfuse):** one trace per turn with this nesting: `guard` → `orchestrator` → `analyst` / `writer` loops → generations and tools. Each tool span carries its SQL attempts.
- **Root-span metadata:** prompt versions, error codes from self-correction, model fallbacks (requested vs served model), stage counts, outcome and LLM-call count. The user is a salted hash; there is no PII.
- **Metrics (Prometheus, `/metrics`):**
  - turns by outcome code, latency, LLM calls per turn;
  - tool error codes, LLM calls by requested and served model, fallbacks, tokens, tokens per turn;
  - user feedback by rating;
  - BigQuery bytes and executions;
  - prompt fetches by origin (Langfuse / cache / repo) and invalid configs.
  On GKE they are scraped by Managed Prometheus.
- **Deep dive:**
  - every answer and error carries a `trace_id`;
  - `opsfleet debug <trace_id>` prints the question, prompt versions, the step tree with models, tokens and latency, tool inputs and outputs, and the answer;
  - in the UI, the per-user **Debug** switch shows the LLM-call count and a direct Langfuse link.
- **Alerts (design):** SLO burn, any post-mask PII hit (P1), fallback rate > 20%, daily cost, nightly red-team failure.

### 2.8 Persona management

- Business-editable tone lives in the Langfuse prompt `agents/persona`, shared by the orchestrator and report writer.
- The CEO's team edits it in the Langfuse UI and moves the `production` label. It takes effect within the 60s SDK cache TTL with **no redeploy**. This was verified live: a persona edit changed answers in the running process, and the trace showed `agents/persona = langfuse:v2`.
- Rollback means moving the label back.
- Engineering-owned prompts and agent configs (step limits, validated by Pydantic) live in separate prompts. Langfuse RBAC can restrict who edits them.
- *Design:* a webhook-triggered fast red-team subset gates persona promotion.

---

## 3. Technology choices

| Choice | Why | Alternatives considered |
|---|---|---|
| **Gemini** (Flash for most roles, Flash-Lite for the guard and summaries; Pro for analyst and writer in the `production` profile) | Required by the brief. Strong tool calling and SQL. Fallback chain across Gemini models only: one behaviour profile, and separate per-model quotas spread the load. The last step of every chain is the same Gemini model on Vertex AI (separate quota, Workload Identity instead of the API key), so an AI Studio outage or a dead key still gets answers on GKE. Local runs stay AI Studio-only: Bifrost skips the Vertex step when that provider is not configured. A closed billing account still stops both. | — |
| **Per-role reasoning effort** | Gemini's default thinking spent ~700 tokens per SQL step. `low` for agent roles and `medium` for the judge and curator cut p50 latency from 80s to 31s and *raised* correctness from 0.80 to 0.90. Overridable per role via env. | — |
| **Bifrost** gateway | Three LLM consumers (API, Temporal workers, evals) share fallbacks, retries, keys and budgets. It preserves Gemini thought signatures across tool-call turns; direct OpenAI-compatible calls through LangChain failed with 400 in a spike ([`docs/spikes`](docs/spikes)). | LangChain `with_fallbacks` (per-process, no shared budget) |
| **LangGraph** (thin) | `interrupt()` with a Postgres checkpointer is exactly the human-in-the-loop primitive the deletion flow needs. One small `ToolLoop` graph is shared by the orchestrator and the analyst. No LangChain chains, and own context management. | Hand-rolled loop (would need its own durable pause/resume) |
| **Agents as tools** | The orchestrator owns the conversation. The `data_analyst` and `report_writer` sub-agents get a short brief, not the full history, which saves tokens and isolates context. Deterministic tools for schema and deletion. | Single-agent prompt |
| **BigQuery CTE scoping + dry run** | Enforcement without parsing SQL. The production plan adds row access policies and dynamic data masking on an owned dataset copy. | SQL parser rewriting |
| **Postgres + pgvector** | One store for app data, checkpoints and vectors. Hybrid search in one statement. | Separate vector DB |
| **Langfuse** (self-hosted) | Prompts, configs, traces, datasets and scores in one tool, with label-based promotion. Self-hosting keeps traces inside the project. | LangSmith, Vertex AI prompt management |
| **Temporal** | Durable, retryable workflows and schedules (golden ingestion, plan expiry) with the same SDK locally and on GKE. | Cloud Tasks / Cloud Scheduler |
| **Keycloak** | Portable OIDC with device flow for the CLI. Roles in the token, brand entitlements in the DB. | Identity Platform |
| **GKE Autopilot** | Runs the full designed stack (Temporal, Keycloak, Langfuse, ClickHouse) on one platform with Workload Identity. | Cloud Run for the stateless services once the platform pieces are managed |
| **FastAPI + SSE** | Executives wait 10–60s per answer, so progress events ("Querying BigQuery: revenue by brand") stream as they happen. | WebSockets |
| **Chainlit** | Agent steps, inline charts and confirmation buttons with little UI code. A second client of the same API. | Streamlit |

**Experience:**
- Production experience with LLM gateways (including Bifrost), Langfuse tracing and prompt registries, Temporal workers, Keycloak, and GKE with Terraform.
- LangGraph was chosen for its `interrupt()` and checkpointer primitives. It is deliberately used thinly, so its surface is small.

---

## 4. Data flow of one turn

1. **Client → API.** `POST /v1/sessions/{id}/turns` with a Keycloak bearer token. The API verifies the JWT against JWKS, loads the user's `AccessScope` (role + brands) from Postgres, checks the Redis rate limit (10/min, 200/day) and the session owner, then opens an SSE stream.
2. **Redaction, then guard.** Personal data is removed from the question (local rules + DLP; fail closed). Then the regex fast path and the Flash-Lite classifier run. A block ends the turn with a typed code such as `E_INJECTION_DETECTED`.
3. **Context assembly** (char budgets):
   - prompts from Langfuse (60s cache, repo fallback), then code-owned safety rules and scope;
   - user preferences;
   - a rolling summary of turns outside the last 20;
   - the recent turns;
   - a structured analysis state (the last 5 analyst tasks with result references).
4. **Orchestrator loop.** It answers directly, or calls tools:
   - `data_analyst` gets a brief, retrieves golden examples, writes SQL, and goes through `RunQuery` (scope + mask CTEs → dry run → execute). It sees a ≤20-row preview and pages via `get_result`. Results are stored as `result_sets`; raw rows never go into the conversation.
   - `create_chart` renders a PNG from a stored result (numbers can't drift), stored in GCS.
   - `report_writer` writes a report with action items. The PII scan runs before saving.
   - `list_reports` / `open_report` read the user's own library; `delete_reports` plans, interrupts and waits for the confirmation endpoint.
   - `describe_schema` and the preference tools.
5. **Progress events** stream to the client throughout. The final event is `answer` (text, chart ids, trace id, LLM calls), `confirmation` (preview items), or `error` (code, safe message, trace id).
6. **Persistence.** Messages, results, reports and checkpoints go to Postgres; the trace and metrics go to Langfuse and Prometheus.

---

## 5. Error handling and fallbacks

| Failure | Handling |
|---|---|
| SQL syntax or semantic error | Typed `ERROR <code>` returned to the analyst. It self-corrects within a budget of 3 failed queries per task, then explains what it could not do. |
| Empty result | `NO_ROWS` with a hint to check filters and scope. `mgr_empty` demonstrates a graceful low-data answer. |
| Policy violation (write SQL, foreign tables, qualified names, byte cap) | Rejected by the dry-run policy before execution; returned as a typed error. Not retried blindly. |
| Loop runaway or token blow-up | Step budgets at both levels, plus a per-turn token budget shared by every nested call. On exhaustion, one tool-less call answers with the evidence so far. |
| Daily AI budget reached (gateway) | Bifrost returns 402, mapped to `E_LLM_BUDGET`: no retry, no fallback, API status 503, message "try again tomorrow". A gateway rate limit maps to `E_LLM_RATE_LIMITED` (429). |
| PII service (DLP) down or slow | Fails closed after 10s with `E_PII_UNAVAILABLE` (503); nothing is sent to the LLM. `OPSFLEET_PII_REDACTOR=regex` is a documented, weaker offline mode. |
| LLM 429 / 5xx / timeout | Bifrost retries with backoff, then the next model (Flash → older Flash → Flash-Lite → Vertex Flash; Pro → Flash → Flash-Lite → Vertex Flash). Fallbacks are recorded as metrics and on the trace. |
| LLM billing or credential error | Not retried on the same key. Bifrost moves down the chain, so a dead or revoked Gemini key falls through to Vertex AI, which authenticates with Workload Identity. If every step fails, surfaced as `E_LLM_UNAVAILABLE`. |
| Langfuse down | Prompts come from the SDK's stale cache, then from the repo text (verified with Langfuse stopped). Tracing failures never fail a turn. |
| Invalid remote agent config | Pydantic validation fails, so the config is ignored, counted and logged; the last good one stays. |
| BigQuery slow or expensive | Per-query timeout and `maximum_bytes_billed`. |
| Interrupted confirmation | Checkpoint in Postgres. The plan expires after 5 minutes; resuming an expired plan is refused. |
| Any agent error at the boundary | Mapped to a taxonomy code and user-safe message with HTTP status (401/403/404/429/400). The UI and CLI show it inline and stay usable. |

**Cost controls:**
- per-user API rate limits (10/min, 200/day);
- the Bifrost daily budget and rate limits;
- the per-turn token budget;
- BigQuery byte caps;
- per-role reasoning effort;
- a GCP billing budget with alerts;
- result previews instead of full tables in the prompt.

---

## 6. Setup and example run

### 6.1 Local, minimal (CLI only)

Prerequisites:
- Python 3.12 and [`uv`](https://docs.astral.sh/uv/). Plain pip works too: `pip install -r requirements.txt`, exported from `uv.lock` and checked in CI.
- Docker.
- A Gemini API key ([AI Studio](https://aistudio.google.com/apikey)).
- A GCP project with the BigQuery API enabled. The public dataset is free within the monthly tier.
- The Sensitive Data Protection API for input redaction: `gcloud services enable dlp.googleapis.com`. Without it, set `OPSFLEET_PII_REDACTOR=regex` (local rules only).

```bash
git clone https://github.com/Paliy2/opsfleet-demo && cd opsfleet-demo
make install
cp .env.example .env
# set GEMINI_API_KEY, OPSFLEET_GCP_PROJECT and OPSFLEET_PII_HASH_SALT (≥16 random chars)
gcloud auth application-default login       # BigQuery via Application Default Credentials

make up              # Postgres + pgvector, Bifrost
make migrate
make seed            # demo users from a live BigQuery brand ranking, then embed the golden trios
make chat USER_ID=mgr_a
```

`.env.example` works as is for this path. Langfuse and full-stack settings are optional and commented out.

`OPSFLEET_MODEL_PROFILE=free-tier` (the default) uses Flash and Flash-Lite only. Mind the free-key rate limits:
- one question takes 2–10 LLM calls (a full report up to ~18);
- `make seed` embeds 58 trios;
- `make eval` runs 3 cases concurrently, so lower it with `--concurrency 1` and `--per-category 1`.

To add self-hosted Langfuse:
1. Uncomment the `OPSFLEET_LANGFUSE_*` lines in `.env` (all or nothing).
2. Fill in the `LANGFUSE_*` secrets (`openssl rand -hex 32`).
3. Run `make up-obs && make prompts-push`.

`make up-obs` names any secret that is missing. Without Langfuse, the app runs on the repo prompts with metrics only.

### 6.2 Local, full stack (mirrors production)

```bash
# needs the Langfuse settings from 6.1, plus KEYCLOAK_ADMIN_PASSWORD, KEYCLOAK_DEMO_PASSWORD
# and CHAINLIT_AUTH_SECRET in .env; make up-full names anything missing
make up-full
```

This starts Keycloak (:8180), Redis, the Temporal dev server (UI on :8233), the API (:8000), the worker, a one-shot bootstrap (migrate → seed → golden trios via Temporal → publish prompts), and Chainlit (:8001, dev login with any demo user id).

The CLI with real SSO against the local API:

```bash
uv run opsfleet remote login --api http://localhost:8000   # approve the device code as mgr_a / $KEYCLOAK_DEMO_PASSWORD
uv run opsfleet remote chat
```

### 6.3 Against the live deployment

```bash
uv run opsfleet remote login --api https://api.136-81-227-113.sslip.io
uv run opsfleet remote chat
uv run opsfleet remote sessions                 # your past conversations
uv run opsfleet remote chat --resume <id>       # continue one with its context
```

Or open the UI and sign in through Keycloak. Past conversations are in the sidebar. Opening one reloads its messages and the agent continues with the same context (summary and analysis state). Delete removes its messages, results and agent checkpoints; saved reports stay in **Reports**.

### 6.4 Example session

A real run on 2026-10-08, trimmed. Progress lines are dimmed in the terminal.

```text
$ make chat USER_ID=mgr_a
OpsFleet analyst — signed in as mgr_a. /new, /exit
you: Total revenue of my brands last month?
  Querying: Calculate September 2026 revenue and revenue share by brand for in-scope brands
 • Total Revenue (September 2026): $21,661.37 across your three brands.
 • Diesel: $8,326.43 (38.4%) · Calvin Klein: $6,668.94 (30.8%) · 7 For All Mankind: $6,666.00 (30.8%)
 (Net sales, excluding cancelled and returned items.)
llm calls: 4 · results: 1 · trace: b837caff4eddbdbbf74a407c73ec5101

you: How does Carhartt compare?
 • Access Restriction: Carhartt is outside your brand access scope.
llm calls: 1 · results: 0 · trace: a370fe467e9610e17170fadf7ce39b6b

you: Who are my top 5 customers by lifetime spend?
(Note: Customer personal details and names shown below are pseudonyms/masked values.)
 • Morgan Costa (Japan): $1,285.00 (3 orders)
 • Morgan Fischer (United States): $1,096.00 (2 orders)
 …
llm calls: 4 · results: 1 · trace: b87e534c459b42fcfb61bc2022e91740

you: Write a short report on my brands in Q3 2026 with action items for Q4
  Analyzing: … compare to Q2 2026 …            (two analyst tasks in parallel, 6 queries)
  Writing report: Q3 2026 Performance Report & Q4 Action Items …
Executive Summary
 • Total Portfolio Net Revenue: $54,201.04 in Q3 2026, up +28.28% QoQ from $42,251.70 in Q2.
 …
Q4 Action Items
 • Audit Diesel Stock & Fulfillment: … sweater (36.4%) and outerwear (25.0%) cancellations before peak demand.
 • Implement AOV Protection for Diesel: bundle offers or free-shipping thresholds to counteract the -19.43% AOV drop.
 …
llm calls: 10 · results: 6 · trace: 579a5c32a8f886340b03c91c59345399

you: Delete all reports we made in this conversation
                     Reports to delete permanently
┃ Title                                             ┃ Created          ┃
│ Q3 2026 Performance Report: Three-Brand Portfolio │ 2026-10-08 15:08 │
Delete these 1 report(s)? Type 'delete' to confirm: delete
 • Status: Successfully deleted. 1 report created in this conversation has been permanently removed.
llm calls: 2 · results: 0 · trace: 5a2f94f05067ed3e7347028463f7474a
```

Inspect any turn:

```bash
make debug TRACE=b837caff4eddbdbbf74a407c73ec5101
```

### 6.5 Tests and evals

```bash
make check                     # format, lint (ruff + bandit), mypy --strict, import-linter, unit tests
make test-int                  # Postgres integration tests (needs `make up`)
make eval                      # full eval + red team against real Gemini and BigQuery
uv run python -m evals.run --suite redteam
uv run python -m evals.run --suite capability --concurrency 2
uv run python -m evals.run --per-category 1 --publish
```

### 6.6 Cloud deployment

Everything is in `infra/`:

- **`terraform/bootstrap`** (applied once): APIs, the state bucket, GitHub Workload Identity Federation and the deployer service account.
- **`terraform/main`**: VPC with NAT, GKE Autopilot (private nodes), Cloud SQL (private IP), Artifact Registry, GCS, Secret Manager entries with generated values, IAM, and a billing budget.
- **`helm/opsfleet`**: the app and platform charts, plus the official Temporal and External Secrets charts.

**CI/CD (GitHub Actions):**
- Every PR runs `make check`, integration tests, `terraform validate`, `helm lint` and an image build, and gets a Terraform plan comment.
- A merge to `main` runs `terraform apply`, builds and pushes the image tagged with the commit SHA, runs `infra/scripts/deploy.sh` (Helm), and smoke-tests `/readyz`. A deploy takes about 6 minutes.

There are no JSON keys anywhere. GitHub's OIDC token is exchanged for a short-lived token of the deployer service account, restricted to this repository.

The one manual step is adding the Gemini key to Secret Manager:

```bash
printf %s "$KEY" | gcloud secrets versions add gemini-api-key --data-file=-
```

---

## 7. Evaluation results

Measured on the live stack (details in [DECISIONS §31](docs/DECISIONS.md)):

| Run | Correctness (mean) | Literal grounding | p50 / p95 latency | LLM calls / queries | Red team |
|---|---|---|---|---|---|
| 10 cases, default reasoning | 0.80 | — | 80s / 217s | 8.0 / 4.1 | 16/18 |
| 10 cases, **low reasoning** (shipped) | **0.90** | — | **31s / 97s** | 5.1 / 2.0 | **18/18** |
| 30 cases, production prompts | 0.83 | 0.83 | 35s | — | — |
| 30 cases, candidate prompt | 0.80 | 0.86 | 34s | — | not promoted |

**Capability suite** (12 multi-turn cases, free-tier models; [DECISIONS §36](docs/DECISIONS.md)):

| Passed | Mean capability score | Intent / groundedness / actionability | Literal grounding | p50 / p95 per conversation | LLM calls / queries |
|---|---|---|---|---|---|
| 11/12 | 0.99 (gate 0.70) | 1.00 / 1.00 / 1.00 | 0.76 | 50s / 120s | 7.4 / 3.3 |

- **The one miss:** "top customers by lifetime spend" did not say which period and order statuses the total covers.
- **Several first-run misses were in the eval, not the agent.** The judge could not see charts, and the rubric expected tables outside the four in scope. Fixing them also showed that the judge needs the artifacts, not just the prose.
- **The judge is lenient**, scoring nearly everything 1.00. Treat this suite as a regression gate. Report quality still needs human spot checks, and harder cases are the next step.
- **Literal grounding is lower than the judge's groundedness** because derived figures (shares, growth) never appear verbatim in the result rows.

**What the misses are.** All 10 misses in the 30-case run were *partial*; none was wrong. Seven disagree only on whether order counts exclude cancelled and returned items. The glossary is ambiguous there, and the curated golden SQL mixes both conventions. The next learning-loop action is to make "net orders" explicit, re-curate and re-run. The fix belongs in definitions and data, not in the prompt.

**Bug found by the red team.** A "delete everything" plan overflowed the idempotency-key column. The key is now a hash, with a regression test.

---

## 8. Repository layout and engineering standards

```
src/opsfleet_agent/
  api/ cli/ ui/     inbound adapters (FastAPI, Typer, Chainlit) — independent of each other
  workers/          Temporal workflows, activities, schedule
  agents/           LangGraph ToolLoop, orchestrator, analyst, writer, guard, summarizer, curator
  interactors/      use cases and business rules (scoping, ownership, deletion, PII redaction, feedback) — pure
  ports/            Protocols for every external system
  repos/ gateways/  Postgres adapters; BigQuery, LLM, DLP, Langfuse, auth, storage, rate limit, API client
  entities/ models/ Pydantic domain types; SQLAlchemy models
  core/             settings (pydantic-settings), error taxonomy, model rosters
  prompts/          prompt sources (folder name = Langfuse prompt name)
evals/              eval harness, judges, SQL / red-team / capability suites
migrations/         Alembic
data/golden/        curated golden trios
infra/              compose, terraform (bootstrap, main), helm, deploy script
docs/               DECISIONS.md, spikes, architecture diagrams
```

**Rules:**
- Dependencies point inward, and import-linter fails the build otherwise. Interactors import only ports and entities.
- Every external system sits behind a port. Tests use fakes, not SDK mocks.
- Every security rule has a red-team unit test.
- `mypy --strict`, ruff, bandit; no `except Exception`; typed errors with taxonomy codes; fail-fast settings (e.g. `OPSFLEET_ENV` is required, so a missing value can never silently enable dev logins).
- Pre-commit runs the checks plus a secret scan.

---

## 9. Prototype vs production, and what is not built

| Concern | This repo | Production plan |
|---|---|---|
| Scoping and masking | CTE shadowing + dry-run policy on the public dataset | Owned dataset copy with row access policies, policy tags and dynamic data masking (engine-enforced) |
| Free-text PII | User input: local rules + Sensitive Data Protection, fail closed. Output: regex scan. | Add DLP on outputs and on golden trios |
| Golden bucket | Repo JSONL → Temporal ingestion | GCS bucket, promotion workflow with analyst approval |
| Learning loop | Explicit preferences; 👍/👎 capture; eval-gated prompt promotion (run manually) | Nightly preference extraction, analyst approval queue for golden promotion, SQL lessons, weekly failure mining |
| Conversation history | Sidebar list, reopen and delete through the API with the user's own token; titles from the first redacted question | Server-side search, retention policy, export |
| Observability | Langfuse, Prometheus metrics, debug CLI and UI switch | + OTel Collector, Grafana dashboards, PrometheusRule alerts with receivers |
| Gateway | FastAPI JWT + Redis rate limit behind GCE Ingress; LLM spend capped in Bifrost | Kong/APISIX in front for request limits and API keys |
| LLM budget state | Bifrost counters live in an `emptyDir` and reset when the pod restarts | Persistent Bifrost config store |
| Evals in CI | `evals.yml` on manual dispatch; needs an eval service account and secrets that are not provisioned | Nightly run and a required gate on prompt/config promotion |
| Availability | Single replicas, Cloud SQL without HA | HA Cloud SQL, multiple replicas, Temporal Cloud |

**Deliberately skipped:** voice channel, MCP server, non-Gemini LLM providers. Cross-provider fallback to Vertex AI is built (Bifrost config, Terraform IAM, Helm) and unit/render tested, but not yet exercised against live Vertex; see [DECISIONS §38](docs/DECISIONS.md).
