"""Trackers: bills, waiting-for-a-reply, flights and flight price watches.

Everything here runs on the host inside the broker and uses no ChatGPT/Codex
quota: the local model only extracts facts (merchant, amount, date, flight
number, time) from already-filtered email, in the background lane, so it yields
to the user's requests; check-in and price alerts go to the user's Telegram chat
as plain messages (no model run); facts from sensitive emails leave the host
only as "🔒 private <kind> due <date>". State lives in <broker cache>/trackers/
(mode 0700/0600).
"""

import datetime as dt
import hashlib
import importlib.util
import itertools
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from email.utils import getaddresses
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

import airportsdata

import mailcore
from privacy import model as privacy_model

TZ = mailcore.TZ  # the user's configured timezone
SCAN_DAYS = 30  # look back this far for bills/flights
WAIT_DAYS = 3  # "waiting for a reply" after this many days of silence
WAIT_LOOKBACK = 21
WAIT_MAX_RECIPIENTS = 3  # more = a group email, not a personal question
UPCOMING_DAYS = 7
SERPAPI_MONTHLY_CAP = int(
    os.environ.get("SERPAPI_MONTHLY_CAP", "240")
)  # free plan: 250 searches/month
WATCH_EVERY_HOURS = 8
WATCH_RETURN_CANDIDATES = 2  # cap return searches per filtered round trip
MAX_EXACT_OUTBOUND = 4
MAX_SEARCHES_PER_REQUEST = 12  # one flight-search request (legs x dates)
SEARCH_TOP = 3  # cheapest options shown per leg and date
DROP_ALERT = 0.05  # alert when the lowest price beats the previous low by 5 %+

CANDIDATE = re.compile(
    r"receipt|invoice|payment|bill\b|billing|due\b|subscription|renew|trial|membership|charged|statement|autopay|"
    r"itinerary|flight|boarding|check-in|check"
    r" in|e-ticket|eticket|booking|reservation|PNR|departure|airline",
    re.I,
)
STRONG_SUBJECT = re.compile(
    r"receipt|invoice|order confirm|your"
    r" order|payment|renew|trial|subscription|bill\b|"
    r"statement|itinerary|boarding|check-in|e-?ticket|booking|reservation|flight",
    re.I,
)
ASKS = re.compile(
    r"\?|let me know|could you|can you|would you|please|available|when can|any"
    r" update",
    re.I,
)
AUTOMATED = re.compile(
    r"no-?reply|noreply|notifications?@|mailer|newsletter|@.*(?:lists?|groups)\.|donotreply",
    re.I,
)

# Online check-in opens this many hours before departure (default 24).
CHECKIN_HOURS = {"AI": 48, "6E": 48, "EK": 48, "QR": 48, "LH": 23}
CHECKIN_URL = {
    "DL": "https://www.delta.com/mytrips/",
    "UA": "https://www.united.com/en/us/checkin",
    "AA": "https://www.aa.com/reservation/view/find-your-trip",
    "WN": "https://www.southwest.com/air/check-in/",
    "AS": "https://www.alaskaair.com/checkin",
    "B6": "https://www.jetblue.com/check-in",
    "NK": "https://www.spirit.com/check-in",
    "F9": "https://www.flyfrontier.com/travel/my-trips/",
    "AC": "https://www.aircanada.com/checkin",
    "AI": "https://www.airindia.com/in/en/manage/web-checkin.html",
    "6E": "https://www.goindigo.in/web-check-in.html",
    "EK": "https://www.emirates.com/us/english/manage-booking/online-check-in/",
    "QR": "https://www.qatarairways.com/en/manage-booking/check-in.html",
    "LH": "https://www.lufthansa.com/us/en/online-check-in",
    "BA": "https://www.britishairways.com/travel/olcilandingpageauthreq/public/en_us",
}
AIRLINE_CODES = {
    "delta": "DL",
    "united": "UA",
    "american": "AA",
    "southwest": "WN",
    "alaska": "AS",
    "jetblue": "B6",
    "spirit": "NK",
    "frontier": "F9",
    "air canada": "AC",
    "air india": "AI",
    "indigo": "6E",
    "emirates": "EK",
    "qatar": "QR",
    "lufthansa": "LH",
    "british airways": "BA",
}

_PROMPTS = Path(__file__).resolve().parent / "prompts"  # prompt texts
EXTRACT_SYSTEM = (_PROMPTS / "extract.txt").read_text()

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "bills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["bill", "subscription", "trial", "receipt"],
                    },
                    "merchant": {"type": "string"},
                    "amount": {"type": "number"},
                    "currency": {"type": "string"},
                    "date": {"type": "string"},
                    "cadence": {"type": "string"},
                },
                "required": [
                    "kind",
                    "merchant",
                    "amount",
                    "currency",
                    "date",
                    "cadence",
                ],
            },
        },
        "flights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "airline": {"type": "string"},
                    "flight_number": {"type": "string"},
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "depart_local": {"type": "string"},
                    "arrive_local": {"type": "string"},
                },
                "required": [
                    "airline",
                    "flight_number",
                    "from",
                    "to",
                    "depart_local",
                    "arrive_local",
                ],
            },
        },
    },
    "required": ["bills", "flights"],
}


def _hid(*parts):
    return hashlib.sha1("\0".join(str(p) for p in parts).encode()).hexdigest()[
        :8
    ]


def _date(s):
    try:
        return dt.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _airports():
    try:
        return airportsdata.load("IATA")
    except Exception:  # noqa: BLE001
        return {}


def airline_code(airline, flight_number):
    """Returns the 2-letter airline code from a flight number or name."""
    m = re.match(r"\s*([A-Z0-9]{2})\s*\d", (flight_number or "").upper())
    if m:
        return m.group(1)
    return next(
        (c for n, c in AIRLINE_CODES.items() if n in (airline or "").lower()),
        "",
    )


def extract_facts(subject, body, date):
    """Extracts bill and flight facts from one filtered email (local model).

    Runs in the background lane.

    Raises:
        PreemptedError: A user request needed the model; the job is retried.
    """
    return privacy_model.call(
        EXTRACT_SYSTEM,
        f"Email date: {date}\nSubject: {subject}\n\n{body[:3000]}",
        EXTRACT_SCHEMA,
    )


def telegram(text):
    """Sends a plain Telegram message to the owner (no model run)."""
    spec = importlib.util.spec_from_file_location(
        "watchdog", Path(__file__).resolve().parents[1] / "ops/watchdog.py"
    )
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    wd.send(text, dry=False)


