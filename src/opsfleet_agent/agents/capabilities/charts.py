from uuid import UUID

import anyio
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from opsfleet_agent.agents.capability import TurnContext
from opsfleet_agent.core.errors import NotFoundError, QueryRejectedError
from opsfleet_agent.entities.conversation import ProgressEvent
from opsfleet_agent.entities.reports import ChartKind, ChartSpec
from opsfleet_agent.ports.reports import ChartRenderer, ChartRepo
from opsfleet_agent.ports.results import ResultStore


class CreateChartArgs(BaseModel):
    result_id: str = Field(description="A stored result id returned by data_analyst.")
    kind: ChartKind
    x: str = Field(description="Result column for categories or time.")
    y: list[str] = Field(min_length=1, max_length=4, description="Numeric result columns.")
    title: str


class ChartCapability:
    name = "charts"
    prompt_hint = (
        "- `create_chart(result_id, kind, x, y, title)`: chart a stored result returned by "
        "`data_analyst` (use its exact column names). Use it when the user asks for a chart "
        "or when a report benefits from one."
    )

    def __init__(self, results: ResultStore, charts: ChartRepo, renderer: ChartRenderer) -> None:
        self._results = results
        self._charts = charts
        self._renderer = renderer

    def tools(self, turn: TurnContext) -> list[BaseTool]:
        async def create_chart(
            result_id: str, kind: ChartKind, x: str, y: list[str], title: str
        ) -> str:
            turn.progress.emit(ProgressEvent(stage="chart", detail=title))
            spec = ChartSpec(kind=kind, title=title, x=x, y=tuple(y))
            try:
                stored = await self._results.get(UUID(result_id), turn.session_id)
                png = await anyio.to_thread.run_sync(self._renderer.render, spec, stored.result)
            except (ValueError, NotFoundError, QueryRejectedError) as error:
                return f"ERROR: cannot chart {result_id}: {error}"
            chart = await self._charts.save(turn.session_id, title, png)
            turn.chart_paths.append(chart.path)
            turn.chart_ids.append(chart.id)
            return f"chart_id={chart.id} title='{chart.title}'"

        return [
            StructuredTool.from_function(
                coroutine=create_chart,
                name="create_chart",
                description="Render a chart from a stored analyst result.",
                args_schema=CreateChartArgs,
            )
        ]
