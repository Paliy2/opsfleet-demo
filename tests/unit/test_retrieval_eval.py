import pytest

from evals.ranking import hit_rate, ndcg, precision, recall, reciprocal_rank
from evals.retrieval import (
    RETRIEVAL_DATASET,
    Gates,
    Mode,
    RetrievalCase,
    build_report,
    evaluate,
    gate,
    read_cases,
    score_case,
    trio_cases,
    validate_cases,
)
from opsfleet_agent.entities.golden import GoldenMatch, GoldenTrio
from opsfleet_agent.gateways.golden_dataset import read_trios
from tests.fakes import FakeEmbedder, FakeGoldenRepo, golden_trio

LIMIT = 6
THRESHOLD = 0.55


def trio(trio_id: str, category: str = "revenue-trends") -> GoldenTrio:
    return golden_trio(trio_id).model_copy(update={"category": category})


def match(trio_id: str, similarity: float, category: str = "revenue-trends") -> GoldenMatch:
    return GoldenMatch(trio=trio(trio_id, category), similarity=similarity, score=similarity / 10)


def labelled(relevant: set[str], theme: str | None = "revenue-trends") -> RetrievalCase:
    return RetrievalCase(
        id="case",
        mode=Mode.LABELLED,
        query="sales by month",
        relevant=frozenset(relevant),
        theme=theme,
    )


def test_hit_recall_precision_at_k() -> None:
    ranked = ["a", "b", "c", "d"]

    assert hit_rate(ranked, {"c"}, 2) == 0.0
    assert hit_rate(ranked, {"c"}, 3) == 1.0
    assert recall(ranked, {"a", "c", "z"}, 3) == pytest.approx(2 / 3)
    assert precision(ranked, {"a", "c"}, 3) == pytest.approx(2 / 3)
    assert precision([], {"a"}, 3) == 0.0


def test_reciprocal_rank_and_ndcg() -> None:
    assert reciprocal_rank(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)
    assert reciprocal_rank(["x"], {"a"}) == 0.0
    assert ndcg(["a", "b"], {"a", "b"}, 2) == 1.0
    assert ndcg(["x", "a"], {"a"}, 2) == pytest.approx(0.631, abs=1e-3)
    assert ndcg(["x", "y"], {"a"}, 2) == 0.0


def test_ranking_metrics_fail_fast_without_relevant_ids() -> None:
    with pytest.raises(ValueError, match="relevant"):
        recall(["a"], set(), 1)


def test_scoring_applies_threshold_and_limit_like_production() -> None:
    pool = [match("far", 0.9), *(match(f"near-{i}", 0.7) for i in range(LIMIT)), match("late", 0.8)]

    result = score_case(labelled({"late"}), pool, LIMIT, THRESHOLD)

    assert result.returned == ("far", "near-0", "near-1", "near-2", "near-3", "near-4")
    assert result.scores[f"hit@{LIMIT}"] == 0.0
    assert result.relevant_rank == 8
    assert result.relevant_similarity == 0.8
    assert not result.passed


def test_relevant_below_threshold_is_a_diagnosed_miss() -> None:
    pool = [match("other", 0.6), match("wanted", 0.5)]

    result = score_case(labelled({"wanted"}), pool, LIMIT, THRESHOLD)

    assert result.returned == ("other",)
    assert result.scores["hit@1"] == 0.0
    assert (result.relevant_rank, result.relevant_similarity) == (2, 0.5)


def test_scores_for_a_perfect_second_place() -> None:
    pool = [match("other", 0.8, "margin"), match("wanted", 0.7), match("sibling", 0.6)]

    result = score_case(labelled({"wanted"}), pool, LIMIT, THRESHOLD)

    assert result.passed
    assert result.scores["hit@1"] == 0.0
    assert result.scores["hit@3"] == 1.0
    assert result.scores["mrr"] == 0.5
    assert result.scores[f"recall@{LIMIT}"] == 1.0
    assert result.scores[f"precision@{LIMIT}"] == pytest.approx(1 / 3, abs=1e-3)
    assert result.scores[f"ndcg@{LIMIT}"] == pytest.approx(0.631, abs=1e-3)
    assert result.scores[f"theme_precision@{LIMIT}"] == pytest.approx(2 / 3, abs=1e-3)
    assert result.scores["returned"] == 3.0


