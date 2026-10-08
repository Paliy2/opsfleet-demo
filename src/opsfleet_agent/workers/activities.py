from pathlib import Path

from temporalio import activity

from opsfleet_agent.gateways.golden_dataset import read_trios
from opsfleet_agent.interactors.golden import LoadGoldenTrios
from opsfleet_agent.interactors.reports import ExpireDeletionPlans


class GoldenIngestion:
    def __init__(self, load: LoadGoldenTrios) -> None:
        self._load = load

    @activity.defn(name="load-golden-trios")
    async def load(self, dataset: str) -> int:
        return await self._load(read_trios(Path(dataset)))


class PlanSweep:
    def __init__(self, expire: ExpireDeletionPlans) -> None:
        self._expire = expire

    @activity.defn(name="expire-deletion-plans")
    async def expire(self) -> int:
        return await self._expire()
