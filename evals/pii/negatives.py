import string
from collections.abc import Callable
from random import Random

from evals.pii.cases import Keep, Preserved
from evals.pii.positives import card_lookalike
from evals.pii.values import MONTHS, digits
from opsfleet_agent.entities.privacy import PSEUDONYM_FIRST_NAMES, PSEUDONYM_LAST_NAMES

PERSON_LIKE_BRANDS = (
    "Calvin Klein",
    "Ralph Lauren",
    "Tommy Hilfiger",
    "Michael Kors",
    "Kenneth Cole",
    "Perry Ellis",
    "Anne Klein",
    "Jessica Simpson",
    "Betsey Johnson",
    "Nicole Miller",
    "Steve Madden",
    "Ted Baker",
    "Ben Sherman",
    "Robert Graham",
    "Tom Ford",
    "John Varvatos",
    "Vince Camuto",
    "Kate Spade",
    "Tory Burch",
    "Cole Haan",
    "Joseph Abboud",
    "Paul Fredrick",
    "Eddie Bauer",
    "Geoffrey Beene",
    "Karen Kane",
    "Rebecca Minkoff",
    "Trina Turk",
    "Tracy Reese",
    "Elie Tahari",
    "Alfred Dunner",
    "Christopher Fischer",
    "Evan Picone",
    "Adrianna Papell",
    "Lilly Pulitzer",
    "Rebecca Taylor",
    "Sean John",
    "Fred Perry",
    "Larry Levine",
    "Louis Raphael",
    "Carole Hochman",
    "Helly Hansen",
    "Austin Reed",
)
OTHER_BRANDS = ("Levi's", "Diesel", "Carhartt", "Columbia", "Hurley", "Dockers", "Nautica")
PRODUCTS = (
    "Levi's Men's 505 Regular Fit Jean",
    "Calvin Klein Women's Wool Blend Coat",
    "Tommy Hilfiger Classic Fit Polo",
    "Carhartt Duck Active Jacket J130",
    "Ralph Lauren Big Pony Mesh Polo",
    "Michael Kors Hamilton Tote",
    "Diesel Sleenker Skinny Jeans",
    "Columbia Bugaboo Interchange Jacket",
    "Kenneth Cole Reaction Slim Fit Suit",
    "Steve Madden Troopa Combat Boot",
    "Ted Baker Endurance Blazer",
    "Ben Sherman Mod Fit Oxford Shirt",
    "Eddie Bauer Cirruslite Down Jacket",
    "Helly Hansen Crew Midlayer Jacket",
    "Fred Perry Twin Tipped Polo",
)
LOCATIONS = (
    "New York",
    "Los Angeles",
    "Springfield, Illinois",
    "Portland, Oregon",
    "Texas",
    "California",
    "São Paulo",
    "Rotterdam",
    "Manchester",
    "Shanghai",
    "Brisbane",
    "Ontario",
    "Bavaria",
    "Florida",
    "Seoul",
)

LOWERCASE_SHARE = 0.15
ONE_TYPO_CHARS = 10

Negative = tuple[str, tuple[Preserved, ...]]
MakeNegative = Callable[[Random], Negative]


def _keep(kind: Keep, *values: str) -> tuple[Preserved, ...]:
    return tuple(Preserved(value=value, kind=kind) for value in values)


def _pair(rng: Random, pool: tuple[str, ...]) -> tuple[str, str]:
    first, second = rng.sample(pool, 2)
    return first, second


def brand_person(rng: Random) -> Negative:
    a, b = _pair(rng, PERSON_LIKE_BRANDS)
    if rng.random() < LOWERCASE_SHARE:
        a, b = a.lower(), b.lower()
    template = rng.choice(
        (
            "Compare Q3 revenue for {a} vs {b}.",
            "How did {a} perform against {b} in Texas last quarter?",
            "Show the top 5 {a} products by margin, then the same for {b}.",
            "Why did {a} returns spike in March while {b} stayed flat?",
            "Which customers bought both {a} and {b} this year?",
            "Delete my reports that mention {a} but keep the {b} ones.",
            "Is {a} or {b} growing faster among women aged 25-34?",
        )
    )
    return template.format(a=a, b=b), _keep(Keep.BRAND, a, b)


def _typo(rng: Random, brand: str, edits: int) -> str:
    chars = list(brand)
    positions = [i for i, c in enumerate(chars) if c.isalpha() and i and chars[i - 1] != " "]
    for position in rng.sample(positions, edits):
        replacement = rng.choice([c for c in string.ascii_lowercase if c != chars[position]])
        chars[position] = replacement.upper() if chars[position].isupper() else replacement
    return "".join(chars)


def misspelled_brand(rng: Random) -> Negative:
    brand = rng.choice(PERSON_LIKE_BRANDS)
    limit = 2 if len(brand) > ONE_TYPO_CHARS else 1
    typo = _typo(rng, brand, rng.randint(1, limit))
    template = rng.choice(
        (
            "Delete all reports mentioning {t}.",
            "What was {t} revenue last year?",
            "Find my saved report about {t} returns.",
            "Compare {t} with Diesel for Q2.",
            "Top selling {t} items in Florida?",
        )
    )
    return template.format(t=typo), _keep(Keep.MISSPELLED_BRAND, typo)


