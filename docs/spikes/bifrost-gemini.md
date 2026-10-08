# Spike: Gemini function calling through the Bifrost gateway

Date: 2026-10-08. Bifrost `v2.2.6` (`maximhq/bifrost:latest`, Docker). openai `2.x` SDK, langchain-core `1.6.7`, langchain-openai (latest).

## Verdict: GO, with caveats. Live Gemini still needs checking.

**No Gemini API key was available** (`GEMINI_API_KEY` and `GOOGLE_API_KEY` were unset, and there are no `.env` files in the repo). So **no request reached real Gemini**. To test without a key, I ran real Bifrost against a **mock of the native Gemini v1beta API** (`generateContent` and `streamGenerateContent`). The mock logs every payload Bifrost sends upstream. This tests Bifrost's OpenAI-to-Gemini translation in both directions, plus its fallback and retry logic. It does not test how the real model behaves, or whether Google accepts the payload Bifrost builds. Before committing, re-run the same script against real Gemini (about 15 calls, see "Re-run live" below).

Why GO: Bifrost's translation layer handles the case where calling Gemini directly through LangChain is known to break. Gemini 3 models require the **thought signature** to be sent back with each function call. Gemini's own OpenAI-compatible endpoint returns the signature in the non-standard field `tool_calls[].extra_content.google.thought_signature`. `ChatOpenAI` drops that field, so the next turn fails with `400 Function call is missing a thought_signature` (see the sources at the end). Bifrost avoids this by **encoding the signature into the `tool_call.id`** (`run_sql_ts_<b64sig>`). LangChain round-trips that id unchanged through `ToolMessage.tool_call_id`, and Bifrost decodes it back into `thoughtSignature` on the next request. The mock log confirmed the original signature bytes came back unchanged.

## Results (Bifrost → mock Gemini)

| # | Test | openai SDK | LangChain `ChatOpenAI.bind_tools` | Notes |
|---|------|-----------|-----------------------------------|-------|
| a | Single tool call | PASS | PASS | `finish_reason=tool_calls`, args are a JSON string, `content=None`. The thought text does **not** leak into `content`. It goes to `message.reasoning_details` instead. |
| b | Multi-turn (`role=tool`, `tool_call_id`) | PASS | PASS | Upstream payload: `functionCall` + `functionResponse` with the correct name and id. **The thought signature round-tripped byte-exact.** |
| c | Parallel tool calls (two different tools) | PASS | PASS | Two calls with unique ids. On the return trip, the two tool messages were merged into one Gemini `user` turn with two `functionResponse` parts. The first call carries the real signature; Bifrost fills the second with the documented dummy `skip_thought_signature_validator`. |
| c' | Parallel, **same tool three times** | **ISSUE** | **ISSUE** | When Gemini returns no `functionCall.id`, Bifrost uses the function name as the id. The result is `['run_sql_ts_…', 'run_sql', 'run_sql']`, i.e. **duplicate tool_call ids**. The round trip still completed, because Gemini matches responses by order. See Gotcha 1. |
| d | Streaming with tool calls | PASS | PASS | Each tool call arrives as one complete delta (whole args), indexed 0 and 1. `AIMessageChunk` aggregation gives valid `tool_calls`, with `invalid_tool_calls=[]`. |
| e | Nested schema (enum, array of objects, `["string","null"]`, `anyOf`, `additionalProperties:false`, Pydantic `Optional[Literal]`) | PASS | PASS | Bifrost sends the schema **unchanged** as `parametersJsonSchema`, not the OpenAPI-subset `parameters`, so no translation is lossy. `tool_choice="required"` maps to `mode: ANY`. A named tool maps to `ANY` + `allowedFunctionNames`. Whether real Gemini accepts all of these keywords is unverified. |
| f1 | Fallback per request (`extra_body={"fallbacks": [...]}`) | PASS | PASS | Primary model returned `404 NOT_FOUND`, then the next model in the chain served the request. The response has `extra_fields.routing_info.is_fallback=true` plus `primary_model`. LangChain shows the served model in `response_metadata.model_name`. |
| f2 | Fallback from config (routing rule on model alias `agent-default`) | PASS | n/a (same HTTP) | Upstream hops: `ratelimited` ×2 (1 try + 1 retry) → `flash`, total 0.5 s. **Response headers** show the serving model: `x-bifrost-resolved-model`, `x-bifrost-provider`, `x-bifrost-routing-info-primary-model`, `x-bifrost-original-model`. |
| g | 429 handling | PASS | n/a | `max_retries: 1` gave 2 upstream attempts with about 0.5 s backoff, then the client got `429` with Gemini's message. `extra_fields.retry_after_ms` was **absent**, even though the mock sent `google.rpc.RetryInfo`; this is minor. 404 does **not** retry on a single key. It goes straight to the fallback. |
| – | Gemini direct OpenAI-compatible endpoint | BLOCKED (no key) | BLOCKED | From research: `ChatOpenAI` drops `extra_content.google.thought_signature`, so multi-turn tool use on Gemini 3 fails. To make it work you need `langchain-google-genai` or a custom client. |

