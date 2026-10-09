import json
from pathlib import Path

from pydantic import TypeAdapter

from opsfleet_agent.core.config import DlpSettings
from opsfleet_agent.gateways.bigquery.catalog import create_catalog

DATASETS = Path(__file__).parent.parent / "datasets"
BRAND_SNAPSHOT = DATASETS / "pii_brands.json"
_BRANDS = TypeAdapter(tuple[str, ...])


def read_snapshot(path: Path = BRAND_SNAPSHOT) -> tuple[str, ...]:
    return _BRANDS.validate_json(path.read_text(encoding="utf-8"))


async def fetch_brands(settings: DlpSettings) -> tuple[str, ...]:
    catalog = create_catalog(settings.gcp_project, settings.bq_source_dataset)
    return tuple(brand.brand for brand in await catalog.brands_by_revenue())


def write_snapshot(brands: tuple[str, ...], path: Path = BRAND_SNAPSHOT) -> int:
    unique = sorted(set(brands))
    path.write_text(json.dumps(unique, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    return len(unique)
