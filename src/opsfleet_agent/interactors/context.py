from collections.abc import Sequence

from opsfleet_agent.entities.context import AnalysisState, AnalysisStep, UserPreferences
from opsfleet_agent.ports.context import PreferenceRepo


def advance_state(
    state: AnalysisState, steps: Sequence[AnalysisStep], max_steps: int
) -> AnalysisState:
    if not steps:
        return state
    return AnalysisState(steps=(*state.steps, *steps)[-max_steps:])


class UpdatePreferences:
    def __init__(self, repo: PreferenceRepo) -> None:
        self._repo = repo

    async def __call__(
        self,
        user_id: str,
        changes: UserPreferences,
        add_note: str | None = None,
        remove_note: str | None = None,
    ) -> UserPreferences:
        current = await self._repo.get(user_id)
        notes = [note for note in current.notes if note != remove_note]
        if add_note and add_note not in notes:
            notes.append(add_note)
        merged = current.model_copy(
            update={
                **changes.model_dump(exclude_unset=True, exclude={"notes"}),
                "notes": tuple(notes[-5:]),
            }
        )
        updated = UserPreferences.model_validate(merged.model_dump())
        await self._repo.save(user_id, updated)
        return updated

    async def reset(self, user_id: str) -> UserPreferences:
        await self._repo.save(user_id, UserPreferences())
        return UserPreferences()