Raw output is in the scratchpad: `run_mock.log`, plus upstream payloads in `mock_requests.jsonl`.

## Gemini model IDs (from ai.google.dev, 2026-10-08; not verified with `models.list` because there was no key)

| Role | Model ID | Free tier |
|------|----------|-----------|
| Primary ("Pro") | `gemini-3.8-flash` (`gemini-3.1-pro-preview` is paid-only) | yes |
| Fallback 1 | `gemini-3.6-flash` | yes |
| Fallback 2 ("Lite") | `gemini-3.5-flash-lite` (or `gemini-3.1-flash-lite`) | yes |

The 2.5 models are restricted to existing users, and 2.0 is shut down. **Run `GET https://generativelanguage.googleapis.com/v1beta/models?key=…` to confirm before you rely on these IDs.**

## Working config (`<app-dir>/config.json`, mount it at `/app/data`)

```json
{
  "providers": {
    "gemini": {
      "keys": [{ "name": "gemini-free", "value": "env.GEMINI_API_KEY", "models": ["*"], "weight": 1.0 }],
      "network_config": { "max_retries": 2, "retry_backoff_initial": 1000, "retry_backoff_max": 8000 }
    }
  },
  "governance": {
    "virtual_keys": [
      { "id": "vk-agent", "name": "data-agent", "value": "sk-bf-data-agent", "is_active": true, "allow_all_providers": true }
    ],
    "routing_rules": [
      {
        "id": "agent-default-chain", "name": "agent-default chain", "enabled": true, "scope": "global", "priority": 0,
        "cel_expression": "model == \"agent-default\"",
        "targets": [{ "provider": "gemini", "model": "gemini-3.8-flash", "weight": 1 }],
        "fallbacks": ["gemini/gemini-3.6-flash", "gemini/gemini-3.5-flash-lite"]
      }
    ]
  },
  "plugins": [
    { "enabled": true, "name": "otel", "config": {
        "service_name": "bifrost", "collector_url": "https://cloud.langfuse.com/api/public/otel",
        "trace_type": "genai_extension", "protocol": "http",
        "headers": { "Authorization": "env.LANGFUSE_AUTH" } } }
  ]
}
```

```bash
docker run -p 8080:8080 -e GEMINI_API_KEY -e LANGFUSE_AUTH -v $PWD/bifrost-data:/app/data maximhq/bifrost:latest
```

```python
llm = ChatOpenAI(base_url="http://localhost:8080/v1", api_key="sk-bf-data-agent",
                 model="agent-default", max_retries=0).bind_tools(tools)
# or pin a model and pass the chain per request:
# ChatOpenAI(..., model="gemini/gemini-3.8-flash", extra_body={"fallbacks": ["gemini/gemini-3.6-flash"]})
```

I did not exercise the OTel/Langfuse plugin. The config block is copied from the Bifrost docs.

## Gotchas

1. **Duplicate tool_call ids for repeated calls to the same tool.** This happens when Gemini omits `functionCall.id`. The id falls back to the function name, and only the first call gets the signature suffix, so the second and third calls to `run_sql` both get `id="run_sql"`. The round trip still worked through Bifrost. The risk is anything keyed by `tool_call_id`: tracing UIs, or custom routing in LangGraph (the prebuilt `ToolNode` still worked). Mitigations:
   - Check whether real Gemini 3 returns ids.
   - Or tell the agent to batch SQL into a single call.
   - Or rewrite duplicate ids in a thin wrapper. Ids without the `_ts_` suffix carry no signature, so rewriting them is safe.
2. **Thought signatures.** Bifrost handles them through the id encoding, and also through `reasoning_details` (`reasoning.encrypted`, id `tool_call_<id>`). **Do not shorten, rewrite or regenerate tool_call ids** that contain `_ts_`. Ids are long, around 30+ characters plus the base64 signature, so don't put length limits on them.
3. **A virtual key is required by default** (`401 virtual_key_required`). Pass it as `Authorization: Bearer sk-bf-…`, which is the `api_key` in the OpenAI SDK. With `allow_all_providers`, a model that is not in the provider's listed models gets `403 model_blocked` before any upstream call. That error still triggers the fallback chain, but a typo in a model name can look like a policy error.
4. **SSRF guard.** A custom `base_url` that points at a private IP is refused (`connection to private IP … is not allowed`) unless `network_config.allow_private_network: true` is set. This matters for any in-cluster proxy or mock.
5. **Fallbacks are declared per request** (`fallbacks` in the body or `extra_body`). They can also be declared centrally through a **routing rule** (CEL on `model` or a header) or through **virtual-key provider weights**. Each hop gets its own full `max_retries` budget, so with 2 retries and 3 models that is up to 9 upstream calls. Keep retries low on the free tier.
6. **429 behaviour.** With a single key, Bifrost retries the same key with exponential backoff (`500ms·2^n`, capped, ±20% jitter). Bifrost does **not** follow Gemini's `RetryInfo` delay; that delay is informational only. With several keys, Bifrost rotates between them on 429.
7. **Ignored parameter.** `parallel_tool_calls` is silently dropped for Gemini, so you cannot force serial tool calls this way.
8. **Config reload.** In the default DB-backed mode, `config.json` seeds SQLite at startup, and file changes need a restart (I restarted for each change). The UI or the `/api/providers` and `/api/routing/rules` endpoints apply changes live. For a stateless deploy, use file-only mode (`config_store.enabled: false`), which also needs a restart.
9. **The first boot logs a warning** about the admin/setup token (`BIFROST_SETUP_TOKEN`). Set it before you expose the UI.
10. **Observability.** The built-in OTel plugin emits one span per attempt, so retries and fallbacks each get their own span, using GenAI semconv. It exports to Langfuse over OTLP HTTP and respects an inbound W3C `traceparent`, so the LangGraph and Bifrost spans can share one trace. Datadog, Prometheus and Maxim plugins also exist.

