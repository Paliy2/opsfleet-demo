import math
import random
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

_RESAMPLES = 2000
_CONFIDENCE = 0.95
_SEED = 7
_ALPHA = 0.05


class PairedDelta(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: str
    pairs: int
    baseline: float
    treatment: float
    delta: float
    low: float
    high: float
    wins: int
    ties: int
    losses: int
    p_value: float = Field(ge=0, le=1)

    @property
    def significant(self) -> bool:
        return (self.low > 0 or self.high < 0) and self.p_value < _ALPHA


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def bootstrap_interval(
    deltas: Sequence[float], resamples: int = _RESAMPLES, seed: int = _SEED
) -> tuple[float, float]:
    rng = random.Random(seed)
    means: list[float] = sorted(_mean(rng.choices(deltas, k=len(deltas))) for _ in range(resamples))
    tail = (1 - _CONFIDENCE) / 2
    return means[int(tail * resamples)], means[int((1 - tail) * resamples) - 1]


def sign_test(wins: int, losses: int) -> float:
    trials = wins + losses
    if trials == 0:
        return 1.0
    extreme = min(wins, losses)
    tail = sum(math.comb(trials, k) for k in range(extreme + 1)) / (1 << trials)
    return min(1.0, 2 * tail)


def compare(metric: str, baseline: Sequence[float], treatment: Sequence[float]) -> PairedDelta:
    if len(baseline) != len(treatment) or not baseline:
        raise ValueError(f"{metric}: need equal, non-empty paired samples")
    deltas = [t - b for b, t in zip(baseline, treatment, strict=True)]
    wins = sum(d > 0 for d in deltas)
    losses = sum(d < 0 for d in deltas)
    low, high = bootstrap_interval(deltas)
    return PairedDelta(
        metric=metric,
        pairs=len(deltas),
        baseline=round(_mean(baseline), 3),
        treatment=round(_mean(treatment), 3),
        delta=round(_mean(deltas), 3),
        low=round(low, 3),
        high=round(high, 3),
        wins=wins,
        ties=len(deltas) - wins - losses,
        losses=losses,
        p_value=round(sign_test(wins, losses), 4),
    )
