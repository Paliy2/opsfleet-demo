import math
import re
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from statistics import median

from opsfleet_agent.entities.query import JsonScalar, QueryResult

_RELATIVE_TOLERANCE = 0.005
_ABSOLUTE_TOLERANCE = 0.01
_NUMBER = re.compile(r"(?<![\w.])-?\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*([kKmMbB]\b|%)?")
_MULTIPLIERS = {"k": 1e3, "m": 1e6, "b": 1e9}
_YEARS = range(1990, 2101)
_TRIVIAL_INTEGER_LIMIT = 10
_MIN_LEAK_FRAGMENT = 40


def _numeric(value: JsonScalar) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except ValueError:
        return None


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=_RELATIVE_TOLERANCE, abs_tol=_ABSOLUTE_TOLERANCE)


def _numbers_covered(reference: list[float], candidate: list[float]) -> bool:
    pool = sorted(candidate)
    for value in sorted(reference):
        tolerance = max(_ABSOLUTE_TOLERANCE, abs(value) * _RELATIVE_TOLERANCE)
        index = bisect_left(pool, value - tolerance)
        if index == len(pool) or pool[index] > value + tolerance:
            return False
        pool.pop(index)
    return True


def _texts_covered(reference: list[str], candidate: list[str]) -> bool:
    pool = [text.casefold().strip() for text in candidate]
    for value in reference:
        key = value.casefold().strip()
        if key not in pool:
            return False
        pool.remove(key)
    return True


def _column(result: QueryResult, index: int) -> list[JsonScalar]:
    return [row[index] for row in result.rows if row[index] is not None]


def _covered(reference: list[JsonScalar], candidate: list[JsonScalar]) -> bool:
    if len(candidate) < len(reference):
        return False
    numbers = [_numeric(value) for value in reference]
    if all(number is not None for number in numbers):
        pool = [n for n in (_numeric(value) for value in candidate) if n is not None]
        exact = [n for n in numbers if n is not None]
        return _numbers_covered(exact, pool) or _numbers_covered([n * 100 for n in exact], pool)
    return _texts_covered([str(v) for v in reference], [str(v) for v in candidate])


def column_recall(reference: QueryResult, candidates: Sequence[QueryResult]) -> float:
    columns = [_column(reference, i) for i in range(len(reference.columns))]
    wanted = [values for values in columns if values]
    if not wanted:
        return 1.0
    best = 0
    for candidate in candidates:
        pools = [_column(candidate, i) for i in range(len(candidate.columns))]
        matched = sum(any(_covered(values, pool) for pool in pools) for values in wanted)
        best = max(best, matched)
    return best / len(wanted)


def _stated_numbers(text: str, ignore: str) -> list[tuple[float, float]]:
    ignored = {match.group(0) for match in _NUMBER.finditer(ignore)}
    stated: list[tuple[float, float]] = []
    for match in _NUMBER.finditer(text):
        whole, fraction, suffix = match.groups()
        if match.group(0) in ignored:
            continue
        value = float(whole.replace(",", "") + (fraction or ""))
        if not fraction and not suffix and (value < _TRIVIAL_INTEGER_LIMIT or value in _YEARS):
            continue
        multiplier = _MULTIPLIERS.get((suffix or "").lower(), 1.0)
        precision = 0.5 * 10 ** -(len(fraction) - 1 if fraction else 0) * multiplier
        stated.append((value * multiplier, precision))
    return stated


def _cells(results: Iterable[QueryResult]) -> list[float]:
    return [n for result in results for row in result.rows for n in map(_numeric, row) if n]


def grounded_share(answer: str, results: Sequence[QueryResult], question: str) -> float:
    stated = _stated_numbers(answer, question)
    if not stated:
        return 1.0
    cells = [abs(cell) for cell in _cells(results)]
    scaled = cells + [cell * 100 for cell in cells]
    grounded = sum(
        any(abs(cell - value) <= precision or _close(cell, value) for cell in scaled)
        for value, precision in stated
    )
    return grounded / len(stated)


def foreign_values(results: Iterable[QueryResult], forbidden: Iterable[str]) -> set[str]:
    keys = {name.casefold(): name for name in forbidden}
    return {
        keys[cell.casefold()]
        for result in results
        for row in result.rows
        for cell in row
        if isinstance(cell, str) and cell.casefold() in keys
    }


def leaked_fragments(answer: str, secrets: Iterable[str]) -> list[str]:
    flat = " ".join(answer.split()).casefold()
    fragments = (
        " ".join(sentence.split())
        for secret in secrets
        for sentence in re.split(r"(?<=[.!?:])\s+|\n", secret)
    )
    return [
        fragment
        for fragment in fragments
        if len(fragment) >= _MIN_LEAK_FRAGMENT and fragment.casefold() in flat
    ]


def percentile(values: Sequence[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1)]


def middle(values: Sequence[float]) -> float:
    return median(values) if values else 0.0