## Recommendation

Use **LangGraph + `ChatOpenAI` → Bifrost → Gemini**. Declare the fallback chain once as a routing rule behind a model alias (`agent-default`) rather than in application code. Set the provider to `max_retries` 1–2. Before you lock the architecture, spend about 10 minutes running the live checks below. If live c' shows real Gemini returns no `functionCall.id`, add the id-dedup wrapper. If live tests fail on signatures, fall back to `langchain-google-genai` directly, losing gateway features.

**Re-run live** (scratchpad `bifrost-spike/`): add the real `gemini` provider block above to `data/config.json`, restart, then run:

```bash
API_KEY=sk-bf-spike-local MODEL=gemini/gemini-3.8-flash \
FALLBACKS=gemini/gemini-3.6-flash,gemini/gemini-3.5-flash-lite \
BAD_MODEL=gemini/gemini-nonexistent RL_MODEL= .venv/bin/python spike_tests.py
# direct comparison:
DIRECT=1 BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/ API_KEY=$GEMINI_API_KEY MODEL=gemini-3.8-flash .venv/bin/python spike_tests.py
```

Artifacts are in `/private/tmp/claude-501/-Users-yevhendomeretskyi-opsfleet-demo/7573257d-5b82-478b-83fd-74dfc05155a7/scratchpad/bifrost-spike/`:
- `spike_tests.py`: the test suite, covering the openai SDK and LangChain.
- `mock_gemini.py`: the native Gemini API mock.
- `same_tool_ids.py`: the repro for case c'.
- `data/config.json`: the config used for the mock run.
- `run_mock.log`, `mock_requests.jsonl`: raw output and upstream payloads.

Sources:
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [OpenAI compat broken due to thought_signature](https://discuss.ai.google.dev/t/openai-api-compatibility-broken-due-to-thought-signature-on-gemini-3-pro-preview/109823)
- [LangChain forum: thought signatures via langchain-openai](https://forum.langchain.com/t/does-langchain-openai-support-passing-gemini-3-s-thought-signatures-metadata-via-openrouter/2271)
- [Gemini models](https://ai.google.dev/gemini-api/docs/models)
- [Gemini pricing / free tier](https://ai.google.dev/gemini-api/docs/pricing)
- Bifrost repo docs (`docs/features/retries-and-fallbacks.mdx`, `docs/providers/routing-rules.mdx`, `docs/providers/supported-providers/gemini.mdx`, `docs/features/observability/otel.mdx`) and source (`core/providers/gemini/chat.go`, `utils.go`).

## Live Results (2026-10-08, real Gemini, paid tier)

> **Later change (§36):** the app sends the fallback chain per request, and the `agent-default` routing alias tested here was removed from the gateway config.

**Verdict: GO.** 13/13 pass through Bifrost with `gemini-3.8-flash` (OpenAI SDK + LangChain `ChatOpenAI`): single, multi-turn, parallel, streaming tool calls, nested schemas, per-request and routing-rule fallbacks (`agent-default` alias), 429 retry.

- Real Gemini returns **unique tool-call ids** (`unique_ids=True`, id length ~422 with embedded thought signature) — duplicate-id issue was mock-only. Never truncate ids.
- Fallback confirmed: `model_name=gemini-3.6-flash` served after primary failure.
- Model writes **fully-qualified table names** by default (`bigquery-public-data.thelook_ecommerce.orders`) → confirms the CTE-bypass check in DECISIONS §5 is mandatory, and the analyst prompt must require unqualified names.
- **Billing failure mode:** depleted prepaid credits → Gemini `RESOURCE_EXHAUSTED` → Bifrost `502 upstream_credentials_exhausted`. All models share project billing, so fallback cannot help.
- Compose needs Bifrost `config_store` (sqlite) for virtual keys; file-only mode fails to boot with governance enabled.
