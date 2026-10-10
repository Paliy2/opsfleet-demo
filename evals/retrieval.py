import asyncio
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from statistics import median
from typing import Annotated

import typer
from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.table import Table

from evals.ranking import hit_rate, ndcg, precision, recall, reciprocal_rank
from evals.run import DATASETS, REPORTS_DIR
from opsfleet_agent.core.config import Settings, get_settings
from opsfleet_agent.entities.golden import GoldenMatch, GoldenTrio
from opsfleet_agent.gateways.golden_dataset import read_trios
from opsfleet_agent.gateways.llm.embeddings import GatewayEmbedder
from opsfleet_agent.ports.golden import Embedder, GoldenRepo
from opsfleet_agent.repos.database import create_database
from opsfleet_agent.repos.golden import SqlGoldenRepo

RETRIEVAL_DATASET = DATASETS / "retrieval.jsonl"
_POOL = 30
_RANK_CUTS = (1, 3)

app = typer.Typer(add_completion=False)
console = Console()


class Mode(StrEnum):
    LABELLED = "labelled"
    OFF_TOPIC = "off_topic"
    HOLDOUT = "holdout"
    IDENTITY = "identity"


_MODE_MEANING = {
    Mode.LABELLED: "hand-written paraphrases of trio questions; the labelled trios must come back",
    Mode.OFF_TOPIC: "questions the bucket cannot answer; nothing may pass the similarity threshold",
    Mode.HOLDOUT: "a trio's own question with the trio hidden; diagnostic, theme siblings expected",
    Mode.IDENTITY: "each trio's own question; the trio itself must be served",
}


class RetrievalCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    mode: Mode
    query: str
    relevant: frozenset[str]
    theme: str | None = None
    hidden: frozenset[str] = frozenset()


class LabelledLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    query: str
    relevant: tuple[str, ...]
    theme: str | None = None

    def case(self) -> RetrievalCase:
        mode = Mode.LABELLED if self.relevant else Mode.OFF_TOPIC
        return RetrievalCase(
            id=self.id,
            mode=mode,
            query=self.query,
            relevant=frozenset(self.relevant),
            theme=self.theme,
        )


class RetrievalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    mode: Mode
    theme: str | None
    query: str
    relevant: tuple[str, ...]
    returned: tuple[str, ...]
    passed: bool
    scores: dict[str, float]
    top_similarity: float
    relevant_rank: int | None
    relevant_similarity: float | None


class ModeSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: Mode
    cases: int
    pass_rate: float
    scores: dict[str, float]
    by_theme: dict[str, float]