def pseudonym(rng: Random) -> Negative:
    name = f"{rng.choice(PSEUDONYM_FIRST_NAMES)} {rng.choice(PSEUDONYM_LAST_NAMES)}"
    masked = f"user_{''.join(rng.choices('0123456789abcdef', k=8))}@example.com"
    user_id = "".join(rng.choices("0123456789abcdef", k=16))
    text, value = rng.choice(
        (
            (f"Show all orders for customer {name}.", name),
            (f"What did {name} buy from Levi's last year?", name),
            (f"Is {name} one of our top 10 Diesel customers?", name),
            (f"Show the order history for {masked}.", masked),
            (f"Why did {masked} return three items?", masked),
            (f"How many orders has customer {user_id} placed?", user_id),
        )
    )
    return text, _keep(Keep.PSEUDONYM, value)


def product(rng: Random) -> Negative:
    name = rng.choice(PRODUCTS)
    template = rng.choice(
        (
            "How many units of {p} sold in Q4?",
            "What is the return rate for the {p}?",
            "Show monthly revenue for {p} since launch.",
        )
    )
    return template.format(p=name), _keep(Keep.PRODUCT, name)


def money_year(rng: Random) -> Negative:
    year = rng.randint(2019, 2025)
    amount = f"${rng.randint(1000, 9_999_999):,}.{rng.randint(0, 99):02d}"
    other = f"{rng.randint(10, 999)}.{rng.randint(0, 99):02d}"
    template = rng.choice(
        (
            "Revenue was {m} in {y}; did Levi's beat that the year after?",
            "Which brands made more than {m} in {y}?",
            "Average order value fell to {o} in {y}, why?",
        )
    )
    text = template.format(m=amount, y=year, o=other)
    values = [v for v in (amount, str(year), other) if v in text]
    return text, _keep(Keep.NUMBER, *values)


def order_id(rng: Random) -> Negative:
    value = rng.choice(
        (
            f"order {rng.randint(1, 125_000)}",
            f"order #{rng.randint(10_000_000, 99_999_999)}",
            f"inventory item {rng.randint(100_000, 499_999)}",
            f"SKU {''.join(rng.choices(string.ascii_uppercase, k=3))}-{digits(rng, 6)}",
            f"product id {rng.randint(1, 29_120)}",
            f"order ids {rng.randint(1000, 9999)}, {rng.randint(1000, 9999)}",
        )
    )
    template = rng.choice(
        (
            "Why was {v} returned?",
            "Show the status of {v} and similar Diesel orders.",
            "When did {v} ship?",
        )
    )
    return template.format(v=value), _keep(Keep.ORDER_ID, value)


def card_like(rng: Random) -> Negative:
    value = card_lookalike(rng)
    template = rng.choice(
        (
            "Tracking number {v} shows the Levi's parcel lost, how many parcels were lost in May?",
            "Gift card batch {v} was issued to Diesel buyers; what did they spend?",
            "Shipment {v} contains Carhartt stock, how much revenue is that?",
            "Promo code {v} was used how many times on Columbia items?",
        )
    )
    return template.format(v=value), _keep(Keep.NUMBER, value)


def percentage(rng: Random) -> Negative:
    share, growth = f"{rng.randint(1, 99)}.{rng.randint(0, 9)}%", f"{rng.randint(1, 60)}%"
    text = f"Levi's had a {share} return rate and {growth} growth; is Diesel above that?"
    return text, _keep(Keep.NUMBER, share, growth)


def calendar_date(rng: Random) -> Negative:
    year, month, day = rng.randint(2019, 2025), rng.randint(1, 12), rng.randint(1, 28)
    value = rng.choice(
        (
            f"{year}-{month:02d}-{day:02d}",
            f"{MONTHS[month - 1]} {day}, {year}",
            f"{month:02d}/{day:02d}/{year}",
            f"{day} {MONTHS[month - 1]} {year}",
        )
    )
    template = rng.choice(
        (
            "Show Diesel orders placed since {d}.",
            "How many Levi's items were delivered on {d}?",
            "Compare sales before and after the {d} price change.",
            "Revenue for the week starting {d}, by brand.",
        )
    )
    return template.format(d=value), _keep(Keep.DATE, value)


def location(rng: Random) -> Negative:
    a, b = _pair(rng, LOCATIONS)
    template = rng.choice(
        (
            "Compare Levi's sales in {a} vs {b}.",
            "Which brands grew fastest in {a} compared to {b}?",
            "How many customers from {a} bought Diesel, versus {b}?",
        )
    )
    return template.format(a=a, b=b), _keep(Keep.LOCATION, a, b)


NEGATIVES: tuple[tuple[str, int, MakeNegative], ...] = (
    ("brand_person_like", 35, brand_person),
    ("brand_misspelled", 25, misspelled_brand),
    ("pseudonym", 25, pseudonym),
    ("product_name", 15, product),
    ("money_year", 15, money_year),
    ("order_id", 20, order_id),
    ("card_lookalike", 20, card_like),
    ("percentage", 10, percentage),
    ("non_birth_date", 20, calendar_date),
    ("location", 15, location),
)
