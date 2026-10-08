from datetime import timedelta
from typing import Any

from langfuse.api.commons.types.observation_v2 import ObservationV2

from opsfleet_agent.gateways.observability.traces import build_trace_detail
from tests.fakes import NOW


def observation(
    obs_id: str, kind: str, name: str, parent: str | None, **extra: Any
) -> ObservationV2:
    offset = timedelta(seconds=len(obs_id))
    return ObservationV2(
        id=obs_id,
        type=kind,
        name=name,
        project_id="p",
        parent_observation_id=parent,
        start_time=NOW + offset,
        end_time=NOW + offset + timedelta(milliseconds=1500),
        **extra,
    )


def test_trace_detail_keeps_agent_steps_and_turn_metadata() -> None:
    observations = [
        observation(
            "r",
            "AGENT",
            "turn.question",
            None,
            input="Q?",
            output="A.",
            user_id="hash",
            metadata={
                "prompts": {"agents/persona": "langfuse:v2"},
                "error_codes": ["E_SQL_SYNTAX"],
                "fallbacks": ["gemini-3.8-flash -> gemini-3.6-flash"],
            },
        ),
        observation("oo", "CHAIN", "orchestrator", "r"),
        observation("ooo", "CHAIN", "model", "oo"),
        observation("oooo", "GENERATION", "ChatOpenAI", "ooo", model="gemini-3.6-flash"),
        observation("ooooo", "TOOL", "run_sql", "oo", input='{"sql": "SELEC 1"}'),
    ]

    detail = build_trace_detail("t1", observations)

    assert [(s.name, s.depth) for s in detail.steps] == [
        ("turn.question", 0),
        ("orchestrator", 1),
        ("ChatOpenAI", 2),
        ("run_sql", 2),
    ]
    assert detail.prompts == {"agents/persona": "langfuse:v2"}
    assert detail.error_codes == ("E_SQL_SYNTAX",)
    assert detail.fallbacks == ("gemini-3.8-flash -> gemini-3.6-flash",)
    assert (detail.question, detail.answer, detail.user_hash) == ("Q?", "A.", "hash")
    assert detail.steps[2].duration_ms == 1500