class Calibration(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_similarity: float
    relevant_similarity_min: float | None
    relevant_similarity_median: float | None
    off_topic_similarity_max: float | None

    @property
    def margin(self) -> float | None:
        if self.relevant_similarity_min is None or self.off_topic_similarity_max is None:
            return None
        return round(self.relevant_similarity_min - self.off_topic_similarity_max, 3)


class RetrievalReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_name: str
    started_at: datetime
    metadata: dict[str, str]
    summaries: tuple[ModeSummary, ...]
    calibration: Calibration
    results: tuple[RetrievalResult, ...]


def read_cases(path: Path) -> tuple[RetrievalCase, ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return tuple(LabelledLine.model_validate_json(line).case() for line in lines if line.strip())


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def validate_cases(cases: Sequence[RetrievalCase], trios: Sequence[GoldenTrio]) -> None:
    known = {trio.id for trio in trios}
    themes = {trio.category for trio in trios}
    questions = {_normalized(trio.question) for trio in trios}
    problems: list[str] = []
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            problems.append(f"{case.id}: duplicate id")
        seen.add(case.id)
        problems += [f"{case.id}: unknown trio {t}" for t in sorted(case.relevant - known)]
        if case.theme is not None and case.theme not in themes:
            problems.append(f"{case.id}: unknown theme {case.theme}")
        if case.mode is Mode.OFF_TOPIC and case.theme is not None:
            problems.append(f"{case.id}: off-topic cases carry no theme")
        if case.mode is Mode.LABELLED and _normalized(case.query) in questions:
            problems.append(f"{case.id}: query repeats a trio question verbatim")
    if problems:
        raise ValueError("invalid retrieval dataset:\n" + "\n".join(problems))


def _siblings(trio: GoldenTrio, trios: Iterable[GoldenTrio]) -> frozenset[str]:
    return frozenset(t.id for t in trios if t.category == trio.category and t.id != trio.id)


def trio_cases(trios: Sequence[GoldenTrio]) -> list[RetrievalCase]:
    holdout = [
        RetrievalCase(
            id=f"holdout--{trio.id}",
            mode=Mode.HOLDOUT,
            query=trio.question,
            relevant=_siblings(trio, trios),
            theme=trio.category,
            hidden=frozenset({trio.id}),
        )
        for trio in trios
    ]
    identity = [
        RetrievalCase(
            id=f"identity--{trio.id}",
            mode=Mode.IDENTITY,
            query=trio.question,
            relevant=frozenset({trio.id}),
            theme=trio.category,
        )
        for trio in trios
    ]
    return holdout + identity


def _ranking_scores(
    returned: Sequence[str], relevant: frozenset[str], limit: int
) -> dict[str, float]:
    cuts = [*(cut for cut in _RANK_CUTS if cut < limit), limit]
    scores = {f"hit@{cut}": hit_rate(returned, relevant, cut) for cut in cuts}
    scores[f"recall@{limit}"] = recall(returned, relevant, limit)
    scores[f"precision@{limit}"] = precision(returned, relevant, limit)
    scores["mrr"] = reciprocal_rank(returned, relevant)
    scores[f"ndcg@{limit}"] = ndcg(returned, relevant, limit)
    return scores


def _theme_precision(served: Sequence[GoldenMatch], theme: str) -> float:
    if not served:
        return 0.0
    return sum(1 for match in served if match.trio.category == theme) / len(served)


def _passed(case: RetrievalCase, scores: dict[str, float], limit: int) -> bool:
    if case.mode is Mode.OFF_TOPIC:
        return scores["abstained"] == 1.0
    return scores[f"hit@{limit}"] == 1.0


def score_case(
    case: RetrievalCase, pool: Sequence[GoldenMatch], limit: int, min_similarity: float
) -> RetrievalResult:
    visible = [match for match in pool if match.trio.id not in case.hidden]
    served = [match for match in visible if match.similarity >= min_similarity][:limit]
    returned = tuple(match.trio.id for match in served)
    best = next(
        ((rank, m.similarity) for rank, m in enumerate(visible, 1) if m.trio.id in case.relevant),
        None,
    )
    if case.mode is Mode.OFF_TOPIC:
        scores = {"abstained": 0.0 if served else 1.0}
    else:
        scores = _ranking_scores(returned, case.relevant, limit)
        if case.theme is not None:
            scores[f"theme_precision@{limit}"] = _theme_precision(served, case.theme)
    scores["returned"] = float(len(served))
    return RetrievalResult(
        case_id=case.id,
        mode=case.mode,
        theme=case.theme,
        query=case.query,
        relevant=tuple(sorted(case.relevant)),
        returned=returned,
        passed=_passed(case, scores, limit),
        scores={name: round(value, 3) for name, value in scores.items()},
        top_similarity=round(max((m.similarity for m in visible), default=0.0), 3),
        relevant_rank=best[0] if best else None,
        relevant_similarity=round(best[1], 3) if best else None,
    )


async def _pools(
    embedder: Embedder, repo: GoldenRepo, queries: Sequence[str], concurrency: int
) -> dict[str, tuple[GoldenMatch, ...]]:
    vectors = await embedder.embed(queries)
    embeddings = dict(zip(queries, vectors, strict=True))
    slots = asyncio.Semaphore(concurrency)

    async def search(query: str) -> tuple[str, tuple[GoldenMatch, ...]]:
        async with slots:
            return query, await repo.search(query, embeddings[query], _POOL, 0.0)

    return dict(await asyncio.gather(*(search(query) for query in queries)))


async def evaluate(
    embedder: Embedder,
    repo: GoldenRepo,
    cases: Sequence[RetrievalCase],
    *,
    limit: int,
    min_similarity: float,
    concurrency: int = 4,
) -> list[RetrievalResult]:
    queries = sorted({case.query for case in cases})
    pools = await _pools(embedder, repo, queries, concurrency)
    return [score_case(case, pools[case.query], limit, min_similarity) for case in cases]


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def summarize(mode: Mode, results: Sequence[RetrievalResult]) -> ModeSummary:
    names = sorted({name for result in results for name in result.scores})
    themes: dict[str, list[float]] = defaultdict(list)
    for result in results:
        if result.theme is not None:
            themes[result.theme].append(1.0 if result.passed else 0.0)
    return ModeSummary(
        mode=mode,
        cases=len(results),
        pass_rate=_mean([1.0 if r.passed else 0.0 for r in results]),
        scores={
            name: _mean([r.scores[name] for r in results if name in r.scores]) for name in names
        },
        by_theme={theme: _mean(values) for theme, values in sorted(themes.items())},
    )


def calibrate(results: Sequence[RetrievalResult], min_similarity: float) -> Calibration:
    relevant = [
        r.relevant_similarity
        for r in results
        if r.mode is Mode.LABELLED and r.relevant_similarity is not None
    ]
    off_topic = [r.top_similarity for r in results if r.mode is Mode.OFF_TOPIC]
    return Calibration(
        min_similarity=min_similarity,
        relevant_similarity_min=min(relevant) if relevant else None,
        relevant_similarity_median=round(median(relevant), 3) if relevant else None,
        off_topic_similarity_max=max(off_topic) if off_topic else None,
    )


def build_report(
    results: Sequence[RetrievalResult], metadata: dict[str, str], min_similarity: float
) -> RetrievalReport:
    started = datetime.now(UTC)
    summaries = tuple(
        summarize(mode, [r for r in results if r.mode is mode])
        for mode in Mode
        if any(r.mode is mode for r in results)
    )
    return RetrievalReport(
        run_name=f"retrieval-{started:%Y%m%d-%H%M%S}",
        started_at=started,
        metadata=metadata,
        summaries=summaries,
        calibration=calibrate(results, min_similarity),
        results=tuple(results),
    )


class Gates(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_hit: float = Field(ge=0, le=1)
    min_mrr: float = Field(ge=0, le=1)
    min_abstention: float = Field(ge=0, le=1)


def gate(report: RetrievalReport, gates: Gates, limit: int) -> list[str]:
    failures: list[str] = []
    for summary in report.summaries:
        hit = summary.scores.get(f"hit@{limit}", 0.0)
        mrr = summary.scores.get("mrr", 0.0)
        abstained = summary.scores.get("abstained", 0.0)
        if summary.mode is Mode.IDENTITY and summary.pass_rate < 1.0:
            failures.append(
                f"identity: {summary.pass_rate:.0%} of trios served for their own question"
            )
        if summary.mode is Mode.LABELLED and hit < gates.min_hit:
            failures.append(f"labelled: hit@{limit} {hit:.2f} < {gates.min_hit:.2f}")
        if summary.mode is Mode.LABELLED and mrr < gates.min_mrr:
            failures.append(f"labelled: mrr {mrr:.2f} < {gates.min_mrr:.2f}")
        if summary.mode is Mode.OFF_TOPIC and abstained < gates.min_abstention:
            failures.append(f"off_topic: abstained {abstained:.2f} < {gates.min_abstention:.2f}")
    return failures


def _miss_note(result: RetrievalResult) -> str:
    if result.mode is Mode.OFF_TOPIC:
        return f"served {result.returned[0]} at similarity {result.top_similarity}"
    if result.relevant_rank is None:
        return "relevant trio absent from the fused top 30"
    return (
        f"relevant at fused rank {result.relevant_rank}, "
        f"similarity {result.relevant_similarity}; served {list(result.returned)}"
    )


def render(report: RetrievalReport) -> None:
    for summary in report.summaries:
        table = Table(
            "metric",
            "mean",
            title=f"{summary.mode} · {summary.cases} cases · pass {summary.pass_rate:.0%}",
        )
        for name, value in summary.scores.items():
            table.add_row(name, f"{value:g}")
        console.print(table)
        for result in report.results:
            if result.mode is summary.mode and not result.passed:
                console.print(f"  [red]{result.case_id}[/]: {_miss_note(result)}")
    console.print(f"[bold]{_calibration_line(report.calibration)}[/]")


def _calibration_line(calibration: Calibration) -> str:
    return (
        f"threshold {calibration.min_similarity}: relevant similarity min "
        f"{calibration.relevant_similarity_min} / median {calibration.relevant_similarity_median}, "
        f"off-topic max {calibration.off_topic_similarity_max}, margin {calibration.margin}"
    )


def markdown(report: RetrievalReport) -> str:
    lines = [f"# Golden retrieval eval {report.run_name}", ""]
    lines += [f"- {key}: {value}" for key, value in report.metadata.items()]
    lines += [f"- mode `{mode}`: {meaning}" for mode, meaning in _MODE_MEANING.items()]
    lines += ["", f"**{_calibration_line(report.calibration)}**"]
    for summary in report.summaries:
        lines += [
            "",
            f"## {summary.mode} — {summary.cases} cases, pass rate {summary.pass_rate:.0%}",
            "",
            "| metric | mean |",
            "|---|---|",
        ]
        lines += [f"| {name} | {value:g} |" for name, value in summary.scores.items()]
        if summary.by_theme:
            lines += ["", "| theme | pass rate |", "|---|---|"]
            lines += [f"| {theme} | {rate:.0%} |" for theme, rate in summary.by_theme.items()]
        lines += _case_rows(report, summary.mode)
    return "\n".join(lines) + "\n"


def _case_rows(report: RetrievalReport, mode: Mode) -> list[str]:
    rows = ["", "| case | result | scores | top sim | note |", "|---|---|---|---|---|"]
    for result in (r for r in report.results if r.mode is mode):
        scores = " ".join(f"{k}={v:g}" for k, v in result.scores.items())
        verdict = "pass" if result.passed else "fail"
        note = "" if result.passed else _miss_note(result)
        rows.append(
            f"| {result.case_id} | {verdict} | {scores} | {result.top_similarity} | {note} |"
        )
    return rows


def write(report: RetrievalReport, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / report.run_name
    stem.with_suffix(".json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    return stem.with_suffix(".md")


def run_metadata(settings: Settings, trios: int, cases: int) -> dict[str, str]:
    return {
        "embedding_model": settings.embedding_model,
        "golden_examples_limit": str(settings.golden_examples_limit),
        "golden_min_similarity": str(settings.golden_min_similarity),
        "fused_pool": str(_POOL),
        "trios": str(trios),
        "cases": str(cases),
        "dataset": str(RETRIEVAL_DATASET),
    }


async def _execute(
    settings: Settings, cases: Sequence[RetrievalCase], concurrency: int
) -> list[RetrievalResult]:
    engine, sessions = create_database(settings)
    try:
        return await evaluate(
            GatewayEmbedder(settings),
            SqlGoldenRepo(sessions),
            cases,
            limit=settings.golden_examples_limit,
            min_similarity=settings.golden_min_similarity,
            concurrency=concurrency,
        )
    finally:
        await engine.dispose()


def plan_cases(trios: Sequence[GoldenTrio], modes: set[Mode], ids: set[str]) -> list[RetrievalCase]:
    labelled = read_cases(RETRIEVAL_DATASET)
    validate_cases(labelled, trios)
    cases = [*labelled, *trio_cases(trios)]
    return [c for c in cases if c.mode in modes and (not ids or c.id in ids)]


@app.command()
def main(
    *,
    mode: Annotated[list[Mode] | None, typer.Option(help="Modes to run (default all).")] = None,
    case: Annotated[list[str] | None, typer.Option(help="Run only these case ids.")] = None,
    concurrency: Annotated[int, typer.Option(min=1, max=16)] = 4,
    min_hit: Annotated[float, typer.Option(min=0, max=1)] = 0.9,
    min_mrr: Annotated[float, typer.Option(min=0, max=1)] = 0.7,
    min_abstention: Annotated[float, typer.Option(min=0, max=1)] = 1.0,
) -> None:
    settings = get_settings()
    if settings.golden_examples_limit == 0:
        raise typer.BadParameter("golden_examples_limit is 0: retrieval is off", param_hint="env")
    trios = read_trios(settings.golden_dataset)
    cases = plan_cases(trios, set(mode or Mode), set(case or ()))
    console.print(f"[dim]{len(cases)} retrieval cases, embeddings only, no chat model[/]")
    results = asyncio.run(_execute(settings, cases, concurrency))
    metadata = run_metadata(settings, len(trios), len(cases))
    report = build_report(results, metadata, settings.golden_min_similarity)
    render(report)
    console.print(f"[dim]report: {write(report, REPORTS_DIR)}[/]")
    gates = Gates(min_hit=min_hit, min_mrr=min_mrr, min_abstention=min_abstention)
    failures = gate(report, gates, settings.golden_examples_limit)
    for failure in failures:
        console.print(f"[bold red]gate failed:[/] {failure}")
    if failures:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