def _public_bill(b):
    if b[
        "sensitive"
    ]:  # never let a sensitive email's merchant/amount reach the cloud
        return {
            "id": b["id"],
            "kind": b["kind"],
            "date": b["date"],
            "private": True,
            "note": "🔒 from a sensitive email: open it in Gmail",
        }
    out = {
        k: b.get(k)
        for k in (
            "id",
            "kind",
            "merchant",
            "amount",
            "currency",
            "date",
            "cadence",
            "ref",
        )
    }
    if not out["amount"]:
        out["amount"] = None  # 0 = amount not stated
    return out


def _public_watch(w):
    last = w["history"][-1] if w["history"] else None
    lows = [h["lowest"] for h in w["history"] if h.get("lowest")]
    return {
        "id": w["id"],
        "route": f"{w['from']} → {w['to']}",
        "depart": w["depart"],
        "return": w.get("return"),
        "max_price": w.get("max_price"),
        "mode": "exact" if w.get("outbound_flights") else "route",
        "airline": w.get("airline"),
        "return_airline": w.get("return_airline") or w.get("airline"),
        "nonstop": bool(w.get("nonstop")),
        "outbound_times": w.get("outbound_times"),
        "outbound_flights": w.get("outbound_flights"),
        "outbound_via": w.get("outbound_via"),
        "return_times": w.get("return_times"),
        "return_flights": w.get("return_flights"),
        "return_via": w.get("return_via"),
        "return_max_stops": w.get("return_max_stops"),
        "return_any_time": bool(
            w.get("outbound_flights")
            and not w.get("return_times")
            and not w.get("return_flights")
        ),
        "reference_price": w.get("reference_price"),
        "latest": last,
        "lowest_seen": min(lows) if lows else None,
        "checks": len(w["history"]),
    }


def _serpapi(params):
    """Calls SerpApi search; never logs the URL (it contains the key)."""
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


_AIRPORTS_RE = re.compile(r"[A-Z]{3}(,[A-Z]{3})*")


def _check_leg(leg, today):
    """Validates and normalises one flight-search leg.

    Raises:
        ValueError: Bad airport codes or dates.
    """
    frm = str(leg.get("from", "")).upper().replace(" ", "")
    to = str(leg.get("to", "")).upper().replace(" ", "")
    if not (_AIRPORTS_RE.fullmatch(frm) and _AIRPORTS_RE.fullmatch(to)):
        raise ValueError("use 3-letter airport codes, e.g. DTW or MHT,BOS")
    dates = list(dict.fromkeys(str(d) for d in leg.get("dates") or []))
    if not dates or any(not _date(d) or _date(d) < today for d in dates):
        raise ValueError("dates must be YYYY-MM-DD and not in the past")
    return {"from": frm, "to": to, "dates": dates}


def _option(o):
    """Returns one Google Flights option in a compact form."""
    segs = o.get("flights") or []
    notes = [
        e
        for e in (o.get("extensions") or [])
        + [x for seg in segs for x in seg.get("extensions") or []]
        if re.search(r"bag|carry|overhead|personal item|fare", e, re.I)
    ]
    return {
        "price": o.get("price"),
        "airline": " + ".join(
            dict.fromkeys(g.get("airline", "") for g in segs)
        ),
        "flights": [g.get("flight_number") for g in segs],
        "from": (segs[0].get("departure_airport") or {}).get("id"),
        "depart": (segs[0].get("departure_airport") or {}).get("time"),
        "to": (segs[-1].get("arrival_airport") or {}).get("id"),
        "arrive": (segs[-1].get("arrival_airport") or {}).get("time"),
        "stops": max(0, len(segs) - 1),
        "duration_min": o.get("total_duration"),
        "bag_notes": list(dict.fromkeys(notes)),
    }


def _leg_result(leg, date, data, top):
    """Summarises one SerpApi answer: the cheapest `top` options."""
    options = [
        o
        for o in (data.get("best_flights") or [])
        + (data.get("other_flights") or [])
        if isinstance(o.get("price"), (int, float))
    ]
    options.sort(key=lambda o: o["price"])
    unique = {}  # Google lists some flights under both best and other
    for o in options:
        legs = tuple(g.get("flight_number") for g in o.get("flights") or [])
        unique.setdefault((legs, o["price"]), o)
    options = list(unique.values())
    query = f"flights from {leg['from']} to {leg['to']} on {date} one way"
    return {
        "from": leg["from"],
        "to": leg["to"],
        "date": date,
        "options": [_option(o) for o in options[:top]],
        "price_level": (data.get("price_insights") or {}).get("price_level"),
        "error": data.get("error"),
        "google_flights": "https://www.google.com/travel/flights?q="
        + urllib.parse.quote(query),
    }


def _matching_options(
    data, *, airline=None, nonstop=False, frm=None, to=None, date=None
):
    """Return priced itineraries whose visible leg satisfies the watch."""
    options = (data.get("best_flights") or []) + (
        data.get("other_flights") or []
    )
    matches = []
    for option in options:
        legs = option.get("flights") or []
        if not isinstance(option.get("price"), (int, float)):
            continue
        if not legs and any((airline, nonstop, frm, to, date)):
            continue
        if any(
            (left.get("arrival_airport") or {}).get("id")
            != (right.get("departure_airport") or {}).get("id")
            for left, right in itertools.pairwise(legs)
        ):
            continue
        if nonstop and len(legs) != 1:
            continue
        if frm and (legs[0].get("departure_airport") or {}).get("id") != frm:
            continue
        if to and (legs[-1].get("arrival_airport") or {}).get("id") != to:
            continue
        if date and not str(
            (legs[0].get("departure_airport") or {}).get("time", "")
        ).startswith(date):
            continue
        if airline and any(
            airline_code(leg.get("airline"), leg.get("flight_number"))
            not in airline.split(",")
            for leg in legs
        ):
            continue
        matches.append(option)
    return sorted(matches, key=lambda option: option["price"])


