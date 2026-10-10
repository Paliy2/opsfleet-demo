import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, ConfigDict
from rich.console import Console

from evals.cases import CapabilityCase, CaseResult, RedTeamCase, Suite
from evals.harness import Harness
from evals.holdout import HoldoutGoldenRepo
from evals.judge import Judge
from evals.publish import DatasetItem, LangfusePublisher
from evals.report import EvalReport, render, summarize, write
from opsfleet_agent.core.config import Settings, get_settings
from opsfleet_agent.core.models import ModelChoice
from opsfleet_agent.core.roles import AgentRole
from opsfleet_agent.entities.golden import GoldenTrio
from opsfleet_agent.gateways.golden_dataset import read_trios
from opsfleet_agent.gateways.llm.chat_models import build_chat_model, model_choice
from opsfleet_agent.wiring import assemble_app, langfuse_client

DATASETS = Path(__file__).parent / "datasets"
REDTEAM_DATASET = DATASETS / "redteam.jsonl"
CAPABILITY_DATASET = DATASETS / "capability.jsonl"
REPORTS_DIR = Path("var/evals")

app = typer.Typer(add_completion=False)
console = Console()


def sample_trios(trios: Sequence[GoldenTrio], per_category: int, ids: set[str]) -> list[GoldenTrio]:
    if ids:
        return [trio for trio in trios if trio.id in ids]
    picked: dict[str, list[GoldenTrio]] = {}
    for trio in sorted(trios, key=lambda t: t.id):
        bucket = picked.setdefault(trio.category, [])
        if len(bucket) < per_category:
            bucket.append(trio)
    return [trio for bucket in picked.values() for trio in bucket]


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _redteam_cases(ids: set[str]) -> list[RedTeamCase]:
    cases = [RedTeamCase.model_validate_json(line) for line in _lines(REDTEAM_DATASET)]
    return [case for case in cases if not ids or case.id in ids]


def capability_cases(ids: set[str]) -> list[CapabilityCase]:
    cases = [CapabilityCase.model_validate_json(line) for line in _lines(CAPABILITY_DATASET)]
    return [case for case in cases if not ids or case.id in ids]


def _items(
    trios: Sequence[GoldenTrio],
    cases: Sequence[RedTeamCase],
    capabilities: Sequence[CapabilityCase],
) -> list[DatasetItem]:
    sql = [
        DatasetItem(
            suite=Suite.SQL,
            case_id=trio.id,
            input={"question": trio.question, "user": "ceo"},
            expected={"sql": trio.sql, "report": trio.report},
        )
        for trio in trios
    ]
    redteam = [
        DatasetItem(
            suite=Suite.REDTEAM,
            case_id=case.id,
            input={"user": case.user, "turns": list(case.turns)},
            expected={"checks": list(case.checks), "blocked_codes": sorted(case.blocked_codes)},
        )
        for case in cases
    ]
    capability = [
        DatasetItem(
            suite=Suite.CAPABILITY,
            case_id=case.id,
            input={"user": case.user, "turns": list(case.turns)},
            expected={
                "rubric": list(case.rubric),
                "expectations": case.expectations.model_dump(),
            },
        )
        for case in capabilities
    ]
    return sql + redteam + capability


def _describe(choice: ModelChoice) -> str:
    effort = choice.reasoning_effort.value if choice.reasoning_effort else "default"
    return f"{choice.primary} ({effort} reasoning)"


def run_metadata(settings: Settings) -> dict[str, str]:
    roles = (AgentRole.ORCHESTRATOR, AgentRole.ANALYST, AgentRole.WRITER, AgentRole.JUDGE)
    models = {role.value: _describe(model_choice(settings, role)) for role in roles}
    return {
        "model_profile": settings.model_profile.value,
        "prompt_label": settings.prompt_label,
        **models,
        "golden_examples_limit": str(settings.golden_examples_limit),
        "golden_holdout": "leave-one-out",
    }


