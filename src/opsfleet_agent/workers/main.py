import asyncio
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleSpec,
)
from temporalio.worker import Worker

from opsfleet_agent.core.config import Settings, get_settings
from opsfleet_agent.gateways.observability.metrics import serve_metrics
from opsfleet_agent.wiring import build_app
from opsfleet_agent.workers.activities import GoldenIngestion, PlanSweep
from opsfleet_agent.workers.workflows import (
    PLAN_SWEEP,
    GoldenIngestionWorkflow,
    PlanSweepWorkflow,
)

_SWEEP_EVERY = timedelta(minutes=5)


async def connect(settings: Settings) -> Client:
    if settings.temporal_address is None:
        raise RuntimeError("OPSFLEET_TEMPORAL_ADDRESS is not configured")
    return await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)


async def _ensure_sweep_schedule(client: Client, task_queue: str) -> None:
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            PlanSweepWorkflow.run, id=PLAN_SWEEP, task_queue=task_queue
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=_SWEEP_EVERY)]),
    )
    try:
        await client.create_schedule(PLAN_SWEEP, schedule)
    except ScheduleAlreadyRunningError:
        return


async def _serve(settings: Settings) -> None:
    client = await connect(settings)
    async with build_app(settings) as application:
        await _ensure_sweep_schedule(client, settings.temporal_task_queue)
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[GoldenIngestionWorkflow, PlanSweepWorkflow],
            activities=[
                GoldenIngestion(application.load_golden).load,
                PlanSweep(application.expire_plans).expire,
            ],
        )
        await worker.run()


def run() -> None:
    settings = get_settings()
    if settings.metrics_port is not None:
        serve_metrics(settings.metrics_port)
    asyncio.run(_serve(settings))
