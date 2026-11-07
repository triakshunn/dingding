"""Layer 1: deterministic detectors. Each returns (kind, exact_value) pairs found in text."""

import re

from . import config

# Order matters only for overlap resolution: longer matches win anyway.
_PATTERNS = [
    # US social security number
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), 0),
    # Full card numbers 13-19 digits, optionally grouped (checked with Luhn below)
    ("CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), 0),
    # Masked / partial account or card numbers: "ending 4821", "ending in 4821", "ends in 4821", "****4821", "x4821"
    (
        "ACCOUNT",
        re.compile(
            r"(?i)\b(?:ending(?:\s+in)?|ends\s+in|last\s+4(?:\s+digits)?(?:\s+of)?)\s*[:#]?\s*(\d{4})\b"
        ),
        1,
    ),
    ("ACCOUNT", re.compile(r"(?:[*xX•]{2,}|\b[xX])[-\s]?(\d{4})\b"), 1),
    # "account number 123456789", "acct # 12345678", "routing number 072000326"
    (
        "ACCOUNT",
        re.compile(
            r"(?i)\b(?:account|acct|a/c|routing|member)\s*(?:no\.?|number|num|#)?\s*[:#]?\s*(\d[\d -]{4,20}\d)\b"
        ),
        1,
    ),
    # Verification / security codes near a keyword
    (
        "CODE",
        re.compile(
            r"(?i)\b(?:code|otp|passcode|pin|verification|security code)\b[^\d\n]{0,25}(\d{4,8})\b"
        ),
        1,
    ),
    (
        "CODE",
        re.compile(
            r"(?i)\b(\d{4,8})\s+is\s+your\s+(?:\w+\s+){0,2}(?:code|otp|passcode|pin)\b"
        ),
        1,
    ),
    # Date of birth
    (
        "DOB",
        re.compile(
            r"(?i)\b(?:dob|d\.o\.b\.?|date of birth|born on|birth ?date)\s*[:\-]?\s*"
            r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2,8}\.? \d{1,2},? \d{4})"
        ),
        1,
    ),
    # Passport-like IDs: 1-2 letters + 6-8 digits (e.g. Z1234567)
    ("PASSPORT", re.compile(r"\b[A-Z]{1,2}\d{6,8}\b"), 0),
    # UMID / student / employee ids: 8 digits, or any digits after an ID keyword
    (
        "ID",
        re.compile(
            r"(?i)\b(?:umid|student id|employee id|emplid|uniqname id|id number|id no\.?|id#)\s*[:#]?\s*([A-Z0-9]{5,12})\b"
        ),
        1,
    ),
    # Bare 8-digit numbers (UMID-like). Context-checked in find_pii(): kept visible when clearly an
    # order/tracking/confirmation/invoice/booking number, hidden otherwise (safer default).
    ("ID8", re.compile(r"(?<![\d$.,/:-])\b\d{8}\b(?![\d.,/:-])"), 0),
    # Phone numbers: international with +, then US formats
    # International "+" numbers of any grouping (+65 9123 4567, +91-9876543210, +44 20 7946 0958);
    # digit count is checked in find_pii() (8-15 digits)
    ("PHONE+", re.compile(r"\+\d{1,3}(?:[\s.-]?\(?\d{1,5}\)?){1,5}\b"), 0),
    (
        "PHONE",
        re.compile(
            r"(?<![\d-])(?:1[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"
        ),
        0,
    ),
    (
        "PHONE",
        re.compile(r"(?<![\d-])\b\d{10}\b(?!\d)"),
        0,
    ),  # incl. Indian mobiles (6-9xxxxxxxxx)
    # Indian mobile grouped 5+5 without +91: "98765 43210"
    ("PHONE", re.compile(r"(?<![\d-])\b[6-9]\d{4}[\s-]\d{5}\b(?![\d-])"), 0),
    # Local numbers next to a phone word: US 7-digit "555-0100", Singapore 8-digit "9123 4567"
    (
        "PHONE",
        re.compile(
            r"(?i)\b(?:call|text|phone|ph|cell|mobile|mob|tel|hp|reach|whatsapp|contact|sms)\b[^\d\n]{0,25}"
            r"((?:[689]\d{3}[\s-]?\d{4})|(?:\d{3}[\s.-]\d{4}))\b(?![\d-])"
        ),
        1,
    ),
    # Street addresses (best effort): number + 1-4 capitalised words + street suffix (+ optional unit)
    (
        "ADDRESS",
        re.compile(
            r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][a-zA-Z]+\.?\s+){1,4}"
            r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Ct|Court|Way|Pl|Place|Pkwy|Parkway|"
            r"Hwy|Highway|Cir|Circle|Ter|Terrace|Trl|Trail)\b\.?"
            r"(?:,?\s+(?:Apt|Apartment|Unit|Suite|Ste|#)\.?\s*[A-Za-z0-9-]+)?"
        ),
        0,
    ),
    # Singapore HDB/condo: "Blk 123 Ang Mo Kio Ave 3 #05-12" (+ optional "Singapore 560123")
    (
        "ADDRESS",
        re.compile(
            r"(?i)\b(?:Blk|Block)\s+\d{1,4}[A-Za-z]?\s+[A-Z][\w']*(?:\s+[A-Za-z0-9][\w']*){0,6}?"
            r"(?:\s*,?\s*#\d{1,3}-\d{1,5})?(?:,?\s+Singapore\s+\d{6})?(?=[,.\n]|$|\s{2})"
        ),
        0,
    ),
    (
        "ADDRESS",
        re.compile(r"#\d{1,3}-\d{1,5}\b"),
        0,
    ),  # SG unit number on its own
    ("POSTAL", re.compile(r"(?i)\bSingapore\s*(\d{6})\b"), 1),  # SG postal code
    # India: PIN code after a city/state name; house/flat/plot lines
    (
        "POSTAL",
        re.compile(
            r"(?i)\b(?:Mumbai|New Delhi|Delhi|Bengaluru|Bangalore|Chennai|Kolkata|Hyderabad|Pune|"
            r"Ahmedabad|Jaipur|Lucknow|Surat|Noida|Gurgaon|Gurugram|Chandigarh|Indore|Bhopal|Nagpur|"
            r"Kochi|Coimbatore|Vadodara|Thane|Navi Mumbai|Maharashtra|Karnataka|Gujarat|Tamil Nadu|"
            r"Kerala|Telangana|Uttar Pradesh|Rajasthan|Haryana|Punjab|West Bengal|Madhya Pradesh|"
            r"Andhra Pradesh|Bihar|Odisha|Goa|India)\b[,\s-]*(\d{3}\s?\d{3})\b"
        ),
        1,
    ),
    (
        "ADDRESS",
        re.compile(
            r"(?i)\b(?:Flat|House\s+No\.?|H\.\s?No\.?|Plot\s+No\.?|Door\s+No\.?|Shop\s+No\.?)\s*[:#]?\s*"
            r"[\w/-]{1,8}(?:,\s*[^,\n]{2,40}){0,2}"
        ),
        0,
    ),
]

