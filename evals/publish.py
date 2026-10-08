from collections.abc import Sequence

from langfuse import Langfuse
from pydantic import BaseModel, ConfigDict

from evals.cases import CaseResult, Suite

_DATASETS = {
    Suite.SQL: "opsfleet-golden-sql",
    Suite.REDTEAM: "opsfleet-redteam",
    Suite.CAPABILITY: "opsfleet-capability",
}
_SCORE_PREFIXES = {
    Suite.SQL: "eval",
    Suite.REDTEAM: "eval",
    Suite.CAPABILITY: "eval.capability",
}


class DatasetItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    suite: Suite
    case_id: str
    input: dict[str, object]
    expected: dict[str, object]

    @property
    def item_id(self) -> str:
        return f"{self.suite}-{self.case_id}"


class LangfusePublisher:
    def __init__(self, client: Langfuse, run_name: str, metadata: dict[str, str]) -> None:
        self._client = client
        self._run_name = run_name
        self._metadata = metadata

    def publish(self, items: Sequence[DatasetItem], results: Sequence[CaseResult]) -> None:
        for suite in {item.suite for item in items}:
            self._client.create_dataset(name=_DATASETS[suite])
        by_id = {item.item_id: item for item in items}
        for result in results:
            item = by_id[f"{result.suite}-{result.case_id}"]
            self._client.create_dataset_item(
                dataset_name=_DATASETS[item.suite],
                id=item.item_id,
                input=item.input,
                expected_output=item.expected,
                metadata={"category": result.category},
            )
            self._record(item.item_id, result)
        self._client.flush()

    def _record(self, item_id: str, result: CaseResult) -> None:
        for trace_id in result.trace_ids:
            self._client.api.dataset_run_items.create(
                run_name=self._run_name,
                run_description="opsfleet eval harness",
                dataset_item_id=item_id,
                trace_id=trace_id,
                metadata=self._metadata,
            )
        if not result.trace_ids:
            return
        trace_id = result.trace_ids[-1]
        prefix = _SCORE_PREFIXES[result.suite]
        comment = "\n".join(result.notes) or None
        self._client.create_score(
            trace_id=trace_id,
            name=f"{prefix}.passed",
            value=1.0 if result.passed else 0.0,
            data_type="BOOLEAN",
            comment=comment,
            metadata={"run": self._run_name},
        )
        for name, value in result.scores.items():
            self._client.create_score(
                trace_id=trace_id,
                name=f"{prefix}.{name}",
                value=value,
                data_type="NUMERIC",
                metadata={"run": self._run_name},
            )
