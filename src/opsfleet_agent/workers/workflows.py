from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from opsfleet_agent.core.errors import LlmBudgetExceededError
    from opsfleet_agent.workers.activities import GoldenIngestion, PlanSweep

_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    non_retryable_error_types=[LlmBudgetExceededError.__name__],
)

GOLDEN_INGESTION = "golden-ingestion"
PLAN_SWEEP = "deletion-plan-sweep"


@workflow.defn(name=GOLDEN_INGESTION)
class GoldenIngestionWorkflow:
    @workflow.run
    async def run(self, dataset: str) -> int:
        loaded: int = await workflow.execute_activity_method(
            GoldenIngestion.load,
            dataset,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=_RETRY,
        )
        return loaded


@workflow.defn(name=PLAN_SWEEP)
class PlanSweepWorkflow:
    @workflow.run
    async def run(self) -> int:
        expired: int = await workflow.execute_activity_method(
            PlanSweep.expire,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=_RETRY,
        )
        return expired