_NOT_ID_CONTEXT = re.compile(
    r"(?i)(order|tracking|track|confirmation|conf\.?|invoice|booking|reservation|receipt|"
    r"ticket|ref(?:erence)?|case|flight|pnr|item|sku|model)\b[^\n]{0,20}$"
)
_ID_CONTEXT = re.compile(
    r"(?i)(umid|student|employee|member|id|emplid|uniqname|account|acct)\b[^\n]{0,20}$"
)


CARD_DIGITS = range(13, 20)  # payment card numbers have 13-19 digits
PHONE_DIGITS = range(8, 16)  # international numbers: 8-15 digits (E.164)
MIN_VALUE_LEN = 4  # shorter matches are too ambiguous to hide


def _luhn_ok(digits: str) -> bool:
    """Returns whether a card number passes the Luhn checksum."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:  # every second digit from the right is doubled
            d = sum(divmod(d * 2, 10))  # e.g. 7 -> 14 -> 1 + 4 = 5
        total += d
    return total % 10 == 0


def find_pii(text: str) -> list[tuple[str, str]]:
    """All (kind, value) pairs, deduplicated, values as they appear in the text."""
    found, spans = [], []
    for pattern_kind, rx, group in _PATTERNS:
        for m in rx.finditer(text):
            kind = pattern_kind  # per match: the checks below may refine it
            value = m.group(group).strip()
            start, end = m.span(group)
            if kind == "CARD":
                digits = re.sub(r"\D", "", value)
                if not (len(digits) in CARD_DIGITS and _luhn_ok(digits)):
                    continue
            if len(value) < MIN_VALUE_LEN:
                continue
            if kind == "PHONE+":
                if len(re.sub(r"\D", "", value)) not in PHONE_DIGITS:
                    continue
                kind = "PHONE"
            if kind == "ID8":
                before = text[max(0, start - 40) : start]
                if _NOT_ID_CONTEXT.search(before) and not _ID_CONTEXT.search(
                    before
                ):
                    continue  # e.g. "Order #12345678" stays visible
                kind = "ID"
            # skip if fully inside an already-found span (e.g. 8 digits inside a card number)
            if any(s <= start and end <= e for s, e in spans):
                continue
            spans.append((start, end))
            found.append((kind, value))
    seen, out = set(), []
    for kind, value in found:
        if value not in seen:
            seen.add(value)
            out.append((kind, value))
    return out


def should_drop(subject: str, body: str) -> bool:
    text = f"{subject}\n{body[:800]}".lower()
    return any(re.search(p, text) for p in config.DROP_PATTERNS)


def sender_sensitive(sender: str) -> bool:
    s = (sender or "").lower()
    return any(x in s for x in config.SENSITIVE_SENDERS)


_KW = [re.compile(k, re.I) for k in config.SENSITIVE_KEYWORDS]
_FOOTER = re.compile(
    r"^.*(?:" + "|".join(config.FOOTER_MARKERS) + r").*$", re.I | re.M
)


def keywords_sensitive(text: str) -> bool:
    t = text or ""
    m = _FOOTER.search(t)
    if m:
        t = t[: m.start()]  # ignore footer boilerplate
    t = t[:3000]
    return any(k.search(t) for k in _KW)


def title_sensitive(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in config.SENSITIVE_PAGE_TITLES)
