import asyncio
import random
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, ConfigDict
from rich.console import Console
from rich.table import Table

from evals.cases import CapabilityCase, CaseResult, Suite
from evals.harness import Harness
from evals.holdout import HoldoutGoldenRepo
from evals.judge import Judge
from evals.paired import PairedDelta, compare
from evals.run import REPORTS_DIR, capability_cases, run_metadata, sample_trios
from opsfleet_agent.core.config import Settings, get_settings
from opsfleet_agent.core.roles import AgentRole
from opsfleet_agent.entities.golden import GoldenTrio
from opsfleet_agent.gateways.golden_dataset import read_trios
from opsfleet_agent.gateways.llm.chat_models import build_chat_model
from opsfleet_agent.wiring import assemble_app, langfuse_client

app = typer.Typer(add_completion=False)
console = Console()

_SHUFFLE_SEED = 11
_PRIMARY = {Suite.SQL: "correctness", Suite.CAPABILITY: "capability"}
_LOWER_IS_BETTER = frozenset({"seconds", "llm_calls", "queries", "retries"})
_METRIC_ORDER = (
    "passed",
    "correctness",
    "completeness",
    "groundedness",
    "column_recall",
    "literal_grounding",
    "capability",
    "rubric",
    "intent",
    "clarity",
    "examples",
    "seconds",
    "llm_calls",
    "queries",
    "retries",
)


class Arm(StrEnum):
    NONE = "none"
    GOLDEN = "golden"
    UNSEEN_THEME = "unseen_theme"


_ARM_MEANING = {
    Arm.NONE: "no golden examples (retrieval off)",
    Arm.GOLDEN: "production retrieval; the trio under test is hidden",
    Arm.UNSEEN_THEME: "retrieval with every trio of the case's theme hidden",
}


class ArmRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    arm: Arm
    repeat: int
    result: CaseResult


class ArmComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    suite: Suite
    baseline: Arm
    treatment: Arm
    deltas: tuple[PairedDelta, ...]

    @property
    def primary(self) -> PairedDelta:
        return next(d for d in self.deltas if d.metric == _PRIMARY[self.suite])


class AblationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_name: str
    started_at: datetime
    metadata: dict[str, str]
    comparisons: tuple[ArmComparison, ...]
    runs: tuple[ArmRun, ...]


@dataclass(frozen=True)
class _Job:
    arm: Arm
    repeat: int
    play: Callable[[], Awaitable[CaseResult]]


class AblationPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    trios: tuple[GoldenTrio, ...]
    capability: tuple[CapabilityCase, ...]
    arms: tuple[Arm, ...]
    repeats: int

    @property
    def turns(self) -> int:
        capability_arms = len(set(self.arms) - {Arm.UNSEEN_THEME})
        per_repeat = len(self.trios) * len(self.arms) + len(self.capability) * capability_arms
        return per_repeat * self.repeats


