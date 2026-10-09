import asyncio
from collections.abc import Iterator
from random import Random
from string import Formatter
from typing import Annotated

import typer

from evals.pii.brands import DATASETS, fetch_brands, read_snapshot, write_snapshot
from evals.pii.cases import Keep, PiiCase, Preserved, Sensitive
from evals.pii.negatives import NEGATIVES, OTHER_BRANDS, PERSON_LIKE_BRANDS
from evals.pii.positives import MakeValue
from evals.pii.templates import MIXED, MIXED_CASES, MIXED_SLOTS, POSITIVES
from evals.pii.values import FIRST_NAMES, LAST_NAMES
from opsfleet_agent.core.config import DlpSettings
from opsfleet_agent.entities.privacy import PSEUDONYM_FIRST_NAMES, PSEUDONYM_LAST_NAMES

PII_DATASET = DATASETS / "pii.jsonl"
SEED = 35
CONTEXT_BRANDS = (*PERSON_LIKE_BRANDS, *OTHER_BRANDS)

app = typer.Typer(add_completion=False)


class DatasetError(ValueError):
    pass


def _brands_in(text: str, keep: tuple[Preserved, ...]) -> tuple[Preserved, ...]:
    kept = {item.value for item in keep}
    found = [brand for brand in CONTEXT_BRANDS if brand in text and brand not in kept]
    return (*keep, *(Preserved(value=brand, kind=Keep.BRAND) for brand in found))


def _case(case_id: str, category: str, text: str, sensitive: tuple[Sensitive, ...]) -> PiiCase:
    return PiiCase(
        id=case_id,
        category=category,
        text=text,
        contains_pii=bool(sensitive),
        expected_info_types=tuple(sorted({item.info_type for item in sensitive})),
        sensitive=sensitive,
        preserve=_brands_in(text, ()),
    )


def _single(rng: Random, case_id: str, category: str, make: MakeValue, template: str) -> PiiCase:
    a, b = rng.sample(CONTEXT_BRANDS, 2)
    value = make(rng)
    return _case(case_id, category, template.format(a=a, b=b, pii=value.value), (value,))


def _mixed(rng: Random, case_id: str, template: str) -> PiiCase:
    a, b = rng.sample(CONTEXT_BRANDS, 2)
    slots = [field for _, field, _, _ in Formatter().parse(template) if field in MIXED_SLOTS]
    values = {slot: MIXED_SLOTS[slot](rng) for slot in slots}
    text = template.format(a=a, b=b, **{slot: value.value for slot, value in values.items()})
    return _case(case_id, "mixed", text, tuple(values.values()))


def _positives(rng: Random) -> Iterator[PiiCase]:
    for category, count, make, templates in POSITIVES:
        for index in range(count):
            template = templates[index % len(templates)]
            yield _single(rng, f"pos-{category}-{index + 1:03d}", category, make, template)
    for index in range(MIXED_CASES):
        yield _mixed(rng, f"pos-mixed-{index + 1:03d}", MIXED[index % len(MIXED)])


def _negatives(rng: Random) -> Iterator[PiiCase]:
    for category, count, make in NEGATIVES:
        for index in range(count):
            text, keep = make(rng)
            yield PiiCase(
                id=f"neg-{category}-{index + 1:03d}",
                category=category,
                text=text,
                contains_pii=False,
                expected_info_types=(),
                sensitive=(),
                preserve=_brands_in(text, keep),
            )


def _check_vocabulary(brands: tuple[str, ...]) -> None:
    known = {brand.casefold() for brand in brands}
    missing = [brand for brand in CONTEXT_BRANDS if brand.casefold() not in known]
    if missing:
        raise DatasetError(f"context brands missing from the catalog: {missing}")
    synthetic = {name.casefold() for name in (*FIRST_NAMES, *LAST_NAMES)}
    reserved = {n.casefold() for n in (*PSEUDONYM_FIRST_NAMES, *PSEUDONYM_LAST_NAMES, *brands)}
    clashes = sorted(synthetic & reserved)
    if clashes:
        raise DatasetError(f"synthetic names clash with brands or pseudonyms: {clashes}")


def _check_typos(cases: list[PiiCase], brands: tuple[str, ...]) -> None:
    known = {brand.casefold() for brand in brands}
    real = [
        item.value
        for case in cases
        for item in case.preserve
        if item.kind is Keep.MISSPELLED_BRAND and item.value.casefold() in known
    ]
    if real:
        raise DatasetError(f"misspellings that are real brands: {real}")


def build_cases(seed: int, brands: tuple[str, ...]) -> list[PiiCase]:
    _check_vocabulary(brands)
    rng = Random(seed)
    cases = [*_positives(rng), *_negatives(rng)]
    _check_typos(cases, brands)
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise DatasetError("duplicate case ids")
    return cases


@app.command()
def main(
    *,
    seed: Annotated[int, typer.Option(help="Random seed.")] = SEED,
    refresh_brands: Annotated[
        bool, typer.Option(help="Reload the brand snapshot from BigQuery first.")
    ] = False,
) -> None:
    if refresh_brands:
        count = write_snapshot(asyncio.run(fetch_brands(DlpSettings())))
        typer.echo(f"brand snapshot: {count} brands")
    cases = build_cases(seed, read_snapshot())
    lines = "".join(case.model_dump_json() + "\n" for case in cases)
    PII_DATASET.write_text(lines, encoding="utf-8")
    positives = sum(case.contains_pii for case in cases)
    typer.echo(f"{PII_DATASET}: {len(cases)} cases, {positives} positive")


if __name__ == "__main__":
    app()
