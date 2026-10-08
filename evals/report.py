from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from rich.console import Console
from rich.table import Table

from evals.cases import CaseResult, Suite
from evals.scoring import middle, percentile


class SuiteSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    suite: Suite
    cases: int
    pass_rate: float
    scores: dict[str, float]
    by_category: dict[str, float]
    p50_seconds: float
    p95_seconds: float
    mean_llm_calls: float
    mean_queries: float
    mean_retries: float


class EvalReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_name: str
    started_at: datetime
    metadata: dict[str, str]
    summaries: tuple[SuiteSummary, ...]
    results: tuple[CaseResult, ...]


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def summarize(suite: Suite, results: Sequence[CaseResult]) -> SuiteSummary:
    names = sorted({name for result in results for name in result.scores})
    categories: dict[str, list[float]] = defaultdict(list)
    for result in results:
        categories[result.category].append(1.0 if result.passed else 0.0)
    stats = [result.stats for result in results if result.stats is not None]
    seconds = [s.seconds for s in stats]
    return SuiteSummary(
        suite=suite,
        cases=len(results),
        pass_rate=_mean([1.0 if r.passed else 0.0 for r in results]),
        scores={
            name: _mean([r.scores[name] for r in results if name in r.scores]) for name in names
        },
        by_category={name: _mean(values) for name, values in sorted(categories.items())},
        p50_seconds=round(middle(seconds), 1),
        p95_seconds=round(percentile(seconds, 0.95), 1),
        mean_llm_calls=_mean([s.llm_calls for s in stats]),
        mean_queries=_mean([s.queries for s in stats]),
        mean_retries=_mean([s.retries for s in stats]),
    )


def render(report: EvalReport, console: Console) -> None:
    for summary in report.summaries:
        table = Table(
            title=f"{summary.suite} · {summary.cases} cases · pass {summary.pass_rate:.0%}"
        )
        table.add_column("case")
        table.add_column("result")
        table.add_column("scores")
        table.add_column("s", justify="right")
        table.add_column("calls", justify="right")
        for result in (r for r in report.results if r.suite is summary.suite):
            mark = "[green]pass[/]" if result.passed else f"[red]fail[/] {result.error_code or ''}"
            scores = " ".join(f"{k}={v:g}" for k, v in result.scores.items())
            stats = result.stats
            table.add_row(
                result.case_id,
                mark,
                scores,
                f"{stats.seconds:.0f}" if stats else "",
                str(stats.llm_calls) if stats else "",
            )
        console.print(table)
        means = " · ".join(f"{k} {v:.2f}" for k, v in summary.scores.items())
        console.print(
            f"[bold]{summary.suite}[/] {means} · p50 {summary.p50_seconds}s · "
            f"p95 {summary.p95_seconds}s · calls {summary.mean_llm_calls} · "
            f"queries {summary.mean_queries} · retries {summary.mean_retries}"
        )
        for result in report.results:
            if result.suite is summary.suite and not result.passed:
                console.print(f"  [red]{result.case_id}[/]: {'; '.join(result.notes)[:300]}")


def markdown(report: EvalReport) -> str:
    lines = [f"# Eval run {report.run_name}", ""]
    lines += [f"- {key}: {value}" for key, value in report.metadata.items()]
    for summary in report.summaries:
        lines += [
            "",
            f"## {summary.suite} — {summary.cases} cases, pass rate {summary.pass_rate:.0%}",
            "",
            "| metric | value |",
            "|---|---|",
        ]
        lines += [f"| {name} | {value:.2f} |" for name, value in summary.scores.items()]
        lines += [
            f"| p50 latency (s) | {summary.p50_seconds} |",
            f"| p95 latency (s) | {summary.p95_seconds} |",
            f"| mean LLM calls | {summary.mean_llm_calls} |",
            f"| mean queries | {summary.mean_queries} |",
            f"| mean SQL retries | {summary.mean_retries} |",
            "",
            "| category | pass rate |",
            "|---|---|",
        ]
        lines += [f"| {name} | {rate:.0%} |" for name, rate in summary.by_category.items()]
        lines += _case_rows(report, summary.suite)
    return "\n".join(lines) + "\n"


def _case_rows(report: EvalReport, suite: Suite) -> list[str]:
    rows = ["", "| case | result | scores | s | LLM calls |", "|---|---|---|---|---|"]
    for result in (r for r in report.results if r.suite is suite):
        scores = " ".join(f"{k}={v:g}" for k, v in result.scores.items())
        verdict = "pass" if result.passed else f"fail {result.error_code or ''}".strip()
        seconds = f"{result.stats.seconds:.0f}" if result.stats else ""
        calls = str(result.stats.llm_calls) if result.stats else ""
        rows.append(f"| {result.case_id} | {verdict} | {scores} | {seconds} | {calls} |")
    return rows


def write(report: EvalReport, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / report.run_name
    stem.with_suffix(".json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    return stem.with_suffix(".md")
