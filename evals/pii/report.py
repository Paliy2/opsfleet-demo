from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from rich.console import Console
from rich.table import Table

from evals.pii.scoring import CaseOutcome, PiiSummary, Rate


class PiiReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_name: str
    started_at: datetime
    mode: str
    dataset: str
    summary: PiiSummary
    outcomes: tuple[CaseOutcome, ...]

    @property
    def misses(self) -> tuple[CaseOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.passed)

    @property
    def over_redacted_positives(self) -> tuple[CaseOutcome, ...]:
        return tuple(o for o in self.outcomes if o.contains_pii and o.masked)


def _cell(rate: Rate) -> str:
    return f"{rate.rate:.1%} ({rate.hits}/{rate.total})"


def headline(summary: PiiSummary) -> list[tuple[str, str]]:
    timing = summary.latency
    return [
        ("Overall score", _cell(summary.score)),
        ("Detection (positives fully redacted)", _cell(summary.detection)),
        ("False alarms (negatives)", _cell(summary.false_alarms)),
        ("  negatives with any finding", _cell(summary.negatives_with_findings)),
        ("  negatives with a kept term masked", _cell(summary.negatives_with_masked_terms)),
        ("Positives with a kept term masked", _cell(summary.positives_with_masked_terms)),
        ("Brand preservation", _cell(summary.brand_preservation)),
        ("Case latency p50 / p95", f"{timing.case_p50_ms} / {timing.case_p95_ms} ms"),
        (
            "DLP call latency p50 / p95",
            f"{timing.dlp_p50_ms} / {timing.dlp_p95_ms} ms "
            f"({timing.dlp_calls} calls, {timing.dlp_retries} retries)",
        ),
    ]


def _table(title: str, rows: list[tuple[str, str]]) -> Table:
    table = Table(title=title, title_justify="left")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for name, value in rows:
        table.add_row(name, value)
    return table


def breakdowns(summary: PiiSummary) -> dict[str, list[tuple[str, str]]]:
    return {
        "Recall by info type": [(str(k), _cell(v)) for k, v in summary.by_type.items()],
        "Pass rate by category": [(k, _cell(v)) for k, v in summary.by_category.items()],
        "Preservation by kind": [(k, _cell(v)) for k, v in summary.preservation.items()],
    }


def render(report: PiiReport, console: Console) -> None:
    console.print(
        _table(f"PII redaction: {report.run_name} ({report.mode})", headline(report.summary))
    )
    for title, rows in breakdowns(report.summary).items():
        console.print(_table(title, rows))
    console.print(f"[bold]{len(report.misses)} misses[/]; details in the markdown report")


def _md_table(title: str, rows: list[tuple[str, str]]) -> str:
    lines = [f"### {title}", "", "| Metric | Value |", "|---|---|"]
    lines += [f"| {name.strip()} | {value} |" for name, value in rows]
    return "\n".join(lines)


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _miss_line(outcome: CaseOutcome) -> str:
    leaked = "; ".join(f"{leak.info_type}: {', '.join(leak.fragments)}" for leak in outcome.leaked)
    masked = "; ".join(f"{item.kind}: {item.value}" for item in outcome.masked)
    problem = leaked if outcome.contains_pii else masked or f"findings {list(outcome.findings)}"
    return (
        f"| {outcome.id} | {_escape(outcome.text)} | {_escape(outcome.redacted)} | "
        f"{_escape(problem)} |"
    )


def _cases_table(title: str, outcomes: tuple[CaseOutcome, ...]) -> str:
    header = [f"## {title} ({len(outcomes)})", "", "| Id | Text | Redacted | Problem |"]
    return "\n".join([*header, "|---|---|---|---|", *map(_miss_line, outcomes)])


def _over_line(outcome: CaseOutcome) -> str:
    masked = "; ".join(f"{item.kind}: {item.value}" for item in outcome.masked)
    return f"| {outcome.id} | {_escape(outcome.redacted)} | {_escape(masked)} |"


def markdown(report: PiiReport) -> str:
    sections = [
        f"# PII redaction benchmark: {report.run_name}",
        f"Mode: `{report.mode}`. Dataset: `{report.dataset}`. "
        f"Started {report.started_at:%Y-%m-%d %H:%M} UTC.",
        _md_table("Summary", headline(report.summary)),
        *(_md_table(title, rows) for title, rows in breakdowns(report.summary).items()),
        _cases_table("Misses", report.misses),
        "\n".join(
            [
                f"## Positives with a kept term masked ({len(report.over_redacted_positives)})",
                "",
                "| Id | Redacted | Masked |",
                "|---|---|---|",
                *map(_over_line, report.over_redacted_positives),
            ]
        ),
    ]
    return "\n\n".join(sections) + "\n"


def write(report: PiiReport, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{report.run_name}.json"
    md_path = directory / f"{report.run_name}.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(markdown(report), encoding="utf-8")
    return json_path, md_path
