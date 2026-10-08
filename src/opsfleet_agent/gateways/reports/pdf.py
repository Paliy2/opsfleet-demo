from io import BytesIO
from pathlib import Path

import anyio
import markdown
import matplotlib
from fpdf import FPDF

from opsfleet_agent.entities.reports import Report
from opsfleet_agent.ports.reports import ReportRenderer

_FONT = "DejaVu"
_FONT_DIR = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
_FONT_FILES = {
    "": "DejaVuSans.ttf",
    "B": "DejaVuSans-Bold.ttf",
    "I": "DejaVuSans-Oblique.ttf",
    "BI": "DejaVuSans-BoldOblique.ttf",
}
_MARGIN_MM = 18
_CHART_WIDTH_MM = 170


def _document() -> FPDF:
    pdf = FPDF(format="A4")
    pdf.set_margins(_MARGIN_MM, _MARGIN_MM, _MARGIN_MM)
    pdf.set_auto_page_break(auto=True, margin=_MARGIN_MM)
    for style, file in _FONT_FILES.items():
        pdf.add_font(_FONT, style, str(_FONT_DIR / file))
    pdf.add_page()
    return pdf


def _render(report: Report, charts: tuple[bytes, ...], missing_charts: int) -> bytes:
    pdf = _document()
    pdf.set_font(_FONT, "B", 18)
    pdf.multi_cell(0, 9, report.title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(_FONT, "", 9)
    pdf.set_text_color(110, 110, 110)
    pdf.multi_cell(
        0,
        6,
        f"OpsFleet analyst report · {report.created_at:%Y-%m-%d %H:%M} UTC",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)
    pdf.set_font(_FONT, "", 11)
    html = markdown.markdown(report.body, extensions=["tables", "sane_lists"])
    pdf.write_html(html, font_family=_FONT)
    for chart in charts:
        pdf.ln(4)
        pdf.image(BytesIO(chart), x=_MARGIN_MM, w=_CHART_WIDTH_MM)
    if missing_charts:
        pdf.ln(4)
        pdf.set_font(_FONT, "I", 9)
        pdf.multi_cell(0, 6, f"{missing_charts} chart(s) could not be loaded.")
    return bytes(pdf.output())


class PdfReportRenderer(ReportRenderer):
    async def render(self, report: Report, charts: tuple[bytes, ...], missing_charts: int) -> bytes:
        return await anyio.to_thread.run_sync(_render, report, charts, missing_charts)
