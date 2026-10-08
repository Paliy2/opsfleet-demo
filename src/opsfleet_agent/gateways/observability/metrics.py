from prometheus_client import Counter, Histogram, start_http_server

TURNS = Counter("opsfleet_turns", "Chat turns by kind and outcome code", ["kind", "outcome"])
TURN_SECONDS = Histogram(
    "opsfleet_turn_duration_seconds",
    "End-to-end turn latency",
    ["kind"],
    buckets=(1, 3, 5, 10, 20, 30, 45, 60, 90, 120, 180, 300),
)
TURN_LLM_CALLS = Histogram(
    "opsfleet_turn_llm_calls", "LLM calls per turn", ["kind"], buckets=(1, 2, 4, 8, 12, 16, 24, 32)
)
AGENT_EVENTS = Counter("opsfleet_agent_events", "Agent progress events by stage", ["stage"])
TOOL_ERRORS = Counter(
    "opsfleet_tool_errors", "Errors returned to agents for self-correction", ["code"]
)
LLM_CALLS = Counter(
    "opsfleet_llm_calls", "LLM calls by requested and served model", ["requested", "served"]
)
LLM_FALLBACKS = Counter(
    "opsfleet_llm_fallbacks", "Calls served by a fallback model", ["requested", "served"]
)
LLM_ERRORS = Counter("opsfleet_llm_errors", "Failed LLM calls", ["requested", "error"])
LLM_TOKENS = Counter("opsfleet_llm_tokens", "Tokens by served model", ["model", "direction"])
LLM_SECONDS = Histogram(
    "opsfleet_llm_latency_seconds",
    "LLM call latency",
    ["model"],
    buckets=(0.25, 0.5, 1, 2, 4, 8, 15, 30, 60),
)
BQ_BYTES = Counter(
    "opsfleet_bigquery_bytes_processed", "BigQuery bytes processed by executed queries"
)
BQ_QUERIES = Counter("opsfleet_bigquery_queries", "BigQuery executions by outcome", ["outcome"])
PROMPT_FETCHES = Counter(
    "opsfleet_prompt_fetches", "Prompt fetches by name and origin", ["name", "origin"]
)
PROMPT_CONFIG_INVALID = Counter(
    "opsfleet_prompt_config_invalid", "Remote agent configs rejected by validation", ["name"]
)


def serve_metrics(port: int) -> None:
    start_http_server(port)
