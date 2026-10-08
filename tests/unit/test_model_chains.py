import pytest

from opsfleet_agent.core.config import ModelProfile
from opsfleet_agent.core.models import ROSTERS, ModelChoice
from opsfleet_agent.core.roles import AgentRole

AI_STUDIO = "gemini/"
VERTEX = "vertex/"

CHOICES = [
    pytest.param(choice, id=f"{profile}-{role}")
    for profile, roster in ROSTERS.items()
    for role, choice in roster.items()
]


@pytest.mark.parametrize("choice", CHOICES)
def test_chain_ends_with_exactly_one_vertex_model(choice: ModelChoice) -> None:
    *ai_studio, last_resort = choice.fallbacks

    assert choice.primary.startswith(AI_STUDIO)
    assert all(model.startswith(AI_STUDIO) for model in ai_studio)
    assert last_resort.startswith(VERTEX)


@pytest.mark.parametrize("choice", CHOICES)
def test_vertex_fallback_serves_a_model_already_in_the_chain(choice: ModelChoice) -> None:
    ai_studio_models = {
        model.removeprefix(AI_STUDIO) for model in (choice.primary, *choice.fallbacks[:-1])
    }

    assert choice.fallbacks[-1].removeprefix(VERTEX) in ai_studio_models


@pytest.mark.parametrize("profile", list(ModelProfile))
def test_every_role_has_a_chain(profile: ModelProfile) -> None:
    assert set(ROSTERS[profile]) == set(AgentRole)
