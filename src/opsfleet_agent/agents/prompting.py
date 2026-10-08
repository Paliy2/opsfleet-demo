from datetime import UTC, datetime
from functools import cache
from importlib.resources import files

from opsfleet_agent.agents.scope_context import describe_scope
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.context import AnalysisStep, ConversationContext, UserPreferences


@cache
def safety_rules() -> str:
    return files(__package__).joinpath("safety_rules.md").read_text(encoding="utf-8")


def render(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


def compose_system(*editable: str, scope: AccessScope) -> str:
    parts = [part.strip() for part in editable if part.strip()]
    today = (
        f"Today is {datetime.now(UTC):%A, %Y-%m-%d}. Resolve relative periods (this year, "
        "last quarter, last 90 days) against today, never against periods discussed earlier."
    )
    return "\n\n".join([*parts, safety_rules().strip(), describe_scope(scope), today])


def describe_preferences(preferences: UserPreferences) -> str:
    if preferences.is_empty:
        return ""
    lines = [
        f"- {name}: {value}"
        for name, value in preferences.model_dump(exclude={"notes"}).items()
        if value is not None
    ]
    lines += [f"- note: {note}" for note in preferences.notes]
    return (
        "## User preferences\nApply these to every answer unless the current request "
        "asks otherwise.\n" + "\n".join(lines)
    )


def describe_context(context: ConversationContext) -> str:
    sections = []
    if context.summary:
        sections.append(f"## Earlier in this conversation (summary)\n{context.summary}")
    if context.state.steps:
        sections.append(
            "## Analysis state\nAnalyst tasks already run in this conversation. Reuse their "
            "result ids for charts and follow-ups instead of re-running the analysis.\n"
            + "\n".join(_describe_step(step) for step in context.state.steps)
        )
    return "\n\n".join(sections)


def _describe_step(step: AnalysisStep) -> str:
    results = "".join(
        f"\n  - {result.id}: {result.purpose} (columns: {', '.join(result.columns)})"
        for result in step.results
    )
    return f"- task: {step.task}{results}"
