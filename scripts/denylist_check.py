import re
import sys
from pathlib import Path

DENYLIST = Path(".denylist")


def load_patterns() -> list[re.Pattern[str]]:
    if not DENYLIST.exists():
        return []
    terms = [line.strip() for line in DENYLIST.read_text().splitlines() if line.strip()]
    return [re.compile(re.escape(term), re.IGNORECASE) for term in terms]


def find_hits(paths: list[str], patterns: list[re.Pattern[str]]) -> list[str]:
    hits = []
    for path in paths:
        try:
            text = Path(path).read_text()
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits.extend(f"{path}: {p.pattern}" for p in patterns if p.search(text))
    return hits


def main() -> int:
    hits = find_hits(sys.argv[1:], load_patterns())
    for hit in hits:
        print(f"denylisted term found in {hit}")  # noqa: T201
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
