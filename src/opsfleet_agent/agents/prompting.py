from functools import cache
from importlib.resources import files

from opsfleet_agent.agents.scope_context import describe_scope
from opsfleet_agent.entities.access import AccessScope


@cache
def safety_rules() -> str:
    return files(__package__).joinpath("safety_rules.md").read_text(encoding="utf-8")


def render(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


def compose_system(*editable: str, scope: AccessScope) -> str:
    parts = [part.strip() for part in editable if part.strip()]
    return "\n\n".join([*parts, safety_rules().strip(), describe_scope(scope)])
