"""Tunable lists for the deterministic layer. Edit freely; everything is lowercase-matched."""

# Sender domains / addresses whose mail is always "sensitive" (never released in full to the cloud model).
SENSITIVE_SENDERS = [
    # banks / payments
    "chase.com", "bankofamerica.com", "bofa.com", "wellsfargo.com", "capitalone.com", "americanexpress.com",
    "aexp.com", "discover.com", "citi.com", "citibank.com", "usbank.com", "pnc.com", "ally.com", "sofi.com",
    "schwab.com", "fidelity.com", "vanguard.com", "robinhood.com", "paypal.com", "venmo.com", "zellepay.com",
    "hdfcbank.net", "hdfcbank.com", "hdfcbank.bank.in", ".bank.in", "icicibank.com", "sbi.co.in", "axisbank.com", "kotak.com", "wise.com",
    # government / immigration / tax
    "irs.gov", "uscis.gov", "state.gov", "dhs.gov", "ice.gov", "studentaid.gov", "ssa.gov",
    # add your school's international office here, e.g. "intl-office@example.edu"
    # health / insurance
    "mychart",  # add your health system's domains
    "aetna.com", "bcbsm.com", "bcbs.com", "uhc.com", "unitedhealthcare.com", "cigna.com", "humana.com",
    "geico.com", "progressive.com", "statefarm.com", "allstate.com", "lemonade.com",
    # grades / records
    # add your school's registrar, e.g. "registrar@example.edu"
]

# Patterns that make an email or page sensitive regardless of sender.
# Regexes with word boundaries, matched against the subject + main body only (footers stripped),
# so "upgrade", "opt out", "transcription" etc. don't trigger. (?-i:...) = case-sensitive part.
SENSITIVE_KEYWORDS = [
    # immigration
    r"\b(?:F-1|J-1|H-1B|I-20|I20|SEVIS|EAD)\b", r"(?-i:\bOPT\b|\bCPT\b|\bSTEM OPT\b)",
    r"\bvisa (?:appointment|interview|status|application|stamp|renewal|expir\w*)\b", r"\bpassport\b",
    r"\bimmigration\b", r"\bgreen card\b", r"\bwork permit\b",
    # health
    r"\bdiagnos(?:is|ed)\b", r"\bprescription\b", r"\b(?:lab|test) results?\b", r"\bmedical records?\b",
    r"\bappointment with dr\b", r"\btherapy session\b", r"\binsurance claim\b", r"\bexplanation of benefits\b",
    # academics
    r"\bgrades? (?:posted|available|report|change)\b", r"\bfinal grades?\b", r"(?-i:\bGPA\b)",
    r"\bofficial transcripts?\b", r"\btranscript request\b", r"\bacademic probation\b", r"\bdisciplinary\b",
    # legal
    r"\blawsuit\b", r"\bsubpoena\b", r"\blegal notice\b",
    # tax / identity
    r"\btax return\b", r"\bW-2\b", r"\b1099(?:-\w+)?\b", r"\bsocial security\b",
    # money
    r"\bstatement is (?:ready|available)\b", r"\baccount balance\b", r"\bminimum payment\b", r"\bpayment due\b",
    r"\boverdraft\b", r"\bwire transfer\b", r"\bdebited\b", r"\bcredited to your (?:a/c|account)\b",
    # account security
    r"\bsecurity alert\b", r"\bsuspicious (?:sign-?in|login|activity)\b", r"\bnew sign-?in\b",
]

# Body text after any of these lines is treated as footer and ignored by the keyword check.
FOOTER_MARKERS = [r"unsubscribe", r"opt[- ]out", r"manage (?:your )?(?:email )?preferences", r"privacy policy",
                  r"you(?:'| a)re receiving this", r"view (?:this email )?in (?:your )?browser"]

# Page titles that are sensitive (Notion).
SENSITIVE_PAGE_TITLES = [
    "financ", "bank", "insurance", "health", "medical", "passport", "visa", "immigration", "tax",
    "password", "credential", "salary", "budget", "loan", "grades", "transcript", "legal",
]

# Emails matching these never leave the laptop at all (drop=True).
DROP_PATTERNS = [
    r"verification code", r"security code", r"one[- ]time (?:pass)?code", r"\botp\b", r"passcode",
    r"(?:sign[- ]?in|log[- ]?in|login|confirmation|access) code", r"your code is", r"2fa", r"two[- ]factor code",
    r"reset (?:your )?password", r"password reset", r"forgot (?:your )?password",
    r"magic link", r"sign[- ]in link", r"log[- ]?in link", r"click (?:the link )?(?:below )?to (?:sign|log) ?in",
    r"confirm (?:it'?s|it is) you",
]

# Very rough promo detection for the no-model fallback.
PROMO_WORDS = ["% off", "sale", "deal", "promo", "coupon", "limited time", "offer ends", "shop now", "free shipping"]
EVENT_WORDS = ["join us", "seminar", "talk by", "workshop", "career fair", "info session", "webinar", "rsvp",
               "you're invited", "you are invited", "meetup", "colloquium", "panel"]
ACTION_WORDS = ["due", "deadline", "submit", "pay ", "payment", "renew", "expires", "by friday", "by monday",
                "upload", "complete your", "action required", "register by"]
