from uuid import uuid4

from langchain_core.messages import AIMessage, SystemMessage

from opsfleet_agent.entities.golden import GoldenMatch
from opsfleet_agent.interactors.golden import (
    LoadGoldenTrios,
    RetrieveExamples,
    render_examples,
)
from tests.fakes import (
    FakeEmbedder,
    FakeGoldenRepo,
    RecordingProgress,
    ScriptedChatModel,
    ScriptedQueryEngine,
    golden_trio,
)
from tests.unit.test_data_analyst import SCOPE, analyst


def match(trio_id: str = "revenue-trends--q", report: str = "Revenue was $123,456.") -> GoldenMatch:
    return GoldenMatch(trio=golden_trio(trio_id, report), similarity=0.8, score=0.03)


def test_examples_never_carry_report_numbers_across_scopes() -> None:
    rendered = render_examples([match(report="Diesel made $987,654.32 last quarter.")], 10_000)

    assert "987,654" not in rendered
    assert "SELECT ROUND(SUM(sale_price), 2)" in rendered
    assert "excluding Cancelled and Returned" in rendered


def test_examples_respect_char_budget() -> None:
    one = render_examples([match("a-one")], 10_000)

    rendered = render_examples([match("a-one"), match("a-two")], len(one) + 10)

    assert rendered == one


async def test_retrieval_searches_with_task_and_limits() -> None:
    repo = FakeGoldenRepo(matches=(match(),))
    retrieve = RetrieveExamples(FakeEmbedder(), repo, limit=6, min_similarity=0.6)

    found = await retrieve("revenue by brand last quarter")

    assert found == (match(),)
    assert repo.searches == [("revenue by brand last quarter", 6, 0.6)]


async def test_retrieval_disabled_skips_embedding() -> None:
    embedder = FakeEmbedder()

    found = await RetrieveExamples(embedder, FakeGoldenRepo(), limit=0, min_similarity=0.6)("q")

    assert found == ()
    assert embedder.calls == []


async def test_loading_embeds_question_and_approach_not_report() -> None:
    embedder, repo = FakeEmbedder(), FakeGoldenRepo()
    trio = golden_trio()

    assert await LoadGoldenTrios(embedder, repo)([trio]) == 1

    assert embedder.calls == [[f"{trio.question}\n{trio.approach}"]]
    assert repo.upserted == {trio.id: trio}


async def test_analyst_gets_examples_in_system_prompt() -> None:
    model = ScriptedChatModel(script=[AIMessage("No query needed.")])
    examples = RetrieveExamples(
        FakeEmbedder(), FakeGoldenRepo(matches=(match(),)), limit=6, min_similarity=0.6
    )
    progress = RecordingProgress()

    finding = await analyst(model, ScriptedQueryEngine(outcomes=[]), examples=examples).analyze(
        "revenue last quarter", SCOPE, uuid4(), progress
    )

    system = next(m for m in model.seen[0] if isinstance(m, SystemMessage))
    assert "## Validated examples" in str(system.content)
    assert "123,456" not in str(system.content)
    assert finding.example_ids == ("revenue-trends--q",)
    assert [e.detail for e in progress.events if e.stage == "examples"] == ["revenue-trends--q"]