def parse_price(
    data, *, airline=None, nonstop=False, frm=None, to=None, date=None
):
    """Summarizes a SerpApi Google Flights answer.

    Returns:
        {"lowest", "level", "typical", "best"}, where best has price, airline,
            stops, departs and duration_min of the cheapest option.
    """
    filtered = any((airline, nonstop, frm, to, date))
    priced = _matching_options(
        data, airline=airline, nonstop=nonstop, frm=frm, to=to, date=date
    )
    best = priced[0] if priced else None
    ins = data.get("price_insights") or {}
    out = {
        "lowest": (best or {}).get("price")
        if filtered
        else ins.get("lowest_price") or (best or {}).get("price"),
        "level": None if filtered else ins.get("price_level"),
        "typical": None if filtered else ins.get("typical_price_range"),
    }
    if best:
        legs = best.get("flights") or []
        out["best"] = {
            "price": best["price"],
            "airline": " + ".join(
                dict.fromkeys(leg.get("airline", "") for leg in legs)
            ),
            "stops": max(0, len(legs) - 1),
            "departs": (
                (legs[0].get("departure_airport") or {}).get("time")
                if legs
                else None
            ),
            "duration_min": best.get("total_duration"),
        }
    return out


def _flight_number(option):
    """Ordered segment numbers, e.g. DL 123 + DL 456 -> DL123+DL456."""
    legs = option.get("flights") or []
    if not legs:
        return ""
    numbers = [
        re.sub(r"\s+", "", leg.get("flight_number") or "").upper()
        for leg in legs
    ]
    if any(not re.fullmatch(r"[A-Z0-9]{2}\d{1,4}", n) for n in numbers):
        return ""
    return "+".join(numbers)


def _via_airports(option):
    """Return the ordered connection airports for one itinerary."""
    return [
        (leg.get("arrival_airport") or {}).get("id")
        for leg in (option.get("flights") or [])[:-1]
    ]


def _flight_time(option):
    legs = option.get("flights") or []
    if not legs:
        return ""
    stamp = (legs[0].get("departure_airport") or {}).get("time") or ""
    return stamp.rsplit(" ", 1)[-1] if " " in stamp else ""


def _watch_times(values, label):
    times = list(dict.fromkeys(values or []))
    if not times or any(
        not isinstance(t, str)
        or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t)
        for t in times
    ):
        raise ValueError(f"{label} must contain 24-hour HH:MM times")
    return times


def _watch_itineraries(values, airline, label):
    """Validate and normalise comma-separated ordered flight numbers."""
    if any(not isinstance(raw, str) for raw in values):
        raise ValueError(f"{label} must be flight numbers")
    itineraries = [
        "+".join(
            re.sub(r"\s+", "", segment).upper()
            for segment in re.split(r"[,+]", raw)
        )
        for raw in values
    ]
    if any(
        not re.fullmatch(r"[A-Z0-9]{2}\d{1,4}", segment)
        or segment[:2] not in airline.split(",")
        for itinerary in itineraries
        for segment in itinerary.split("+")
    ):
        raise ValueError(
            f"{label} must use the airline code for every segment, "
            "e.g. DL123,DL456"
        )
    return itineraries


def _watch_airlines(value, label):
    """Normalise one or more comma-separated IATA carrier codes."""
    raw = str(value or "").upper().replace(" ", "")
    if raw == "DELTA":
        raw = "DL"
    if not raw:
        return None
    codes = list(dict.fromkeys(raw.split(",")))
    if any(not re.fullmatch(r"[A-Z0-9]{2}", code) for code in codes):
        raise ValueError(f"{label} must be IATA codes, e.g. DL or DL,UA")
    return ",".join(codes)


