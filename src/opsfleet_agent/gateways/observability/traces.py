from collections.abc import Sequence
from functools import partial
from typing import Any

import anyio
from langfuse import Langfuse
from langfuse.api.commons.types.observation_v2 import ObservationV2

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.telemetry import TraceDetail, TraceStep
from opsfleet_agent.ports.telemetry import TraceReader

_FIELDS = "core,basic,io,metadata,model,usage,trace_context"
_EXPANDED_METADATA = "prompts,error_codes,fallbacks"
_PAGE_SIZE = 1000
_VISIBLE_TYPES = frozenset({"AGENT", "GENERATION", "TOOL"})
_NAMED_LOOPS = frozenset({"orchestrator", "analyst", "guard"})
_FLAGGED_LEVELS = frozenset({"WARNING", "ERROR"})


def _is_visible(item: ObservationV2) -> bool:
    return (
        item.type in _VISIBLE_TYPES
        or item.name in _NAMED_LOOPS
        or str(item.level) in _FLAGGED_LEVELS
    )


def _depth(item: ObservationV2, by_id: dict[str, ObservationV2]) -> int:
    depth, parent = 0, by_id.get(item.parent_observation_id or "")
    while parent is not None:
        depth += _is_visible(parent)
        parent = by_id.get(parent.parent_observation_id or "")
    return depth


def _step(item: ObservationV2, depth: int) -> TraceStep:
    usage = item.usage_details or {}
    duration = item.end_time - item.start_time if item.end_time else None
    return TraceStep(
        kind=str(item.type),
        name=item.name or "",
        started_at=item.start_time,
        duration_ms=int(duration.total_seconds() * 1000) if duration else None,
        depth=depth,
        model=item.model,
        input=item.input,
        output=item.output,
        level=str(item.level) if item.level else None,
        status=item.status_message,
        tokens=usage.get("total") or None,
    )


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def build_trace_detail(trace_id: str, observations: Sequence[ObservationV2]) -> TraceDetail:
    ordered = sorted(observations, key=lambda item: item.start_time)
    by_id = {item.id: item for item in ordered}
    root = next((item for item in ordered if item.parent_observation_id is None), ordered[0])
    metadata: dict[str, Any] = root.metadata if isinstance(root.metadata, dict) else {}
    prompts = metadata.get("prompts")
    return TraceDetail(
        trace_id=trace_id,
        name=root.trace_name or root.name,
        started_at=root.start_time,
        user_hash=root.user_id,
        session_id=root.session_id,
        tags=tuple(root.tags or ()),
        question=root.input,
        answer=root.output,
        prompts={str(k): str(v) for k, v in prompts.items()} if isinstance(prompts, dict) else {},
        error_codes=_strings(metadata.get("error_codes")),
        fallbacks=_strings(metadata.get("fallbacks")),
        steps=tuple(_step(item, _depth(item, by_id)) for item in ordered if _is_visible(item)),
    )


class LangfuseTraceReader(TraceReader):
    def __init__(self, client: Langfuse) -> None:
        self._client = client

    async def get(self, trace_id: str) -> TraceDetail:
        observations: list[ObservationV2] = []
        cursor: str | None = None
        while True:
            page = await anyio.to_thread.run_sync(
                partial(
                    self._client.api.observations.get_many,
                    trace_id=trace_id,
                    fields=_FIELDS,
                    expand_metadata=_EXPANDED_METADATA,
                    limit=_PAGE_SIZE,
                    cursor=cursor,
                )
            )
            observations.extend(page.data)
            cursor = page.meta.cursor
            if cursor is None:
                break
        if not observations:
            raise NotFoundError(f"trace {trace_id} not found or not ingested yet")
        return build_trace_detail(trace_id, observations)
