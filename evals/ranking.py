import math
from collections.abc import Sequence, Set


def _check(relevant: Set[str], k: int) -> None:
    if not relevant:
        raise ValueError("ranking metrics need at least one relevant id")
    if k < 1:
        raise ValueError("k must be positive")


def hits(ranked: Sequence[str], relevant: Set[str], k: int) -> int:
    _check(relevant, k)
    return sum(1 for item in ranked[:k] if item in relevant)


def hit_rate(ranked: Sequence[str], relevant: Set[str], k: int) -> float:
    return 1.0 if hits(ranked, relevant, k) else 0.0


def recall(ranked: Sequence[str], relevant: Set[str], k: int) -> float:
    return hits(ranked, relevant, k) / len(relevant)


def precision(ranked: Sequence[str], relevant: Set[str], k: int) -> float:
    returned = len(ranked[:k])
    return hits(ranked, relevant, k) / returned if returned else 0.0


def reciprocal_rank(ranked: Sequence[str], relevant: Set[str]) -> float:
    _check(relevant, 1)
    for position, item in enumerate(ranked, start=1):
        if item in relevant:
            return 1.0 / position
    return 0.0


def _discounted_gain(position: int) -> float:
    return 1.0 / math.log2(position + 1)


def ndcg(ranked: Sequence[str], relevant: Set[str], k: int) -> float:
    _check(relevant, k)
    gained = sum(
        _discounted_gain(position)
        for position, item in enumerate(ranked[:k], start=1)
        if item in relevant
    )
    ideal = sum(_discounted_gain(position) for position in range(1, min(k, len(relevant)) + 1))
    return gained / ideal