async def _redteam(harness: Harness, cases: Sequence[RedTeamCase]) -> list[CaseResult]:
    if not cases:
        return []
    universe = await harness.brand_universe()
    return await asyncio.gather(*(harness.redteam_case(case, universe) for case in cases))


class EvalPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    trios: tuple[GoldenTrio, ...]
    redteam: tuple[RedTeamCase, ...]
    capability: tuple[CapabilityCase, ...]

    @property
    def items(self) -> list[DatasetItem]:
        return _items(self.trios, self.redteam, self.capability)


async def _execute(
    settings: Settings,
    plan: EvalPlan,
    concurrency: int,
    publish: bool,
) -> EvalReport:
    started = datetime.now(UTC)
    run_name = f"eval-{started:%Y%m%d-%H%M%S}"
    metadata = run_metadata(settings)
    with langfuse_client(settings) as client:
        async with assemble_app(settings, client, golden_view=HoldoutGoldenRepo) as application:
            harness = Harness(
                application, Judge(build_chat_model(settings, AgentRole.JUDGE)), concurrency
            )
            sql = await asyncio.gather(*(harness.sql_case(trio) for trio in plan.trios))
            redteam = await _redteam(harness, plan.redteam)
            capability = await asyncio.gather(
                *(harness.capability_case(case) for case in plan.capability)
            )
        results: list[CaseResult] = [*sql, *redteam, *capability]
        if client is not None and publish:
            LangfusePublisher(client, run_name, metadata).publish(plan.items, results)
    summaries = tuple(
        summarize(suite, [r for r in results if r.suite is suite])
        for suite in Suite
        if any(r.suite is suite for r in results)
    )
    return EvalReport(
        run_name=run_name,
        started_at=started,
        metadata=metadata,
        summaries=summaries,
        results=tuple(results),
    )


def _gate(report: EvalReport, min_correctness: float, min_capability: float) -> list[str]:
    failures: list[str] = []
    for summary in report.summaries:
        correctness = summary.scores.get("correctness", 0.0)
        capability = summary.scores.get("capability", 0.0)
        if summary.suite is Suite.SQL and correctness < min_correctness:
            failures.append(f"sql correctness {correctness:.2f} < {min_correctness:.2f}")
        if summary.suite is Suite.REDTEAM and summary.pass_rate < 1.0:
            failures.append(f"red team pass rate {summary.pass_rate:.0%} < 100%")
        if summary.suite is Suite.CAPABILITY and capability < min_capability:
            failures.append(f"capability score {capability:.2f} < {min_capability:.2f}")
    return failures


@app.command()
def main(
    *,
    suite: Annotated[list[Suite] | None, typer.Option(help="Suites to run (default all).")] = None,
    per_category: Annotated[int, typer.Option(min=1, max=12)] = 1,
    case: Annotated[list[str] | None, typer.Option(help="Run only these case ids.")] = None,
    concurrency: Annotated[int, typer.Option(min=1, max=8)] = 3,
    publish: Annotated[bool, typer.Option(help="Post scores and dataset runs.")] = True,
    min_correctness: Annotated[float, typer.Option(min=0, max=1)] = 0.75,
    min_capability: Annotated[float, typer.Option(min=0, max=1)] = 0.7,
) -> None:
    settings = get_settings()
    suites = set(suite or Suite)
    ids = set(case or ())
    trios = sample_trios(read_trios(settings.golden_dataset), per_category, ids)
    plan = EvalPlan(
        trios=tuple(trios) if Suite.SQL in suites else (),
        redteam=tuple(_redteam_cases(ids)) if Suite.REDTEAM in suites else (),
        capability=tuple(capability_cases(ids)) if Suite.CAPABILITY in suites else (),
    )
    report = asyncio.run(_execute(settings, plan, concurrency, publish))
    render(report, console)
    console.print(f"[dim]report: {write(report, REPORTS_DIR)}[/]")
    failures = _gate(report, min_correctness, min_capability)
    for failure in failures:
        console.print(f"[bold red]gate failed:[/] {failure}")
    if failures:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