def _themes(trios: Sequence[GoldenTrio]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for trio in trios:
        grouped[trio.category].append(trio.id)
    return {category: tuple(ids) for category, ids in grouped.items()}


def _sql_jobs(
    plan: AblationPlan, harnesses: dict[Arm, Harness], themes: dict[str, tuple[str, ...]]
) -> list[_Job]:
    jobs: list[_Job] = []
    for trio in plan.trios:
        hidden = {Arm.UNSEEN_THEME: themes[trio.category]}
        for arm in plan.arms:
            play = _bind(harnesses[arm].sql_case, trio, hidden.get(arm, ()))
            jobs += [_Job(arm, repeat, play) for repeat in range(plan.repeats)]
    return jobs


def _capability_jobs(plan: AblationPlan, harnesses: dict[Arm, Harness]) -> list[_Job]:
    arms = [arm for arm in plan.arms if arm is not Arm.UNSEEN_THEME]
    return [
        _Job(arm, repeat, _bind_capability(harnesses[arm], case))
        for case in plan.capability
        for arm in arms
        for repeat in range(plan.repeats)
    ]


def _bind(
    play: Callable[[GoldenTrio, Sequence[str]], Awaitable[CaseResult]],
    trio: GoldenTrio,
    hidden: Sequence[str],
) -> Callable[[], Awaitable[CaseResult]]:
    return lambda: play(trio, hidden)


def _bind_capability(harness: Harness, case: CapabilityCase) -> Callable[[], Awaitable[CaseResult]]:
    return lambda: harness.capability_case(case)


async def _play_all(jobs: list[_Job], concurrency: int) -> list[ArmRun]:
    random.Random(_SHUFFLE_SEED).shuffle(jobs)
    slots = asyncio.Semaphore(concurrency)

    async def play(job: _Job) -> ArmRun:
        async with slots:
            return ArmRun(arm=job.arm, repeat=job.repeat, result=await job.play())

    return list(await asyncio.gather(*(play(job) for job in jobs)))


def _measures(result: CaseResult) -> dict[str, float]:
    stats = result.stats.model_dump() if result.stats else {}
    return {**result.scores, **stats, "passed": 1.0 if result.passed else 0.0}


def _per_case(runs: Sequence[ArmRun], suite: Suite, arm: Arm) -> dict[str, dict[str, float]]:
    collected: dict[str, list[dict[str, float]]] = defaultdict(list)
    for run in runs:
        if run.arm is arm and run.result.suite is suite and run.result.stats is not None:
            collected[run.result.case_id].append(_measures(run.result))
    return {
        case_id: {
            name: sum(m[name] for m in measures) / len(measures)
            for name in set.intersection(*(set(m) for m in measures))
        }
        for case_id, measures in collected.items()
    }


def _compare(runs: Sequence[ArmRun], suite: Suite, treatment: Arm) -> ArmComparison | None:
    baseline = _per_case(runs, suite, Arm.NONE)
    treated = _per_case(runs, suite, treatment)
    paired = sorted(baseline.keys() & treated.keys())
    if not paired:
        return None
    shared = set.intersection(*(set(baseline[c]) & set(treated[c]) for c in paired))
    deltas = tuple(
        compare(name, [baseline[c][name] for c in paired], [treated[c][name] for c in paired])
        for name in _METRIC_ORDER
        if name in shared
    )
    return ArmComparison(suite=suite, baseline=Arm.NONE, treatment=treatment, deltas=deltas)


def comparisons(runs: Sequence[ArmRun], arms: Sequence[Arm]) -> tuple[ArmComparison, ...]:
    found = (
        _compare(runs, suite, arm) for suite in _PRIMARY for arm in arms if arm is not Arm.NONE
    )
    return tuple(comparison for comparison in found if comparison is not None)


def verdict(comparison: ArmComparison) -> str:
    primary = comparison.primary
    span = f"{primary.delta:+.3f} (95% CI {primary.low:+.3f}..{primary.high:+.3f}, "
    span += f"sign test p={primary.p_value:g}, n={primary.pairs})"
    if not primary.significant:
        return f"no detectable effect on {primary.metric}: {span}"
    direction = "improve" if primary.delta > 0 else "hurt"
    return f"{comparison.treatment} examples {direction} {primary.metric}: {span}"


async def _execute(settings: Settings, plan: AblationPlan, concurrency: int) -> list[ArmRun]:
    bare = settings.model_copy(update={"golden_examples_limit": 0})
    with langfuse_client(settings) as client:
        async with (
            assemble_app(settings, client, golden_view=HoldoutGoldenRepo) as golden_app,
            assemble_app(bare, client) as bare_app,
        ):
            judge = Judge(build_chat_model(settings, AgentRole.JUDGE))
            with_examples = Harness(golden_app, judge, concurrency)
            harnesses = {
                Arm.NONE: Harness(bare_app, judge, concurrency),
                Arm.GOLDEN: with_examples,
                Arm.UNSEEN_THEME: with_examples,
            }
            themes = _themes(read_trios(settings.golden_dataset))
            jobs = _sql_jobs(plan, harnesses, themes) + _capability_jobs(plan, harnesses)
            return await _play_all(jobs, concurrency)


def _rows(comparison: ArmComparison) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for delta in comparison.deltas:
        better = -delta.delta if delta.metric in _LOWER_IS_BETTER else delta.delta
        mark = "*" if delta.significant else ""
        rows.append(
            (
                delta.metric,
                f"{delta.baseline:g}",
                f"{delta.treatment:g}",
                f"{delta.delta:+g}{mark}",
                f"{delta.low:+g}..{delta.high:+g}",
                f"{delta.wins}/{delta.ties}/{delta.losses}",
                f"{delta.p_value:g}",
                "better" if better > 0 else "worse" if better < 0 else "same",
            )
        )
    return rows


_HEADERS = ("metric", "none", "examples", "Δ", "95% CI", "W/T/L", "p", "")


def render(report: AblationReport) -> None:
    for comparison in report.comparisons:
        title = f"{comparison.suite}: {comparison.treatment} vs {comparison.baseline}"
        table = Table(*_HEADERS, title=title)
        for row in _rows(comparison):
            table.add_row(*row)
        console.print(table)
        console.print(f"[bold]{verdict(comparison)}[/]")


def markdown(report: AblationReport) -> str:
    lines = [f"# Golden-example ablation {report.run_name}", ""]
    lines += [f"- {key}: {value}" for key, value in report.metadata.items()]
    lines += [f"- arm `{arm}`: {meaning}" for arm, meaning in _ARM_MEANING.items()]
    for comparison in report.comparisons:
        lines += [
            "",
            f"## {comparison.suite}: {comparison.treatment} vs {comparison.baseline}",
            "",
            f"**{verdict(comparison)}**",
            "",
            "| " + " | ".join(_HEADERS[:-1]) + " | |",
            "|" + "---|" * len(_HEADERS),
        ]
        lines += ["| " + " | ".join(row) + " |" for row in _rows(comparison)]
    return "\n".join(lines) + "\n"


def write(report: AblationReport, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / report.run_name
    stem.with_suffix(".json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    return stem.with_suffix(".md")


@app.command()
def main(
    *,
    suite: Annotated[list[Suite] | None, typer.Option(help="sql and/or capability.")] = None,
    arm: Annotated[list[Arm] | None, typer.Option(help="Arms to run (default all).")] = None,
    per_category: Annotated[int, typer.Option(min=1, max=6)] = 2,
    case: Annotated[list[str] | None, typer.Option(help="Run only these case ids.")] = None,
    repeats: Annotated[int, typer.Option(min=1, max=5)] = 1,
    concurrency: Annotated[int, typer.Option(min=1, max=8)] = 3,
) -> None:
    settings = get_settings()
    suites = set(suite or (Suite.SQL,))
    if Suite.REDTEAM in suites:
        raise typer.BadParameter(
            "the red team is a safety gate, not a quality metric", param_hint="--suite"
        )
    ids = set(case or ())
    arms = tuple(dict.fromkeys([Arm.NONE, *(arm or Arm)]))
    trios = sample_trios(read_trios(settings.golden_dataset), per_category, ids)
    plan = AblationPlan(
        trios=tuple(trios) if Suite.SQL in suites else (),
        capability=tuple(capability_cases(ids)) if Suite.CAPABILITY in suites else (),
        arms=arms,
        repeats=repeats,
    )
    console.print(f"[dim]{plan.turns} agent turns, each followed by one judge call[/]")
    started = datetime.now(UTC)
    runs = asyncio.run(_execute(settings, plan, concurrency))
    report = AblationReport(
        run_name=f"ablation-{started:%Y%m%d-%H%M%S}",
        started_at=started,
        metadata={**run_metadata(settings), "repeats": str(repeats)},
        comparisons=comparisons(runs, arms),
        runs=tuple(runs),
    )
    render(report)
    console.print(f"[dim]report: {write(report, REPORTS_DIR)}[/]")


if __name__ == "__main__":
    app()
