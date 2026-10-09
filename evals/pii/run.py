import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Annotated

import anyio
import typer
from google.cloud.dlp_v2 import DlpServiceAsyncClient
from rich.console import Console

from evals.pii.brands import fetch_brands
from evals.pii.cases import PiiCase
from evals.pii.dlp import MeteredInspectClient, Pacer, ensure_adc_quota_project
from evals.pii.generate import PII_DATASET
from evals.pii.report import PiiReport, render, write
from evals.pii.scoring import CaseOutcome, latency, score, summarize
from opsfleet_agent.core.config import DlpSettings
from opsfleet_agent.gateways.dlp.redactor import DlpRedactor
from opsfleet_agent.interactors.pii_redaction import LayeredRedactor, RegexRedactor
from opsfleet_agent.ports.privacy import PiiRedactor

REPORTS_DIR = Path("var/evals")

app = typer.Typer(add_completion=False)
console = Console()


@dataclass(frozen=True)
class Subject:
    mode: str
    redactor: PiiRedactor
    meter: MeteredInspectClient | None = None
    pacer: Pacer | None = None


def read_cases(path: Path, categories: set[str], ids: set[str]) -> list[PiiCase]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    cases = [PiiCase.model_validate_json(line) for line in lines]
    return [
        case
        for case in cases
        if (not categories or case.category in categories) and (not ids or case.id in ids)
    ]


async def _dlp_subject(settings: DlpSettings, per_second: float) -> Subject:
    ensure_adc_quota_project(settings.gcp_project)
    brands = await fetch_brands(settings)
    meter = MeteredInspectClient(DlpServiceAsyncClient())

    async def brand_names() -> tuple[str, ...]:
        return brands

    dlp = DlpRedactor(
        meter,
        project=settings.gcp_project,
        location=settings.dlp_location,
        allowed_terms=brand_names,
        timeout_seconds=settings.dlp_timeout_seconds,
    )
    return Subject(
        mode=f"regex+dlp ({settings.gcp_project})",
        redactor=LayeredRedactor([RegexRedactor(), dlp]),
        meter=meter,
        pacer=Pacer(per_second),
    )


async def _one(subject: Subject, case: PiiCase, limiter: anyio.CapacityLimiter) -> CaseOutcome:
    async with limiter:
        if subject.pacer is not None:
            await subject.pacer.wait()
        started = perf_counter()
        redaction = await subject.redactor.redact(case.text)
        return score(case, redaction, perf_counter() - started)


async def run_cases(
    subject: Subject, cases: Sequence[PiiCase], concurrency: int
) -> list[CaseOutcome]:
    limiter = anyio.CapacityLimiter(concurrency)
    return list(await asyncio.gather(*(_one(subject, case, limiter) for case in cases)))


def build_report(
    subject: Subject,
    cases: Sequence[PiiCase],
    outcomes: Sequence[CaseOutcome],
    started: datetime,
) -> PiiReport:
    meter = subject.meter
    timing = latency(outcomes, meter.seconds if meter else (), meter.retries if meter else 0)
    return PiiReport(
        run_name=f"pii-{started:%Y%m%d-%H%M%S}",
        started_at=started,
        mode=subject.mode,
        dataset=str(PII_DATASET),
        summary=summarize(cases, outcomes, timing),
        outcomes=tuple(outcomes),
    )


async def _execute(
    cases: Sequence[PiiCase], regex_only: bool, concurrency: int, per_second: float
) -> PiiReport:
    started = datetime.now(UTC)
    subject = (
        Subject(mode="regex", redactor=RegexRedactor())
        if regex_only
        else await _dlp_subject(DlpSettings(), per_second)
    )
    outcomes = await run_cases(subject, cases, concurrency)
    return build_report(subject, cases, outcomes, started)


@app.command()
def main(
    *,
    regex_only: Annotated[bool, typer.Option(help="Local rules only, no DLP calls.")] = False,
    category: Annotated[list[str] | None, typer.Option(help="Only these categories.")] = None,
    case: Annotated[list[str] | None, typer.Option(help="Only these case ids.")] = None,
    concurrency: Annotated[int, typer.Option(min=1, max=16)] = 6,
    per_second: Annotated[float, typer.Option(min=0.5, max=9.0, help="DLP calls/second.")] = 8.0,
) -> None:
    cases = read_cases(PII_DATASET, set(category or ()), set(case or ()))
    if not cases:
        raise typer.BadParameter("no cases match the filters")
    report = asyncio.run(_execute(cases, regex_only, concurrency, per_second))
    render(report, console)
    json_path, md_path = write(report, REPORTS_DIR)
    console.print(f"[dim]reports: {json_path} {md_path}[/]")


if __name__ == "__main__":
    app()
