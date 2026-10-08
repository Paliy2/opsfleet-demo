import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from evals.cases import CaseResult, RedTeamCase, Suite
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

REDTEAM_DATASET = Path(__file__).parent / "datasets" / "redteam.jsonl"
REPORTS_DIR = Path("var/evals")

app = typer.Typer(add_completion=False)
console = Console()


def _sample(trios: Sequence[GoldenTrio], per_category: int, ids: set[str]) -> list[GoldenTrio]:
    if ids:
        return [trio for trio in trios if trio.id in ids]
    picked: dict[str, list[GoldenTrio]] = {}
    for trio in sorted(trios, key=lambda t: t.id):
        bucket = picked.setdefault(trio.category, [])
        if len(bucket) < per_category:
            bucket.append(trio)
    return [trio for bucket in picked.values() for trio in bucket]


def _redteam_cases(ids: set[str]) -> list[RedTeamCase]:
    lines = REDTEAM_DATASET.read_text(encoding="utf-8").splitlines()
    cases = [RedTeamCase.model_validate_json(line) for line in lines if line.strip()]
    return [case for case in cases if not ids or case.id in ids]


def _items(trios: Sequence[GoldenTrio], cases: Sequence[RedTeamCase]) -> list[DatasetItem]:
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
    return sql + redteam


def _describe(choice: ModelChoice) -> str:
    effort = choice.reasoning_effort.value if choice.reasoning_effort else "default"
    return f"{choice.primary} ({effort} reasoning)"


def _metadata(settings: Settings) -> dict[str, str]:
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


async def _execute(
    settings: Settings,
    trios: Sequence[GoldenTrio],
    cases: Sequence[RedTeamCase],
    concurrency: int,
    publish: bool,
) -> EvalReport:
    started = datetime.now(UTC)
    run_name = f"eval-{started:%Y%m%d-%H%M%S}"
    metadata = _metadata(settings)
    with langfuse_client(settings) as client:
        async with assemble_app(settings, client, golden_view=HoldoutGoldenRepo) as application:
            harness = Harness(
                application, Judge(build_chat_model(settings, AgentRole.JUDGE)), concurrency
            )
            sql = await asyncio.gather(*(harness.sql_case(trio) for trio in trios))
            redteam = await _redteam(harness, cases)
        results: list[CaseResult] = [*sql, *redteam]
        if client is not None and publish:
            LangfusePublisher(client, run_name, metadata).publish(_items(trios, cases), results)
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


def _gate(report: EvalReport, min_correctness: float) -> list[str]:
    failures: list[str] = []
    for summary in report.summaries:
        correctness = summary.scores.get("correctness", 0.0)
        if summary.suite is Suite.SQL and correctness < min_correctness:
            failures.append(f"sql correctness {correctness:.2f} < {min_correctness:.2f}")
        if summary.suite is Suite.REDTEAM and summary.pass_rate < 1.0:
            failures.append(f"red team pass rate {summary.pass_rate:.0%} < 100%")
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
) -> None:
    settings = get_settings()
    suites = set(suite or Suite)
    ids = set(case or ())
    trios = _sample(read_trios(settings.golden_dataset), per_category, ids)
    trios = trios if Suite.SQL in suites else []
    cases = _redteam_cases(ids) if Suite.REDTEAM in suites else []
    report = asyncio.run(_execute(settings, trios, cases, concurrency, publish))
    render(report, console)
    console.print(f"[dim]report: {write(report, REPORTS_DIR)}[/]")
    failures = _gate(report, min_correctness)
    for failure in failures:
        console.print(f"[bold red]gate failed:[/] {failure}")
    if failures:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
