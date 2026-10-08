from collections.abc import Sequence
from pathlib import Path

from opsfleet_agent.entities.golden import GoldenTrio


def read_trios(path: Path) -> tuple[GoldenTrio, ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return tuple(GoldenTrio.model_validate_json(line) for line in lines if line.strip())


def write_trios(path: Path, trios: Sequence[GoldenTrio]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(trios, key=lambda trio: trio.id)
    path.write_text("".join(f"{trio.model_dump_json()}\n" for trio in ordered), encoding="utf-8")
