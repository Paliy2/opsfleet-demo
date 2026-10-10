# Spike: Gemini function calling through the Bifrost gateway

Bifrost `v2.2.6` (Docker), openai `2.x` SDK, langchain-core `1.6.7`, langchain-openai.

## Verdict: go

I ran the same suite twice: first against a mock of the native Gemini API that logs every upstream payload, then against real Gemini (`gemini-3.8-flash`, paid tier). All 13 cases passed live, through both the openai SDK and LangChain `ChatOpenAI`: single, multi-turn, parallel and streaming tool calls, nested schemas, per-request and routing-rule fallbacks, and 429 retry.

Why it matters: Gemini 3 needs the thought signature sent back with every function call. Gemini's own OpenAI-compatible endpoint returns it in the non-standard field `tool_calls[].extra_content.google.thought_signature`, `ChatOpenAI` drops that field, and the next turn fails with `400 Function call is missing a thought_signature` (sources at the end). Bifrost encodes the signature into the `tool_call.id` (`run_sql_ts_<b64sig>`). LangChain round-trips the id unchanged through `ToolMessage.tool_call_id`, and Bifrost decodes it back into `thoughtSignature` on the next request. The mock log showed the signature bytes coming back unchanged.

## Live results (real Gemini)

- Real Gemini returns unique tool-call ids (about 422 characters with the embedded signature). The duplicate-id issue seen against the mock (Gotcha 1) does not happen live. Never truncate ids.
- Fallback works: `model_name=gemini-3.6-flash` served the request after the primary failed.
- The model writes fully-qualified table names by default (`bigquery-public-data.thelook_ecommerce.orders`). That confirmed the CTE-bypass check in the query engine is mandatory, and the analyst prompt requires unqualified names.
- Billing failure mode: depleted prepaid credits give Gemini `RESOURCE_EXHAUSTED`, which Bifrost returns as `502 upstream_credentials_exhausted`. Every model shares the project's billing, so a fallback cannot help.
- Compose needs Bifrost's `config_store` (SQLite) for virtual keys; file-only mode fails to boot with governance enabled.
- The app later moved to sending the fallback chain per request; the `agent-default` routing alias tested here is no longer in the gateway config.

## Mock results (Bifrost → Gemini API mock)

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


## Gemini model IDs

| Role | Model ID |
|------|----------|
| Primary | `gemini-3.8-flash` (`gemini-3.1-pro-preview` is paid-only) |
| Fallback 1 | `gemini-3.6-flash` |
| Fallback 2 (Lite) | `gemini-3.5-flash-lite` |

The 2.5 models are restricted to existing users, and 2.0 is shut down.

## Config used in the spike (`config.json`, mounted at `/app/data`)

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

The OTel plugin block follows the Bifrost docs; the spike did not exercise it.

## Gotchas

1. **Duplicate tool_call ids for repeated calls to the same tool.** This happens when Gemini omits `functionCall.id`. The id falls back to the function name, and only the first call gets the signature suffix, so the second and third calls to `run_sql` both get `id="run_sql"`. The round trip still worked through Bifrost. The risk is anything keyed by `tool_call_id`: tracing UIs, or custom routing in LangGraph (the prebuilt `ToolNode` still worked). Mitigations:
   Real Gemini returns ids, so this never happened live. If it does, rewrite duplicate ids in a thin wrapper: ids without the `_ts_` suffix carry no signature, so rewriting them is safe.
2. **Thought signatures.** Bifrost handles them through the id encoding, and also through `reasoning_details` (`reasoning.encrypted`, id `tool_call_<id>`). **Do not shorten, rewrite or regenerate tool_call ids** that contain `_ts_`. Ids are long, around 30+ characters plus the base64 signature, so don't put length limits on them.
3. **A virtual key is required by default** (`401 virtual_key_required`). Pass it as `Authorization: Bearer sk-bf-…`, which is the `api_key` in the OpenAI SDK. With `allow_all_providers`, a model that is not in the provider's listed models gets `403 model_blocked` before any upstream call. That error still triggers the fallback chain, but a typo in a model name can look like a policy error.
4. **SSRF guard.** A custom `base_url` that points at a private IP is refused (`connection to private IP … is not allowed`) unless `network_config.allow_private_network: true` is set. This matters for any in-cluster proxy or mock.
5. **Fallbacks are declared per request** (`fallbacks` in the body or `extra_body`). They can also be declared centrally through a **routing rule** (CEL on `model` or a header) or through **virtual-key provider weights**. Each hop gets its own full `max_retries` budget, so with 2 retries and 3 models that is up to 9 upstream calls. Keep retries low on the free tier.
6. **429 behaviour.** With a single key, Bifrost retries the same key with exponential backoff (`500ms·2^n`, capped, ±20% jitter). Bifrost does **not** follow Gemini's `RetryInfo` delay; that delay is informational only. With several keys, Bifrost rotates between them on 429.
7. **Ignored parameter.** `parallel_tool_calls` is silently dropped for Gemini, so you cannot force serial tool calls this way.
8. **Config reload.** In the default DB-backed mode, `config.json` seeds SQLite at startup, and file changes need a restart. The UI or the `/api/providers` and `/api/routing/rules` endpoints apply changes live. For a stateless deploy, use file-only mode (`config_store.enabled: false`), which also needs a restart.
9. **The first boot logs a warning** about the admin/setup token (`BIFROST_SETUP_TOKEN`). Set it before you expose the UI.
10. **Observability.** The built-in OTel plugin emits one span per attempt, so retries and fallbacks each get their own span, using GenAI semconv. It exports to Langfuse over OTLP HTTP and respects an inbound W3C `traceparent`, so the LangGraph and Bifrost spans can share one trace. Datadog, Prometheus and Maxim plugins also exist.

## Outcome

The app uses LangGraph + `ChatOpenAI` → Bifrost → Gemini, with provider `max_retries` at 1–2 and the fallback chain sent per request. The fallback would have been `langchain-google-genai` directly, losing the gateway features (budgets, fallbacks, one OpenAI-compatible surface for every model).

Sources:
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [OpenAI compat broken due to thought_signature](https://discuss.ai.google.dev/t/openai-api-compatibility-broken-due-to-thought-signature-on-gemini-3-pro-preview/109823)
- [LangChain forum: thought signatures via langchain-openai](https://forum.langchain.com/t/does-langchain-openai-support-passing-gemini-3-s-thought-signatures-metadata-via-openrouter/2271)
- [Gemini models](https://ai.google.dev/gemini-api/docs/models)
- [Gemini pricing / free tier](https://ai.google.dev/gemini-api/docs/pricing)
- Bifrost repo docs (`docs/features/retries-and-fallbacks.mdx`, `docs/providers/routing-rules.mdx`, `docs/providers/supported-providers/gemini.mdx`, `docs/features/observability/otel.mdx`) and source (`core/providers/gemini/chat.go`, `utils.go`).
