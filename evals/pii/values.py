import re
import string
from random import Random

from pydantic import BaseModel, ConfigDict

from evals.pii.cases import InfoType, Sensitive

FIRST_NAMES = (
    "Marla",
    "Tobias",
    "Ingrid",
    "Desmond",
    "Priya",
    "Orla",
    "Bastian",
    "Lucinda",
    "Evander",
    "Henrietta",
    "Cormac",
    "Philippa",
    "Ambrose",
    "Seraphina",
    "Leopold",
    "Odette",
    "Thaddeus",
    "Clementine",
    "Rufus",
    "Wilhelmina",
    "Florian",
    "Imelda",
    "Barnaby",
    "Rosalind",
    "Augustin",
)
LAST_NAMES = (
    "Thistlewood",
    "Quillfeather",
    "Brambleton",
    "Marchbanks",
    "Fennimore",
    "Oakenshaw",
    "Pennywhistle",
    "Wrenfield",
    "Nettlebury",
    "Grimsdale",
    "Vantwerp",
    "Kestrelmoor",
    "Bellweather",
    "Saffronhill",
    "Mossgrove",
    "Tumblebrook",
    "Ravensworth",
    "Duskwood",
    "Larchmere",
    "Puddingfield",
    "Cobblestone",
    "Hazelburrow",
    "Inkwell",
    "Moonbridge",
)
STREETS = (
    "Larkspur",
    "Willowmere",
    "Juniper Hollow",
    "Quarry Bend",
    "Foxglove",
    "Silverbirch",
    "Marigold",
    "Copperleaf",
    "Heronsgate",
    "Bluebell",
    "Thornbury",
    "Wisteria",
)
STREET_SUFFIXES = ("Street", "Avenue", "Lane", "Road", "Drive", "Court", "Boulevard")
CITIES = (
    ("Springfield", "IL", "627"),
    ("Portland", "OR", "972"),
    ("Austin", "TX", "787"),
    ("Columbus", "OH", "432"),
    ("Madison", "WI", "537"),
    ("Boise", "ID", "837"),
    ("Tucson", "AZ", "857"),
    ("Raleigh", "NC", "276"),
)
EMAIL_DOMAINS = ("example.com", "example.org", "example.net")
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
DIGIT_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_US_AREAS = ("415", "212", "312", "617", "503", "720")
_PASSPORT_LETTERS = "CFGHJKLMNPRTVWXYZ"
AMEX_LENGTH = 15
COMPACT_IBAN_SHARE = 0.25
INVALID_SSN_AREA = 666
_DIGIT_GROUP = re.compile(r"\d{3,}")
_WORD = re.compile(r"\w+")


class Person(BaseModel):
    model_config = ConfigDict(frozen=True)

    first: str
    last: str

    @property
    def full(self) -> str:
        return f"{self.first} {self.last}"


def digits(rng: Random, count: int) -> str:
    return "".join(rng.choice(string.digits) for _ in range(count))


def person(rng: Random) -> Person:
    return Person(first=rng.choice(FIRST_NAMES), last=rng.choice(LAST_NAMES))


def item(value: str, info_type: InfoType, fragments: tuple[str, ...] = ()) -> Sensitive:
    return Sensitive(value=value, info_type=info_type, fragments=fragments or (value,))


def digit_fragments(value: str) -> tuple[str, ...]:
    return tuple(_DIGIT_GROUP.findall(value)) or (value,)


def tail_words(value: str, count: int) -> str:
    starts = [match.start() for match in _WORD.finditer(value)]
    return value[starts[-count] :] if len(starts) >= count else value


def luhn_check_digit(partial: str) -> int:
    total = 0
    for index, char in enumerate(reversed(partial)):
        total += sum(divmod(int(char) * (1 if index % 2 else 2), 10))
    return (10 - total % 10) % 10


def card_number(rng: Random, *, valid: bool) -> str:
    prefix = rng.choice(("4", "51", "53", "55", "6011", "37"))
    length = AMEX_LENGTH if prefix == "37" else 16
    body = prefix + digits(rng, length - 1 - len(prefix))
    check = luhn_check_digit(body)
    return body + str(check if valid else (check + rng.randint(1, 9)) % 10)


def format_card(rng: Random, number: str) -> str:
    separator = rng.choice((" ", "-", " ", ""))
    sizes = (4, 6, 5) if len(number) == AMEX_LENGTH else (4, 4, 4, 4)
    groups, start = [], 0
    for size in sizes:
        groups.append(number[start : start + size])
        start += size
    return separator.join(groups)


def _iban_numeric(text: str) -> int:
    return int("".join(str(int(char, 36)) for char in text))


def iban(rng: Random) -> str:
    country, bban = rng.choice(
        (
            ("DE", digits(rng, 18)),
            ("GB", "".join(rng.choices(string.ascii_uppercase, k=4)) + digits(rng, 14)),
            ("NL", "".join(rng.choices(string.ascii_uppercase, k=4)) + digits(rng, 10)),
            ("FR", digits(rng, 23)),
            ("ES", digits(rng, 20)),
        )
    )
    check = 98 - _iban_numeric(bban + country + "00") % 97
    return f"{country}{check:02d}{bban}"


def format_iban(rng: Random, compact: str) -> str:
    if rng.random() < COMPACT_IBAN_SHARE:
        return compact
    return " ".join(compact[start : start + 4] for start in range(0, len(compact), 4))


def ssn(rng: Random) -> str:
    area = rng.choice([value for value in range(101, 900) if value != INVALID_SSN_AREA])
    separator = rng.choice(("-", "-", " "))
    return f"{area:03d}{separator}{rng.randint(1, 99):02d}{separator}{rng.randint(1, 9999):04d}"


def passport(rng: Random) -> str:
    return rng.choice(
        (
            "5" + digits(rng, 8),
            digits(rng, 9),
            "C" + "".join(rng.choices(_PASSPORT_LETTERS + string.digits, k=8)),
            digits(rng, 2) + "".join(rng.choices(string.ascii_uppercase, k=2)) + digits(rng, 5),
        )
    )


def ip_address(rng: Random) -> str:
    return rng.choice(
        (
            f"192.0.2.{rng.randint(1, 254)}",
            f"198.51.100.{rng.randint(1, 254)}",
            f"203.0.113.{rng.randint(1, 254)}",
            f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}",
            f"2001:db8:{rng.randint(1, 0xFFFF):x}::{rng.randint(1, 0xFFFF):x}",
        )
    )