def test_off_topic_passes_only_when_nothing_is_served() -> None:
    case = RetrievalCase(id="off", mode=Mode.OFF_TOPIC, query="weather", relevant=frozenset())

    abstained = score_case(case, [match("any", 0.4)], LIMIT, THRESHOLD)
    served = score_case(case, [match("any", 0.6)], LIMIT, THRESHOLD)

    assert abstained.passed
    assert abstained.scores == {"abstained": 1.0, "returned": 0.0}
    assert not served.passed
    assert served.top_similarity == 0.6


def test_holdout_hides_the_trio_and_expects_its_siblings() -> None:
    trios = [trio("rt-a"), trio("rt-b"), trio("rt-c"), trio("m-a", "margin")]
    holdout = next(c for c in trio_cases(trios) if c.id == "holdout--rt-a")
    identity = next(c for c in trio_cases(trios) if c.id == "identity--rt-a")
    pool = [match("rt-a", 1.0), match("m-a", 0.8, "margin"), match("rt-b", 0.7)]

    hidden = score_case(holdout, pool, LIMIT, THRESHOLD)
    itself = score_case(identity, pool, LIMIT, THRESHOLD)

    assert holdout.relevant == {"rt-b", "rt-c"}
    assert hidden.returned == ("m-a", "rt-b")
    assert hidden.passed
    assert hidden.scores["mrr"] == 0.5
    assert hidden.scores[f"recall@{LIMIT}"] == 0.5
    assert itself.passed
    assert itself.scores["hit@1"] == 1.0


async def test_evaluate_embeds_each_query_once_and_searches_unfiltered() -> None:
    embedder, repo = FakeEmbedder(), FakeGoldenRepo(matches=(match("rt-a", 0.9),))
    cases = [labelled({"rt-a"}), labelled({"rt-a"}).model_copy(update={"id": "twin"})]

    results = await evaluate(embedder, repo, cases, limit=LIMIT, min_similarity=THRESHOLD)

    assert embedder.calls == [["sales by month"]]
    assert repo.searches == [("sales by month", 30, 0.0)]
    assert [r.passed for r in results] == [True, True]


def test_report_summarizes_per_mode_and_gates() -> None:
    pool_hit = [match("rt-a", 0.9)]
    results = [
        score_case(labelled({"rt-a"}), pool_hit, LIMIT, THRESHOLD),
        score_case(labelled({"rt-z"}), pool_hit, LIMIT, THRESHOLD),
        score_case(
            RetrievalCase(id="off", mode=Mode.OFF_TOPIC, query="poem", relevant=frozenset()),
            [match("rt-a", 0.3)],
            LIMIT,
            THRESHOLD,
        ),
    ]

    report = build_report(results, {}, THRESHOLD)

    by_mode = {s.mode: s for s in report.summaries}
    assert by_mode[Mode.LABELLED].scores[f"hit@{LIMIT}"] == 0.5
    assert by_mode[Mode.LABELLED].by_theme == {"revenue-trends": 0.5}
    assert by_mode[Mode.OFF_TOPIC].scores["abstained"] == 1.0
    assert report.calibration.relevant_similarity_min == 0.9
    assert report.calibration.off_topic_similarity_max == 0.3
    assert report.calibration.margin == pytest.approx(0.6)
    failures = gate(report, Gates(min_hit=0.9, min_mrr=0.7, min_abstention=1.0), LIMIT)
    assert failures == [f"labelled: hit@{LIMIT} 0.50 < 0.90", "labelled: mrr 0.50 < 0.70"]


def test_dataset_validation_rejects_unknown_ids_and_verbatim_questions() -> None:
    trios = [trio("rt-a")]
    verbatim = labelled({"rt-a"}).model_copy(update={"query": trios[0].question.upper()})
    unknown = labelled({"rt-missing"}).model_copy(update={"id": "unknown", "theme": "nope"})

    with pytest.raises(ValueError, match="rt-missing") as failure:
        validate_cases([verbatim, unknown], trios)

    assert "verbatim" in str(failure.value)
    assert "unknown theme nope" in str(failure.value)


def test_shipped_dataset_matches_the_golden_trios() -> None:
    trios = read_trios(RETRIEVAL_DATASET.parent.parent.parent / "data" / "golden" / "trios.jsonl")
    cases = read_cases(RETRIEVAL_DATASET)

    validate_cases(cases, trios)

    modes = {case.mode for case in cases}
    assert modes == {Mode.LABELLED, Mode.OFF_TOPIC}
    covered = {case.theme for case in cases if case.theme}
    assert covered == {trio.category for trio in trios}
