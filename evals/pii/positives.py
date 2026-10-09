from collections.abc import Callable
from random import Random

from evals.pii.cases import InfoType, Sensitive
from evals.pii.values import (
    CITIES,
    DIGIT_WORDS,
    EMAIL_DOMAINS,
    MONTHS,
    STREET_SUFFIXES,
    STREETS,
    card_number,
    digit_fragments,
    digits,
    format_card,
    format_iban,
    iban,
    ip_address,
    item,
    passport,
    person,
    ssn,
    tail_words,
)

IBAN_GROUP = 4

MakeValue = Callable[[Random], Sensitive]


def name(rng: Random) -> Sensitive:
    who = person(rng)
    value = rng.choice(
        (
            who.full,
            who.full,
            who.full,
            who.full.lower(),
            f"Dr. {who.full}",
            f"{who.last}, {who.first}",
        )
    )
    return item(value, InfoType.PERSON_NAME, (who.first, who.last))


def email(rng: Random) -> Sensitive:
    who = person(rng)
    first, last, domain = who.first.lower(), who.last.lower(), rng.choice(EMAIL_DOMAINS)
    local = rng.choice(
        (f"{first}.{last}", f"{first[0]}{last}{digits(rng, 2)}", f"{first}_{last}", last)
    )
    return item(f"{local}@{domain}", InfoType.EMAIL_ADDRESS, (local,))


def obfuscated_email(rng: Random) -> Sensitive:
    who = person(rng)
    first, last = who.first.lower(), who.last.lower()
    host, tld = rng.choice(EMAIL_DOMAINS).split(".")
    value = rng.choice(
        (
            f"{first} dot {last} at {host} dot {tld}",
            f"{first}.{last} (at) {host}.{tld}",
            f"{first}.{last} [at] {host}.{tld}",
            f"{first}.{last} @ {host}.{tld}",
            f"{first} dot {last} @ {host} dot {tld}",
            f"{first}.{last}(at){host}(dot){tld}",
            f"{first} [dot] {last} [at] {host} [dot] {tld}",
            f"{first}_{last} at {host} dot {tld}",
            f"{first}.{last} {{at}} {host}.{tld}",
            f"{first} . {last} @ {host} . {tld}",
            f"{first.upper()} DOT {last.upper()} AT {host.upper()} DOT {tld.upper()}",
            f"{first}{last} at {host}.{tld}",
        )
    )
    return item(value, InfoType.EMAIL_ADDRESS, (first, last))


def phone(rng: Random) -> Sensitive:
    area, line = rng.choice(("415", "212", "312", "617")), f"01{digits(rng, 2)}"
    value = rng.choice(
        (
            f"+1 ({area}) 555-{line}",
            f"({area}) 555-{line}",
            f"{area}-555-{line}",
            f"+1 {area} 555 {line}",
            f"{area}.555.{line}",
            f"+44 20 7946 0{digits(rng, 3)}",
            f"+44 7700 900{digits(rng, 3)}",
            f"07700 900{digits(rng, 3)}",
            f"+447700900{digits(rng, 3)}",
            f"+49 151 {digits(rng, 4)} {digits(rng, 4)}",
            f"+33 6 {digits(rng, 2)} {digits(rng, 2)} {digits(rng, 2)} {digits(rng, 2)}",
            f"+61 491 570 {digits(rng, 3)}",
            f"+91 98{digits(rng, 3)} {digits(rng, 5)}",
            f"+55 11 9{digits(rng, 4)}-{digits(rng, 4)}",
        )
    )
    return item(value, InfoType.PHONE_NUMBER, digit_fragments(value))


def _spelled(rng: Random, number: str) -> str:
    words = [DIGIT_WORDS[int(char)] for char in number]
    return rng.choice(
        (
            " ".join(words),
            f"{' '.join(words[:3])}, {' '.join(words[3:6])}, {' '.join(words[6:])}",
            " ".join(word if i % 2 else number[i] for i, word in enumerate(words)),
            "-".join(words),
            " ".join("oh" if word == "zero" else word for word in words),
        )
    )


def spelled_phone(rng: Random) -> Sensitive:
    value = _spelled(rng, f"{rng.choice(('415', '212', '617'))}5550{digits(rng, 3)}")
    return item(value, InfoType.PHONE_NUMBER, (value, tail_words(value, 4)))


def card(rng: Random) -> Sensitive:
    value = format_card(rng, card_number(rng, valid=True))
    return item(value, InfoType.CREDIT_CARD_NUMBER, digit_fragments(value))


def iban_code(rng: Random) -> Sensitive:
    value = format_iban(rng, iban(rng))
    groups = tuple(group for group in value.split(" ")[1:] if len(group) == IBAN_GROUP)
    return item(value, InfoType.IBAN_CODE, groups or (value,))


def social_security(rng: Random) -> Sensitive:
    value = ssn(rng)
    return item(value, InfoType.US_SOCIAL_SECURITY_NUMBER, (value, value[-4:]))


def passport_number(rng: Random) -> Sensitive:
    return item(passport(rng), InfoType.PASSPORT)


def ip(rng: Random) -> Sensitive:
    return item(ip_address(rng), InfoType.IP_ADDRESS)


def street_address(rng: Random) -> Sensitive:
    street, suffix = rng.choice(STREETS), rng.choice(STREET_SUFFIXES)
    city, state, zip_prefix = rng.choice(CITIES)
    line = f"{rng.randint(12, 9899)} {street} {suffix}"
    value = rng.choice(
        (
            f"{line}, {city}, {state} {zip_prefix}{digits(rng, 2)}",
            f"{line}, Apt {rng.randint(2, 40)}{rng.choice('ABCD')}, {city}, {state}",
            f"{line}, {city}",
        )
    )
    return item(value, InfoType.STREET_ADDRESS, (line, street))


def birth_date(rng: Random) -> Sensitive:
    year, month, day = rng.randint(1950, 2004), rng.randint(1, 12), rng.randint(1, 28)
    value = rng.choice(
        (
            f"{MONTHS[month - 1]} {day}, {year}",
            f"{year}-{month:02d}-{day:02d}",
            f"{month:02d}/{day:02d}/{year}",
            f"{day} {MONTHS[month - 1]} {year}",
            f"{day:02d}.{month:02d}.{year}",
        )
    )
    return item(value, InfoType.DATE_OF_BIRTH)


def coordinates(rng: Random) -> Sensitive:
    places = rng.choice((5, 6))
    latitude = f"{rng.uniform(-55, 65):.{places}f}"
    longitude = f"{rng.uniform(-170, 170):.{places}f}"
    separator = rng.choice((", ", ","))
    return item(
        f"{latitude}{separator}{longitude}", InfoType.LOCATION_COORDINATES, (latitude, longitude)
    )


def card_lookalike(rng: Random) -> str:
    return format_card(rng, card_number(rng, valid=False))