class Trackers:
    """The user's trackers, kept in the broker's cache directory.

    Attributes:
        br: The broker (for mail records, the queue and the backend).
        notify: Sends a Telegram message (replaced in tests).
        now: Returns the current time in the user's timezone (replaced in
            tests).
    """

    def __init__(
        self, broker, notify=telegram, now=lambda: dt.datetime.now(TZ)
    ):
        self.br = broker
        self.notify = notify
        self.now = now
        self.dir = broker.cache / "trackers"
        self.dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self._airports = None
        self._queued = set()  # extraction jobs waiting in the background lane

    # ---------- storage ----------
    def _load(self, name, default):
        p = self.dir / f"{name}.json"
        return json.loads(p.read_text()) if p.exists() else default

    def _save(self, name, obj):
        p = self.dir / f"{name}.json"
        tmp = p.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)

    # ---------- bills & flights from email (background) ----------
    def scan(self):
        """Queues fact extraction for recent emails about money or travel.

        Runs in the background lane every 30 minutes. Emails without money or
        travel keywords are recorded as having no facts, without a model call.
        """
        known = set(self._load("facts", {}))
        cutoff = time.time() - SCAN_DAYS * 86400
        nothing, todo = [], []
        for f in (self.br.cache / "mail").glob("*/*.json"):
            ref = f"{f.parent.name}:{f.stem}"
            if ref in known or f.stat().st_mtime < cutoff:
                continue
            flt = json.loads(f.read_text())["filter"]
            subject = flt.get("redacted_subject", "")
            text = f"{subject}\n{flt.get('redacted_body', '')}"
            likely = CANDIDATE.search(text) and (
                flt.get("bucket") != "skip" or STRONG_SUBJECT.search(subject)
            )
            (todo if likely and not flt.get("drop") else nothing).append(ref)
        with (
            self.br._lock
        ):  # merge: extraction jobs may be writing results concurrently
            facts = self._load("facts", {})
            for ref in nothing:
                facts.setdefault(ref, {"bills": [], "flights": []})
            self._save("facts", facts)
        for ref in todo:
            if ref not in self._queued:
                self._queued.add(ref)
                self.br.submit(4, self.extract, ref)

    def extract(self, ref):
        """Extracts and stores the facts of one email (background job).

        A model failure is recorded as "no facts" so the email isn't retried
        forever.

        Raises:
            PreemptedError: A user request needed the model; the broker
                re-queues.
        """
        acct, mid = ref.split(":", 1)
        rec = self.br.mail_record(acct, mid)
        flt, msg = rec["filter"], rec["msg"]
        try:
            out = extract_facts(
                flt.get("redacted_subject", ""),
                flt.get("redacted_body", ""),
                msg.get("date", ""),
            )
        except privacy_model.PreemptedError:
            raise  # a user needed the model: the broker re-queues this job
        except Exception as e:  # noqa: BLE001  (model down/invalid output: record and move on, no endless retries)
            out = {"bills": [], "flights": [], "error": type(e).__name__}
        self._queued.discard(
            ref
        )  # not on PreemptedError: that job is still queued
        with self.br._lock:
            facts = self._load("facts", {})
            facts[ref] = {
                "bills": out.get("bills") or [],
                "flights": out.get("flights") or [],
                "error": out.get("error"),
                "sensitive": bool(flt.get("sensitive")),
                "date": msg.get("date", ""),
            }
            self._save("facts", facts)
        return facts[ref]

    def _all(self, key):
        dismissed = set(self._load("dismissed", []))
        rows = {}
        for ref, f in self._load("facts", {}).items():
            for x in f.get(key) or []:
                fid = _hid(
                    key,
                    (x.get("merchant") or x.get("flight_number") or "").lower(),
                    x.get("date") or x.get("depart_local"),
                )
                if fid not in dismissed:
                    rows[fid] = {
                        **x,
                        "id": fid,
                        "ref": ref,
                        "sensitive": f.get("sensitive", False),
                    }
        return list(rows.values())

    def bills(self, days=UPCOMING_DAYS):
        """Returns upcoming bills/renewals/trials and the latest subscriptions.

        Args:
            days: How far ahead counts as upcoming.

        Returns:
            {"upcoming": [...], "subscriptions": [...]}; entries from sensitive
                emails only carry kind, date and "private".
        """
        today = self.now().date()
        rows = [b for b in self._all("bills") if _date(b.get("date"))]
        upcoming = sorted(
            (
                b
                for b in rows
                if b["kind"] in ("bill", "subscription", "trial")
                and today <= _date(b["date"]) <= today + dt.timedelta(days=days)
            ),
            key=lambda b: b["date"],
        )
        subs = {}
        for b in sorted(
            (
                b
                for b in rows
                if b["kind"] == "subscription" and not b["sensitive"]
            ),
            key=lambda b: b["date"],
        ):
            subs[b["merchant"].lower()] = b  # latest charge per merchant
        return {
            "upcoming": [_public_bill(b) for b in upcoming],
            "subscriptions": [_public_bill(b) for b in subs.values()],
        }

    # ---------- flights + check-in alerts ----------
    def _depart_utc(self, f):
        if self._airports is None:
            self._airports = _airports()
        tz = (self._airports.get((f.get("from") or "").upper()) or {}).get(
            "tz"
        ) or TZ.key
        try:
            local = dt.datetime.fromisoformat(f["depart_local"][:16])
        except (ValueError, KeyError, TypeError):
            return None
        return local.replace(tzinfo=ZoneInfo(tz)).astimezone(dt.UTC)

    def flights(self, days=60):
        """Returns upcoming flights with check-in opening time and link.

        Args:
            days: How far ahead to look (flights up to 6 h past departure are
                kept).
        """
        now = self.now().astimezone(dt.UTC)
        out = []
        for f in self._all("flights"):
            dep = self._depart_utc(f)
            if dep and now - dt.timedelta(hours=6) <= dep <= now + dt.timedelta(
                days=days
            ):
                code = airline_code(f.get("airline"), f.get("flight_number"))
                opens = dep - dt.timedelta(hours=CHECKIN_HOURS.get(code, 24))
                out.append(
                    {
                        **{
                            k: f.get(k)
                            for k in (
                                "id",
                                "airline",
                                "flight_number",
                                "from",
                                "to",
                                "depart_local",
                                "arrive_local",
                            )
                        },
                        "checkin_opens": opens.astimezone(TZ).isoformat(
                            timespec="minutes"
                        ),
                        "checkin_url": CHECKIN_URL.get(code)
                        or "https://www.google.com/search?q="
                        + urllib.parse.quote(
                            f"{f.get('airline', '')} online check-in"
                        ),
                    }
                )
        return sorted(out, key=lambda f: f["checkin_opens"])

    def checkin_alerts(self):
        """Sends one Telegram alert per flight once its check-in opens."""
        sent = set(self._load("alerted", []))
        now = self.now()
        for f in self.flights(days=3):
            opens = dt.datetime.fromisoformat(f["checkin_opens"])
            if f["id"] in sent or now < opens:
                continue
            dep = dt.datetime.fromisoformat(f["depart_local"][:16])
            self.notify(
                f"✈️ Check-in is open: {f['airline']} {f['flight_number']} ·"
                f" {f['from']} → {f['to']} ·"
                f" {dep:%a %b %-d, %-I:%M %p} (local)\nCheck in:"
                f" {f['checkin_url']}"
            )
            sent.add(f["id"])
            self._save("alerted", sorted(sent))

    # ---------- waiting for a reply (background, no model needed for the check
    # itself) ----------
    def scan_waiting(self):
        """Finds sent emails still waiting for a reply (runs every 6 hours).

        A thread counts when its last message is the user's, it is at least
        WAIT_DAYS calendar days old, it went to at most WAIT_MAX_RECIPIENTS
        non-automated recipients, and the user's text asks something.
        """
        now = self.now()
        waiting = []
        for acct in self.br.b.accounts():
            me = (self.br.profile_email(acct) or "").lower()
            seen_threads = set()
            for mid in self.br.b.mail_list(
                acct, f"in:sent newer_than:{WAIT_LOOKBACK}d -in:chats", 60
            ):
                t = self.br.b.thread_meta(acct, mid)
                if not t or t["thread_id"] in seen_threads:
                    continue
                seen_threads.add(t["thread_id"])
                last = t["messages"][-1]
                if me not in parseaddr(last["from"])[1].lower():
                    continue  # someone answered after you
                when = dt.datetime.fromisoformat(last["date"])
                days = (
                    now.astimezone(TZ).date() - when.astimezone(TZ).date()
                ).days  # calendar days
                to = [a for _, a in getaddresses([last.get("to", "")]) if a]
                if (
                    days < WAIT_DAYS
                    or not to
                    or len(to) > WAIT_MAX_RECIPIENTS
                    or any(AUTOMATED.search(a) for a in to)
                ):
                    continue
                rec = self.br.mail_record(
                    acct, last["id"]
                )  # filtered: redacted subject, sensitive flag
                body = rec["filter"].get("redacted_body", "")
                if not ASKS.search(body):
                    continue
                name = (
                    getaddresses([last.get("to", "")])[0][0]
                    or to[0].split("@")[0]
                )
                waiting.append(
                    {
                        "id": _hid("wait", acct, t["thread_id"]),
                        "ref": f"{acct}:{last['id']}",
                        "to": (
                            "🔒 (sensitive)"
                            if rec["filter"].get("sensitive")
                            else name
                        ),
                        "subject": (
                            ""
                            if rec["filter"].get("sensitive")
                            else rec["filter"].get("redacted_subject", "")
                        ),
                        "sent": when.date().isoformat(),
                        "days": days,
                    }
                )
        dismissed = set(self._load("dismissed", []))
        self._save(
            "waiting",
            sorted(
                (w for w in waiting if w["id"] not in dismissed),
                key=lambda w: -w["days"],
            ),
        )

    def waiting(self):
        """Returns the waiting-for-a-reply list without dismissed items."""
        dismissed = set(self._load("dismissed", []))
        return [
            w for w in self._load("waiting", []) if w["id"] not in dismissed
        ]

    # ---------- flight price watches (SerpApi Google Flights) ----------
    def _serpapi_key(self):
        p = Path(
            os.environ.get(
                "SERPAPI_KEY_FILE",
                Path(__file__).resolve().parents[1] / "data/serpapi_key",
            )
        )
        return p.read_text().strip() if p.exists() else ""

    def _new_watch(self, a):
        """Validate watch input and build a record without saving it.

        Args:
            a: {"from", "to", "depart", "return"?, "max_price"?,
                "airline"?, "nonstop"?} with IATA codes and YYYY-MM-DD dates.

        Raises:
            ValueError: Missing field, bad airport code or date.
        """
        for k in ("from", "to", "depart"):
            if not a.get(k):
                raise ValueError(f"missing {k}")
        frm, to = a["from"].upper(), a["to"].upper()
        if not (
            re.fullmatch(r"[A-Z]{3}", frm) and re.fullmatch(r"[A-Z]{3}", to)
        ):
            raise ValueError("use 3-letter airport codes, e.g. DTW BOM")
        for k in ("depart", "return"):
            if a.get(k) and (
                not isinstance(a[k], str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a[k])
                or not _date(a[k])
            ):
                raise ValueError(f"{k} must be YYYY-MM-DD")
        if a.get("return") and _date(a["return"]) <= _date(a["depart"]):
            raise ValueError("return must be after depart")
        if _date(a["depart"]) < self.now().date():
            raise ValueError("departure date is in the past")
        airline = _watch_airlines(a.get("airline"), "airline")
        return_airline = _watch_airlines(
            a.get("return_airline"), "return_airline"
        )
        if return_airline and not a.get("return"):
            raise ValueError("return_airline needs a return date")
        nonstop = a.get("nonstop", False)
        if not isinstance(nonstop, bool):
            raise ValueError("nonstop must be true or false")
        max_price = a.get("max_price")
        if max_price is not None and (
            not isinstance(max_price, (int, float))
            or isinstance(max_price, bool)
            or not math.isfinite(max_price)
            or max_price <= 0
        ):
            raise ValueError("max_price must be a positive number")
        return {
            "id": _hid(
                "watch",
                frm,
                to,
                a["depart"],
                a.get("return"),
                *(
                    [airline, nonstop, return_airline]
                    if return_airline
                    else ([airline, nonstop] if airline or nonstop else [])
                ),
            ),
            "from": frm,
            "to": to,
            "depart": a["depart"],
            "return": a.get("return"),
            "max_price": max_price,
            "airline": airline,
            "return_airline": return_airline,
            "nonstop": nonstop,
            "created": self.now().isoformat(timespec="minutes"),
            "history": [],
        }

    def _store_watch(self, w):
        watches = self._load("watches", [])
        watches = [x for x in watches if x["id"] != w["id"]] + [w]
        self._save("watches", watches)

    def watch_add(self, a):
        """Adds (or replaces) a general route price watch."""
        w = self._new_watch(a)
        self._store_watch(w)
        note = (
            ""
            if self._serpapi_key()
            else (
                " (no SerpApi key on the laptop yet: checks start once it is"
                " added)"
            )
        )
        return {
            "added": w["id"],
            "watch": _public_watch(w),
            "note": "Checked about every 8 hours while search quota remains." + note,
        }

    def watch_exact_add(self, a):  # noqa: PLR0912, PLR0915  (validation and fare resolution)
        """Resolve specified outbound flights and save a checked itinerary.

        Each outbound option has a fixed sequence of flight numbers. The
        return is filtered by date, airline, times and allowed stops.
        No watch is saved if any requested outbound cannot be identified or
        has no matching return fare.
        """
        if not a.get("return"):
            raise ValueError("exact itinerary needs a return date")
        w = self._new_watch(a)
        w["exact"] = True
        if not w["airline"]:
            raise ValueError(
                "which airline? Give its 2-character code, e.g. DL"
            )
        return_carrier = w["return_airline"] or w["airline"]
        raw_times = a.get("outbound_times") or []
        raw_numbers = a.get("outbound_flights") or []
        raw_itineraries = a.get("outbound_itineraries") or []
        if sum(bool(x) for x in (raw_times, raw_numbers, raw_itineraries)) != 1:
            raise ValueError(
                "give outbound time(s), flight number(s), or full "
                "itinerary segment numbers"
            )
        if not w["nonstop"] and not raw_itineraries:
            raise ValueError(
                "for a connecting outbound use --outbound-itinerary "
                "DL123,DL456 with every segment number"
            )
        times = _watch_times(raw_times, "outbound_times") if raw_times else []
        numbers = _watch_itineraries(
            raw_numbers, w["airline"], "outbound flights"
        )
        itineraries = _watch_itineraries(
            raw_itineraries, w["airline"], "outbound itineraries"
        )
        if w["nonstop"] and any(
            "+" in itinerary for itinerary in numbers + itineraries
        ):
            raise ValueError("remove --nonstop for a connecting outbound")
        specs = times or numbers or itineraries
        if len(specs) > MAX_EXACT_OUTBOUND:
            raise ValueError("use at most four outbound options per watch")
        return_max_stops = a.get("return_max_stops")
        return_any_stops = a.get("return_any_stops", False)
        raw_return_itineraries = a.get("return_itineraries") or []
        return_itineraries = _watch_itineraries(
            raw_return_itineraries, return_carrier, "return itineraries"
        )
        if len(return_itineraries) > MAX_EXACT_OUTBOUND:
            raise ValueError("use at most four return options per watch")
        if w["nonstop"] and any(
            "+" in itinerary for itinerary in return_itineraries
        ):
            raise ValueError("remove --nonstop for a connecting return")
        if return_itineraries:
            if return_max_stops is not None or return_any_stops:
                raise ValueError(
                    "return flight numbers already specify the stop pattern"
                )
            return_max_stops = 0 if w["nonstop"] else None
        elif w["nonstop"]:
            if return_max_stops is not None or return_any_stops:
                raise ValueError("--nonstop already requires a nonstop return")
            return_max_stops = 0
        elif (
            not isinstance(return_any_stops, bool)
            or (return_max_stops is None) != return_any_stops
            or (
                return_max_stops is not None
                and (
                    isinstance(return_max_stops, bool)
                    or return_max_stops not in (0, 1, 2)
                )
            )
        ):
            raise ValueError(
                "choose --return-max-stops 0|1|2 or --return-any-stops"
            )
        w["return_max_stops"] = return_max_stops
        return_any = a.get("return_any_time", False)
        raw_return_times = a.get("return_times") or []
        if return_itineraries:
            if return_any or raw_return_times:
                raise ValueError(
                    "return itinerary numbers already specify the return flight"
                )
        elif (
            not isinstance(return_any, bool)
            or bool(raw_return_times) == return_any
        ):
            raise ValueError(
                "choose --return-any-time, return time(s), or return itinerary"
            )
        return_times = (
            _watch_times(raw_return_times, "return_times")
            if raw_return_times
            else []
        )
        reference = a.get("reference_price")
        if reference is not None and (
            not isinstance(reference, (int, float))
            or isinstance(reference, bool)
            or not math.isfinite(reference)
            or reference <= 0
        ):
            raise ValueError("reference_price must be a positive number")
        key = self._serpapi_key()
        if not key:
            raise ValueError("no SerpApi key on the laptop yet")
        month = self.now().strftime("%Y-%m")
        used = self._load("serpapi_usage", {}).get(month, 0)
        if used + 1 + len(specs) > SERPAPI_MONTHLY_CAP:
            raise ValueError("not enough flight searches left this month")

        self._spend()
        data = self.fetch_price(w, key)
        if data.get("error"):
            raise ValueError("Google Flights lookup failed; try again later")
        outbound = _matching_options(
            data,
            airline=w["airline"],
            nonstop=w["nonstop"],
            frm=w["from"],
            to=w["to"],
            date=w["depart"],
        )
        selected = []
        for spec in specs:
            candidates = [
                o
                for o in outbound
                if (_flight_time(o) if times else _flight_number(o)) == spec
            ]
            unique = {
                _flight_number(o): o
                for o in reversed(candidates)
                if _flight_number(o)
            }
            if len(unique) != 1:
                available = (
                    ", ".join(
                        f"{_flight_time(o)} {_flight_number(o)}"
                        + (
                            f" via {','.join(_via_airports(o))}"
                            if _via_airports(o)
                            else ""
                        )
                        for o in outbound[:8]
                    )
                    or "none"
                )
                raise ValueError(
                    f"could not identify one {w['airline']} "
                    f"outbound for {spec}; "
                    f"available: {available}. Ask which flight to track."
                )
            selected.append(next(iter(unique.values())))
        if len({_flight_number(o) for o in selected}) != len(selected):
            raise ValueError("outbound options must name different flights")

        results = []
        return_paths = {}
        resolved_returns = set(return_itineraries)
        for option in selected:
            token = option.get("departure_token")
            if not token:
                raise ValueError(
                    "Google Flights did not provide return options for "
                    f"{_flight_number(option)}"
                )
            self._spend()
            returns = self.fetch_price(w, key, token)
            if returns.get("error"):
                raise ValueError(
                    "Google Flights return lookup failed; try again later"
                )
            choices = _matching_options(
                returns,
                airline=return_carrier,
                nonstop=return_max_stops == 0,
                frm=w["to"],
                to=w["from"],
                date=w["return"],
            )
            if return_max_stops is not None:
                choices = [
                    o
                    for o in choices
                    if len(o.get("flights") or []) - 1 <= return_max_stops
                ]
            if return_times:
                timed = []
                for at in return_times:
                    same_time = [o for o in choices if _flight_time(o) == at]
                    unique = {_flight_number(o) for o in same_time}
                    if len(unique) != 1 or "" in unique:
                        available = (
                            ", ".join(
                                f"{_flight_time(o)} {_flight_number(o)}"
                                for o in choices[:8]
                            )
                            or "none"
                        )
                        raise ValueError(
                            f"return time {at} is missing or ambiguous for "
                            f"{_flight_number(option)}; available: {available}"
                        )
                    timed.extend(same_time)
                    number = next(iter(unique))
                    resolved_returns.add(number)
                    return_paths[number] = _via_airports(same_time[0])
                choices = sorted(timed, key=lambda o: o["price"])
            if return_itineraries:
                choices = [
                    o
                    for o in choices
                    if _flight_number(o) in return_itineraries
                ]
                found = {_flight_number(o) for o in choices}
                if found != set(return_itineraries):
                    missing = ", ".join(sorted(set(return_itineraries) - found))
                    raise ValueError(
                        f"return flight(s) {missing} not found for "
                        f"{_flight_number(option)} on {w['return']}"
                    )
                return_paths.update(
                    {_flight_number(o): _via_airports(o) for o in choices}
                )
            if not choices:
                raise ValueError(
                    f"no matching return fare for {_flight_number(option)} "
                    f"on {w['return']}; ask about another return option"
                )
            results.append((option, choices[0]))

        w["id"] = _hid(
            "watch-exact",
            w["from"],
            w["to"],
            w["depart"],
            w["return"],
            *sorted(_flight_number(o) for o in selected),
            *([w["airline"]] if "," in w["airline"] else []),
            *([return_carrier] if w["return_airline"] else []),
            *sorted(resolved_returns),
            *return_times,
            *([return_max_stops] if return_max_stops != 0 else []),
        )
        w["outbound_flights"] = [_flight_number(o) for o in selected]
        w["outbound_times"] = [_flight_time(o) for o in selected]
        w["outbound_via"] = [_via_airports(o) for o in selected]
        w["return_times"] = return_times or None
        w["return_flights"] = sorted(resolved_returns) or None
        w["return_via"] = return_paths or None
        w["reference_price"] = reference
        best = min(results, key=lambda pair: pair[1]["price"])
        initial = parse_price(
            {"best_flights": [best[1]]},
            airline=return_carrier,
            nonstop=return_max_stops == 0,
            frm=w["to"],
            to=w["from"],
            date=w["return"],
        )
        initial["best"]["outbound_flight"] = _flight_number(best[0])
        initial["best"]["outbound_via"] = _via_airports(best[0])
        initial["best"]["return_flight"] = _flight_number(best[1])
        initial["best"]["return_via"] = _via_airports(best[1])
        w["history"] = [
            {"at": self.now().isoformat(timespec="minutes"), **initial}
        ]
        self._store_watch(w)
        return {
            "added": w["id"],
            "watch": _public_watch(w),
            "note": "Exact outbound flight(s) verified; return fare checked. "
            "Checks run about every 8 hours while search quota remains; price changes go to Telegram. "
            "This does not enable Google Flights email tracking.",
        }

    def watch_rm(self, wid):
        """Removes the watch with this id.

        Raises:
            ValueError: No such watch.
        """
        watches = self._load("watches", [])
        keep = [w for w in watches if w["id"] != wid]
        if len(keep) == len(watches):
            raise ValueError(f"no watch {wid}")
        self._save("watches", keep)
        return {"removed": wid}

    def watches(self):
        """Returns all price watches (route, dates, latest and lowest price)."""
        return [_public_watch(w) for w in self._load("watches", [])]

    def _budget_ok(self):
        usage = self._load("serpapi_usage", {})
        return usage.get(self.now().strftime("%Y-%m"), 0) < SERPAPI_MONTHLY_CAP

    def _spend(self):
        usage = self._load("serpapi_usage", {})
        k = self.now().strftime("%Y-%m")
        usage[k] = usage.get(k, 0) + 1
        self._save("serpapi_usage", usage)

    def fetch_price(self, w, key, departure_token=None):
        """Queries SerpApi Google Flights for one leg (uses one search)."""
        q = {
            "engine": "google_flights",
            "departure_id": w["from"],
            "arrival_id": w["to"],
            "outbound_date": w["depart"],
            "type": "1" if w.get("return") else "2",
            "currency": "USD",
            "hl": "en",
            "api_key": key,
        }
        if w.get("return"):
            q["return_date"] = w["return"]
        if w.get("exact") or w.get("outbound_flights"):
            q["show_hidden"] = "true"
            q["deep_search"] = "true"
        carriers = (
            w.get("return_airline") or w.get("airline")
            if departure_token
            else w.get("airline")
        )
        if carriers:
            q["include_airlines"] = carriers
        max_stops = (
            w.get("return_max_stops", 0 if w.get("nonstop") else None)
            if departure_token
            else (0 if w.get("nonstop") else None)
        )
        if max_stops is not None:
            q["stops"] = str(max_stops + 1)
        if departure_token:
            q["departure_token"] = departure_token
        return _serpapi(q)

    def search_flights(self, legs, carry_on=True, top=SEARCH_TOP):
        """Searches Google Flights now, one-way per leg and date (SerpApi).

        Args:
            legs: [{"from": "DTW", "to": "MHT,BOS", "dates": ["2026-12-20"]}],
                IATA codes (comma-separated alternatives) and YYYY-MM-DD dates.
            carry_on: Price fares including one overhead-bin carry-on bag.
            top: Cheapest options to return per leg and date.

        Returns:
            {"legs": [...], "searches_used", "searches_left_this_month"}; each
            leg/date lists its cheapest options with times, stops and bag notes.

        Raises:
            ValueError: Bad input, no SerpApi key, or not enough searches left.
        """
        legs = [_check_leg(leg, self.now().date()) for leg in legs]
        needed = sum(len(leg["dates"]) for leg in legs)
        if not legs or needed > MAX_SEARCHES_PER_REQUEST:
            raise ValueError(
                f"1-{MAX_SEARCHES_PER_REQUEST} searches per request "
                f"(legs x dates); this needs {needed}"
            )
        key = self._serpapi_key()
        if not key:
            raise ValueError("no SerpApi key on the laptop yet")
        month = self.now().strftime("%Y-%m")
        used = self._load("serpapi_usage", {}).get(month, 0)
        if used + needed > SERPAPI_MONTHLY_CAP:
            raise ValueError(
                f"only {SERPAPI_MONTHLY_CAP - used} flight searches left this "
                "month"
            )
        results = []
        for leg in legs:
            for date in leg["dates"]:
                self._spend()
                q = {
                    "engine": "google_flights",
                    "departure_id": leg["from"],
                    "arrival_id": leg["to"],
                    "outbound_date": date,
                    "type": "2",
                    "currency": "USD",
                    "hl": "en",
                    "api_key": key,
                }
                if carry_on:
                    q["bags"] = "1"
                results.append(_leg_result(leg, date, _serpapi(q), top))
        return {
            "legs": results,
            "carry_on_included": carry_on,
            "searches_used": needed,
            "searches_left_this_month": SERPAPI_MONTHLY_CAP - used - needed,
            "note": "Prices from Google Flights, one-way per leg; book on the "
            "airline or Google Flights site.",
        }

    def check_prices(self):  # noqa: PLR0912, PLR0915  (watch state and notifications)
        """Checks each due watch (about every eight hours) and alerts on price changes.

        Stays within SERPAPI_MONTHLY_CAP searches per month. Alerts when the
        price beats the previous low by DROP_ALERT or first goes under the
        watch's max price.
        """
        key = self._serpapi_key()
        if not key:
            return
        watches = self._load("watches", [])
        now = self.now()
        changed = False
        for w in watches:
            if _date(w["depart"]) and _date(w["depart"]) < now.date():
                continue
            if w["history"] and now - dt.datetime.fromisoformat(
                w["history"][-1]["at"]
            ) < dt.timedelta(hours=WATCH_EVERY_HOURS):
                continue
            if not self._budget_ok():
                break
            self._spend()
            try:
                data = self.fetch_price(w, key)
                filtered = bool(
                    w.get("airline")
                    or w.get("nonstop")
                    or w.get("return_airline")
                )
                if filtered and w.get("return"):
                    outbound = _matching_options(
                        data,
                        airline=w.get("airline"),
                        nonstop=w.get("nonstop"),
                        frm=w["from"],
                        to=w["to"],
                        date=w["depart"],
                    )
                    exact_flights = set(w.get("outbound_flights") or [])
                    if exact_flights:
                        exact_via = dict(
                            zip(
                                w["outbound_flights"],
                                w.get("outbound_via") or [],
                                strict=False,
                            )
                        )
                        outbound = [
                            o
                            for o in outbound
                            if _flight_number(o) in exact_flights
                            and (
                                _flight_number(o) not in exact_via
                                or _via_airports(o)
                                == exact_via[_flight_number(o)]
                            )
                        ]
                    res = {"lowest": None}
                    seen_tokens = set()
                    seen_flights = set()
                    for candidate in outbound:
                        token = candidate.get("departure_token")
                        if not token or token in seen_tokens:
                            continue
                        number = _flight_number(candidate)
                        if exact_flights and number in seen_flights:
                            continue
                        limit = (
                            len(exact_flights)
                            if exact_flights
                            else WATCH_RETURN_CANDIDATES
                        )
                        if len(seen_tokens) >= limit or not self._budget_ok():
                            break
                        seen_tokens.add(token)
                        seen_flights.add(number)
                        self._spend()
                        returns = self.fetch_price(w, key, token)
                        return_max_stops = w.get(
                            "return_max_stops", 0 if w.get("nonstop") else None
                        )
                        choices = _matching_options(
                            returns,
                            airline=w.get("return_airline") or w.get("airline"),
                            nonstop=return_max_stops == 0,
                            frm=w["to"],
                            to=w["from"],
                            date=w["return"],
                        )
                        if return_max_stops is not None:
                            choices = [
                                o
                                for o in choices
                                if len(o.get("flights") or []) - 1
                                <= return_max_stops
                            ]
                        if w.get("return_times"):
                            choices = [
                                o
                                for o in choices
                                if _flight_time(o) in w["return_times"]
                            ]
                        if w.get("return_flights"):
                            return_ids = set(w["return_flights"])
                            return_via = w.get("return_via") or {}
                            choices = [
                                o
                                for o in choices
                                if _flight_number(o) in return_ids
                                and (
                                    _flight_number(o) not in return_via
                                    or _via_airports(o)
                                    == return_via[_flight_number(o)]
                                )
                            ]
                        result = parse_price(
                            {"best_flights": choices[:1]},
                            airline=w.get("return_airline") or w.get("airline"),
                            nonstop=return_max_stops == 0,
                            frm=w["to"],
                            to=w["from"],
                            date=w["return"],
                        )
                        if result.get("lowest") and (
                            not res.get("lowest")
                            or result["lowest"] < res["lowest"]
                        ):
                            result["best"]["outbound_flight"] = number
                            result["best"]["outbound_time"] = _flight_time(
                                candidate
                            )
                            result["best"]["outbound_via"] = _via_airports(
                                candidate
                            )
                            result["best"]["return_flight"] = _flight_number(
                                choices[0]
                            )
                            result["best"]["return_via"] = _via_airports(
                                choices[0]
                            )
                            res = result
                else:
                    res = parse_price(
                        data,
                        airline=w.get("airline"),
                        nonstop=w.get("nonstop", False),
                        frm=w["from"] if filtered else None,
                        to=w["to"] if filtered else None,
                        date=w["depart"] if filtered else None,
                    )
            except Exception as e:  # noqa: BLE001  (never log the URL: it holds the key)
                res = {"error": type(e).__name__}
            prev = [h["lowest"] for h in w["history"] if h.get("lowest")]
            w["history"].append(
                {"at": now.isoformat(timespec="minutes"), **res}
            )
            w["history"] = w["history"][-60:]
            changed = True
            low = res.get("lowest")
            exact = bool(w.get("outbound_flights"))
            price_changed = (
                bool(low and prev and low != prev[-1])
                if exact
                else bool(
                    low
                    and (
                        (prev and low <= min(prev) * (1 - DROP_ALERT))
                        or (
                            w.get("max_price")
                            and low <= w["max_price"]
                            and not any(p <= w["max_price"] for p in prev)
                        )
                    )
                )
            )
            if price_changed:
                b = res.get("best") or {}
                direction = "📈" if exact and low > prev[-1] else "📉"
                label = "Flight price changed" if exact else "Flight price drop"
                outbound_time = b.get("outbound_time") or next(
                    (
                        t
                        for n, t in zip(
                            w.get("outbound_flights") or [],
                            w.get("outbound_times") or [],
                            strict=False,
                        )
                        if n == b.get("outbound_flight")
                    ),
                    "?",
                )
                self.notify(
                    f"{direction} {label}: {w['from']} → {w['to']} on"
                    f" {w['depart']}"
                    + (f" (return {w['return']})" if w.get("return") else "")
                    + f": ${low:,.0f}"
                    + (
                        f", was ${prev[-1] if exact else min(prev):,.0f}"
                        if prev
                        else ""
                    )
                    + (
                        f"\nOutbound: {b.get('outbound_flight')} at "
                        f"{outbound_time}"
                        + (
                            f" via {', '.join(b['outbound_via'])}"
                            if b.get("outbound_via")
                            else ""
                        )
                        if exact
                        else ""
                    )
                    + (
                        f"\nReturn: {b.get('return_flight')}"
                        + (
                            f" via {', '.join(b['return_via'])}"
                            if b.get("return_via")
                            else ""
                        )
                        if exact and w.get("return_flights")
                        else ""
                    )
                    + (
                        f"\n{b.get('airline')} · "
                        + (
                            "nonstop both ways"
                            if w.get("return") and w.get("nonstop")
                            else f"{b.get('stops')} stop(s)"
                        )
                        if b
                        else ""
                    )
                    + "\nGoogle Flights:"
                    " https://www.google.com/travel/flights?q="
                    + urllib.parse.quote(
                        f"flights from {w['from']} to {w['to']} on"
                        f" {w['depart']}"
                        + (
                            f" returning {w['return']}"
                            if w.get("return")
                            else ""
                        )
                    )
                )
        if changed:
            self._save("watches", watches)

    # ---------- agent-facing ----------
    def dismiss(self, ids):
        """Hides tracker items (bills, flights, waiting) by id."""
        d = set(self._load("dismissed", [])) | set(ids)
        self._save("dismissed", sorted(d))
        return {"dismissed": sorted(set(ids))}

    def summary(self):
        """Returns the tracker items a brief should show."""
        b = self.bills()
        return {
            "upcoming_bills": b["upcoming"],
            "waiting_on": self.waiting()[:5],
            "flights": self.flights(days=7),
            "price_watches": self.watches(),
        }

    def report(self, what=None):
        """Returns one tracker (bills/waiting/flights/watches) or all."""
        if what == "bills":
            return self.bills(days=30)
        if what == "waiting":
            return self.waiting()
        if what == "flights":
            return self.flights()
        if what == "watches":
            return self.watches()
        return {
            **self.summary(),
            "subscriptions": self.bills()["subscriptions"],
        }
