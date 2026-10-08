import io

import matplotlib as mpl

mpl.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.axes import Axes

from opsfleet_agent.core.errors import QueryRejectedError
from opsfleet_agent.entities.query import QueryResult
from opsfleet_agent.entities.reports import ChartKind, ChartSpec
from opsfleet_agent.ports.reports import ChartRenderer

_MAX_POINTS = 50


def _label(column: str) -> str:
    return column.replace("_", " ").title()


def _numeric(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise QueryRejectedError(f"chart value {value!r} is not numeric")
    return float(value)


class MatplotlibChartRenderer(ChartRenderer):
    def render(self, spec: ChartSpec, result: QueryResult) -> bytes:
        missing = {spec.x, *spec.y} - set(result.columns)
        if missing:
            raise QueryRejectedError(f"columns not in result: {sorted(missing)}")
        rows = result.rows[:_MAX_POINTS]
        labels = [str(row[result.columns.index(spec.x)]) for row in rows]
        figure, axes = plt.subplots(figsize=(9, 5), dpi=120)
        try:
            for index, column in enumerate(spec.y):
                values = [_numeric(row[result.columns.index(column)]) for row in rows]
                self._plot(
                    axes,
                    spec.kind,
                    labels=labels,
                    values=values,
                    name=_label(column),
                    index=index,
                    series=len(spec.y),
                )
            axes.set_title(spec.title)
            axes.grid(axis="y" if spec.kind is not ChartKind.HORIZONTAL_BAR else "x", alpha=0.3)
            if len(spec.y) > 1:
                axes.legend()
            if spec.kind is ChartKind.LINE:
                figure.autofmt_xdate()
            figure.tight_layout()
            buffer = io.BytesIO()
            figure.savefig(buffer, format="png")
            return buffer.getvalue()
        finally:
            plt.close(figure)

    @staticmethod
    def _plot(
        axes: Axes,
        kind: ChartKind,
        *,
        labels: list[str],
        values: list[float],
        name: str,
        index: int,
        series: int,
    ) -> None:
        if kind is ChartKind.LINE:
            axes.plot(labels, values, marker="o", label=name)
            return
        width = 0.8 / series
        positions = [position + index * width for position in range(len(labels))]
        if kind is ChartKind.HORIZONTAL_BAR:
            axes.barh(positions, values, height=width, label=name)
            axes.set_yticks([p + width * (series - 1) / 2 for p in range(len(labels))], labels)
            axes.invert_yaxis()
            return
        axes.bar(positions, values, width=width, label=name)
        axes.set_xticks([p + width * (series - 1) / 2 for p in range(len(labels))], labels)
        axes.tick_params(axis="x", rotation=30)
