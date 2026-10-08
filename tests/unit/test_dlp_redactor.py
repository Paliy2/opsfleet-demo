import pytest
from google.api_core.exceptions import ServiceUnavailable
from google.cloud import dlp_v2

from opsfleet_agent.core.errors import PiiServiceUnavailableError
from opsfleet_agent.gateways.dlp.redactor import DlpRedactor


def finding(info_type: str, text: str, quote: str) -> dlp_v2.Finding:
    start = text.index(quote)
    return dlp_v2.Finding(
        info_type=dlp_v2.InfoType(name=info_type),
        location=dlp_v2.Location(codepoint_range=dlp_v2.Range(start=start, end=start + len(quote))),
    )


class FakeDlp:
    def __init__(self, quotes: tuple[tuple[str, str], ...] = (), fail: bool = False) -> None:
        self.quotes = quotes
        self.fail = fail
        self.requests: list[dlp_v2.InspectContentRequest] = []

    async def inspect_content(
        self, request: dlp_v2.InspectContentRequest
    ) -> dlp_v2.InspectContentResponse:
        self.requests.append(request)
        if self.fail:
            raise ServiceUnavailable("dlp down")
        text = request.item.value
        findings = [finding(kind, text, quote) for kind, quote in self.quotes if quote in text]
        return dlp_v2.InspectContentResponse(result=dlp_v2.InspectResult(findings=findings))


def redactor(client: FakeDlp, loads: list[int]) -> DlpRedactor:
    async def brands() -> tuple[str, ...]:
        loads.append(1)
        return ("Ralph Lauren", "Calvin Klein")

    return DlpRedactor(
        client, project="p-123456", location="global", allowed_terms=brands, timeout_seconds=5
    )


async def test_replaces_findings_with_info_types() -> None:
    client = FakeDlp((("PERSON_NAME", "Eugene Domer"), ("STREET_ADDRESS", "1 Main St")))

    redaction = await redactor(client, list[int]()).redact("Eugene Domer lives at 1 Main St")

    assert redaction.text == "[PERSON_NAME] lives at [STREET_ADDRESS]"
    assert redaction.findings == ("PERSON_NAME", "STREET_ADDRESS")
    assert client.requests[0].parent == "projects/p-123456/locations/global"


async def test_names_inside_brands_and_pseudonyms_are_kept() -> None:
    client = FakeDlp(
        (("PERSON_NAME", "Ralph"), ("PERSON_NAME", "Lauren"), ("PERSON_NAME", "Morgan Costa"))
    )
    loads = list[int]()
    dlp = redactor(client, loads)
    text = "Compare ralph lauren buyers like Morgan Costa"

    first = await dlp.redact(text)
    await dlp.redact(text)

    assert first.text == text
    assert first.findings == ()
    assert loads == [1]


async def test_name_that_only_overlaps_a_brand_is_still_redacted() -> None:
    client = FakeDlp((("PERSON_NAME", "Calvin Smith"),))

    redaction = await redactor(client, list[int]()).redact("Calvin Smith bought Calvin Klein")

    assert redaction.text == "[PERSON_NAME] bought Calvin Klein"


async def test_outage_fails_closed() -> None:
    with pytest.raises(PiiServiceUnavailableError):
        await redactor(FakeDlp(fail=True), list[int]()).redact("Eugene Domer")


async def test_blank_text_skips_the_service() -> None:
    client = FakeDlp()

    await redactor(client, list[int]()).redact("  ")

    assert client.requests == []
