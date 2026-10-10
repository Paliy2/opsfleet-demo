import pytest

from evals.ablation import AblationPlan, Arm, ArmRun, comparisons, verdict
from evals.cases import Capability, CapabilityCase, CaseResult, Suite, TurnStats
from evals.paired import bootstrap_interval, compare, sign_test
from tests.fakes import golden_trio


def run(arm: Arm, case_id: str, correctness: float, calls: int = 5, repeat: int = 0) -> ArmRun:
    return ArmRun(
        arm=arm,
        repeat=repeat,
        result=CaseResult(
            suite=Suite.SQL,
            case_id=case_id,
            category="revenue-trends",
            passed=correctness == 1.0,
            scores={"correctness": correctness},
            stats=TurnStats(seconds=10, llm_calls=calls, queries=2, retries=0, examples=0),
        ),
    )


def test_sign_test_matches_binomial_tails() -> None:
    assert sign_test(0, 0) == 1.0
    assert sign_test(6, 0) == pytest.approx(2 / 64)
    assert sign_test(5, 5) == 1.0


def test_bootstrap_of_constant_deltas_is_a_point() -> None:
    assert bootstrap_interval([0.5, 0.5, 0.5]) == (0.5, 0.5)


def test_consistent_gain_is_significant() -> None:
    delta = compare("correctness", [0.5] * 8, [1.0] * 8)
    assert (delta.delta, delta.wins, delta.ties, delta.losses) == (0.5, 8, 0, 0)
    assert delta.significant


def test_single_pair_is_never_significant() -> None:
    assert not compare("correctness", [0.0], [1.0]).significant


def test_mixed_results_are_not_significant() -> None:
    delta = compare("correctness", [1.0, 0.0, 1.0, 0.0], [0.0, 1.0, 1.0, 0.0])
    assert delta.delta == 0.0
    assert not delta.significant


def test_unpaired_samples_fail_fast() -> None:
    with pytest.raises(ValueError, match="paired"):
        compare("correctness", [1.0], [])


def test_cases_are_paired_by_id_and_repeats_are_averaged() -> None:
    runs = [
        run(Arm.NONE, "a", 0.0),
        run(Arm.NONE, "a", 1.0, repeat=1),
        run(Arm.GOLDEN, "a", 1.0, calls=3),
        run(Arm.NONE, "b", 0.5),
        run(Arm.GOLDEN, "b", 1.0, calls=3),
        run(Arm.GOLDEN, "only-treated", 1.0),
    ]
    [comparison] = comparisons(runs, [Arm.NONE, Arm.GOLDEN])
    correctness = comparison.primary
    assert (correctness.pairs, correctness.baseline, correctness.treatment) == (2, 0.5, 1.0)
    calls = next(d for d in comparison.deltas if d.metric == "llm_calls")
    assert calls.delta == -2.0


def test_stale_cases_without_stats_are_left_out() -> None:
    stale = run(Arm.GOLDEN, "a", 0.0).result.model_copy(update={"stats": None})
    runs = [run(Arm.NONE, "a", 0.0), ArmRun(arm=Arm.GOLDEN, repeat=0, result=stale)]
    assert comparisons(runs, [Arm.NONE, Arm.GOLDEN]) == ()


def test_verdict_names_no_effect_when_inconclusive() -> None:
    runs = [run(Arm.NONE, "a", 0.5), run(Arm.GOLDEN, "a", 1.0)]
    [comparison] = comparisons(runs, [Arm.NONE, Arm.GOLDEN])
    assert verdict(comparison).startswith("no detectable effect on correctness")


def test_unseen_theme_arm_does_not_apply_to_capability_cases() -> None:
    plan = AblationPlan(
        trios=(golden_trio("trio-a"), golden_trio("trio-b")),
        capability=(
            CapabilityCase(
                id="c", category=Capability.SCHEMA, user="ceo", turns=("hi",), rubric=("ok",)
            ),
        ),
        arms=(Arm.NONE, Arm.GOLDEN, Arm.UNSEEN_THEME),
        repeats=2,
    )
    assert plan.turns == (2 * 3 + 1 * 2) * 2
