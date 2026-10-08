import pytest

from opsfleet_agent.core.errors import PiiDetectedError
from opsfleet_agent.interactors.pii_scan import ensure_no_pii


@pytest.mark.parametrize(
    "text",
    [
        "Revenue was $1,234,567.89 in Q1, up 12.5% from 1,100,000.00.",
        "Top customer: Morgan Costa (user_5aa7c63e@example.net), spend $1,285.00.",
        "Region centroid -8.1, 45.3 (rounded).",
        "Order 102517 shipped on 2026-04-17.",
        "Q3 covers 2026-07-01 to 2026-09-30; report 78354742-f506-47c1-8756-33910e467e69.",
        "Revenue 1234567890 cents across 2026-07-01 - 2026-09-30.",
    ],
)
def test_masked_or_aggregate_text_passes(text: str) -> None:
    assert ensure_no_pii(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "Contact jane.doe@gmail.com for details.",
        "Located at 40.712776, -74.005974.",
        "Call +1 (415) 555-0132 now.",
    ],
)
def test_unmasked_personal_data_is_blocked(text: str) -> None:
    with pytest.raises(PiiDetectedError):
        ensure_no_pii(text)
