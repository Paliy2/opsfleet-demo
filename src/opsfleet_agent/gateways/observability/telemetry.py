from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from uuid import UUID, uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langfuse import Langfuse, LangfuseAgent, propagate_attributes

from opsfleet_agent.core.errors import AgentError, ErrorCode
from opsfleet_agent.core.identity import user_hash
from opsfleet_agent.entities.conversation import AgentReply, ProgressEvent
from opsfleet_agent.entities.telemetry import TurnKind
from opsfleet_agent.gateways.llm.token_budget import TokenMeter, metered_turn
from opsfleet_agent.gateways.llm.trace_propagation import LlmParentSpanCallback
from opsfleet_agent.gateways.observability import metrics
from opsfleet_agent.gateways.observability.agent_spans import AgentSpanHandler
from opsfleet_agent.gateways.observability.llm_metrics import LlmMetricsCallback
from opsfleet_agent.gateways.observability.turn_context import TurnRecord, recording_turn
from opsfleet_agent.ports.telemetry import Telemetry, TurnTrace

_POLICY_BLOCKS = frozenset(
    {ErrorCode.OFF_TOPIC, ErrorCode.INJECTION_DETECTED, ErrorCode.PII_DETECTED}
)
_UNHANDLED = "E_INTERNAL"


class _Turn(TurnTrace):
    def __init__(
        self, trace_id: str, record: TurnRecord, meter: TokenMeter, span: LangfuseAgent | None
    ) -> None:
        self._trace_id = trace_id
        self._record = record
        self._span = span
        self._meter = meter
        self.llm_calls = 0

    @property
    def trace_id(self) -> str:
        return self._trace_id

    def emit(self, event: ProgressEvent) -> None:
        self._record.stages[event.stage] += 1
        metrics.AGENT_EVENTS.labels(event.stage).inc()
        if event.code is not None:
            self._record.error_codes.append(event.code)
            metrics.TOOL_ERRORS.labels(event.code).inc()

    def complete(self, reply: AgentReply) -> None:
        self.llm_calls = reply.llm_calls
        if self._span is not None:
            self._span.update(
                output=reply.text if reply.confirmation is None else "awaiting confirmation"
            )

    def close(self, outcome: str) -> None:
        if self._span is None:
            return
        failed = outcome != "ok"
        self._span.update(
            metadata={
                **self._record.metadata(),
                "outcome": outcome,
                "llm_calls": self.llm_calls,
                "tokens": self._meter.used,
                "cached_tokens": self._meter.cached,
                "token_budget": self._meter.limit,
            },
            level=("WARNING" if outcome in _POLICY_BLOCKS else "ERROR") if failed else None,
            status_message=outcome if failed else None,
        )


class TracingTelemetry(Telemetry):
    def __init__(
        self, client: Langfuse | None, *, salt: str, environment: str, turn_token_budget: int
    ) -> None:
        self._client = client
        self._salt = salt
        self._environment = environment
        self._turn_token_budget = turn_token_budget
        self._llm_metrics = LlmMetricsCallback()
        self._agent_spans = AgentSpanHandler() if client is not None else None

    def callbacks(self) -> list[BaseCallbackHandler]:
        if self._agent_spans is None:
            return [self._llm_metrics]
        return [
            self._llm_metrics,
            self._agent_spans,
            LlmParentSpanCallback(self._agent_spans.parent_span),
        ]

    @contextmanager
    def turn(
        self, kind: TurnKind, user_id: str, session_id: UUID, question: str
    ) -> Iterator[TurnTrace]:
        started = perf_counter()
        outcome = _UNHANDLED
        with (
            recording_turn() as record,
            metered_turn(self._turn_token_budget) as meter,
            self._span(kind, user_id, session_id, question) as span,
        ):
            turn = _Turn(span.trace_id if span is not None else uuid4().hex, record, meter, span)
            try:
                yield turn
                outcome = "ok"
            except AgentError as error:
                error.trace_id = turn.trace_id
                outcome = error.code
                raise
            finally:
                turn.close(outcome)
                metrics.TURNS.labels(kind.value, outcome).inc()
                metrics.TURN_SECONDS.labels(kind.value).observe(perf_counter() - started)
                metrics.TURN_LLM_CALLS.labels(kind.value).observe(turn.llm_calls)
                metrics.TURN_TOKENS.labels(kind.value).observe(meter.used)
                metrics.TURN_CACHED_TOKENS.labels(kind.value).observe(meter.cached)

    @contextmanager
    def _span(
        self, kind: TurnKind, user_id: str, session_id: UUID, question: str
    ) -> Iterator[LangfuseAgent | None]:
        if self._client is None:
            yield None
            return
        name = f"turn.{kind}"
        with (
            self._client.start_as_current_observation(
                name=name, as_type="agent", input=question
            ) as span,
            propagate_attributes(
                user_id=user_hash(user_id, self._salt),
                session_id=str(session_id),
                tags=[kind.value, self._environment],
                trace_name=name,
            ),
        ):
            yield span
