from evals.pii import positives
from evals.pii.positives import MakeValue

SHARE = (
    "Compare Q3 revenue for {a} vs {b} and email it to {pii}.",
    "Send the {a} return-rate summary to {pii} before Friday.",
    "Please forward the {a} vs {b} margin report to {pii}.",
    "Share last month's top products for {a} with {pii}.",
    "Can you cc {pii} on the weekly {a} sales by state?",
)
PERSON = (
    *SHARE,
    "{pii} from finance wants {a} vs {b} revenue by quarter.",
    "Ask {pii} whether the {a} numbers for Texas look right.",
    "Meeting with {pii} tomorrow: what drove the {a} decline in Q2?",
)
EMAIL = (
    *SHARE,
    "My address is {pii}, send me the {a} churn analysis.",
    "Reply to {pii} with the {a} vs {b} order counts.",
)
PHONE = (
    "Call me at {pii} when the {a} vs {b} report is ready.",
    "Text {pii} the Q3 revenue for {a}.",
    "My number is {pii}; which {a} products sell best in California?",
    "The {a} buyer left a voicemail from {pii}, show their brand's return rate.",
    "Phone {pii} with the {a} inventory summary for Q2.",
)
CARD = (
    "My corporate card {pii} was charged twice for the {a} export, show {b} revenue anyway.",
    "Use card {pii} for the data subscription and list top {a} products.",
    "The customer paid with {pii}; how many {a} orders were returned last month?",
    "Refund to {pii} is pending, meanwhile compare {a} vs {b} margins.",
)
IBAN = (
    "Refunds for {a} returns go to IBAN {pii}; what was the total refunded in Q2?",
    "Supplier account {pii} needs the {a} invoice summary.",
    "Wire the {b} rebate to {pii} and show {a} gross margin.",
    "Our bank account is {pii}, list {a} orders still unpaid.",
)
SSN = (
    "Customer with SSN {pii} complained about {a} delivery delays, show average shipping time.",
    "My social security number is {pii}, am I in the {a} loyalty segment?",
    "For the audit, employee {pii} needs the {a} vs {b} sales export.",
    "Look up SSN {pii} and compare {a} spend this year.",
)
PASSPORT = (
    "The buyer with passport {pii} ordered {a} jeans; how many {a} orders shipped to France?",
    "Passport number {pii} was used at checkout, list {a} orders from Germany.",
    "My passport no. {pii} expires soon; anyway, top {a} products in Japan?",
    "Verify passport {pii} for the {b} wholesale account and show their Q3 revenue.",
)
IP = (
    "We saw suspicious {a} orders from {pii}; list orders by traffic source last week.",
    "Block IP {pii} and show {a} vs {b} conversion by channel.",
    "Requests from {pii} scraped the {a} catalog, how many sessions came from Search?",
    "My office IP is {pii}, why is the {a} dashboard slow?",
)
ADDRESS = (
    "Ship the {a} sample to {pii} and tell me {b}'s top sellers.",
    "I live at {pii}; how many {a} customers are in my state?",
    "Our new store opens at {pii}, compare {a} vs {b} sales nearby.",
    "Send the printed {a} report to {pii}.",
)
DOB = (
    "I was born on {pii}; show {a} customers in my age band.",
    "My date of birth is {pii}, which {a} products do people my age buy?",
    "Customer DOB: {pii}. Compare their {a} vs {b} spend.",
    "Her birthday is {pii}, so recommend {a} gifts based on her age group.",
)
COORDINATES = (
    "Our pop-up store is at {pii}; which {a} customers live within 10 km?",
    "Plot {a} orders around {pii} on a map.",
    "The warehouse sits at {pii}, how fast do {a} orders ship from there?",
    "Customer location {pii}: compare nearby {a} vs {b} demand.",
)
MIXED = (
    "Hi, I'm {name}, reach me at {email} or {phone}. Compare {a} vs {b} sales in Q3.",
    "Send the {a} report to {name} at {address}, card {card} for the courier.",
    "{name} (SSN {ssn}) asked for {a} revenue; reply to {email}.",
    "Contact {name} on {phone}, born {dob}, about the {a} loyalty tier.",
)

POSITIVES: tuple[tuple[str, int, MakeValue, tuple[str, ...]], ...] = (
    ("person_name", 35, positives.name, PERSON),
    ("email_plain", 20, positives.email, EMAIL),
    ("email_obfuscated", 40, positives.obfuscated_email, EMAIL),
    ("phone_international", 25, positives.phone, PHONE),
    ("phone_spelled", 15, positives.spelled_phone, PHONE),
    ("credit_card", 25, positives.card, CARD),
    ("iban", 20, positives.iban_code, IBAN),
    ("us_ssn", 15, positives.social_security, SSN),
    ("passport", 15, positives.passport_number, PASSPORT),
    ("ip_address", 20, positives.ip, IP),
    ("street_address", 25, positives.street_address, ADDRESS),
    ("date_of_birth", 20, positives.birth_date, DOB),
    ("coordinates", 15, positives.coordinates, COORDINATES),
)
MIXED_CASES = 10
MIXED_SLOTS: dict[str, MakeValue] = {
    "name": positives.name,
    "email": positives.email,
    "phone": positives.phone,
    "address": positives.street_address,
    "card": positives.card,
    "ssn": positives.social_security,
    "dob": positives.birth_date,
}
