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
import json
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
WATCH_EVERY_HOURS = 24
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


def parse_price(data):
    """Summarizes a SerpApi Google Flights answer.

    Returns:
        {"lowest", "level", "typical", "best"}, where best has price, airline,
            stops, departs and duration_min of the cheapest option.
    """
    options = (data.get("best_flights") or []) + (
        data.get("other_flights") or []
    )
    priced = [o for o in options if isinstance(o.get("price"), (int, float))]
    best = min(priced, key=lambda o: o["price"]) if priced else None
    ins = data.get("price_insights") or {}
    out = {
        "lowest": ins.get("lowest_price") or (best or {}).get("price"),
        "level": ins.get("price_level"),
        "typical": ins.get("typical_price_range"),
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
                Path.home() / "dingding/data/serpapi_key",
            )
        )
        return p.read_text().strip() if p.exists() else ""

    def watch_add(self, a):
        """Adds (or replaces) a flight price watch.

        Args:
            a: {"from", "to", "depart", "return"?, "max_price"?} with IATA codes
                and YYYY-MM-DD dates.

        Returns:
            The new watch's id and public view, plus a note.

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
            if a.get(k) and not _date(a[k]):
                raise ValueError(f"{k} must be YYYY-MM-DD")
        watches = self._load("watches", [])
        w = {
            "id": _hid("watch", frm, to, a["depart"], a.get("return")),
            "from": frm,
            "to": to,
            "depart": a["depart"],
            "return": a.get("return"),
            "max_price": a.get("max_price"),
            "created": self.now().isoformat(timespec="minutes"),
            "history": [],
        }
        watches = [x for x in watches if x["id"] != w["id"]] + [w]
        self._save("watches", watches)
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
            "note": "Checked about once a day." + note,
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

    def fetch_price(self, w, key):
        """Queries SerpApi Google Flights for one watch (uses one search)."""
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

    def check_prices(self):
        """Checks each due watch (about daily) and alerts on price drops.

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
                res = parse_price(self.fetch_price(w, key))
            except Exception as e:  # noqa: BLE001  (never log the URL: it holds the key)
                res = {"error": type(e).__name__}
            prev = [h["lowest"] for h in w["history"] if h.get("lowest")]
            w["history"].append(
                {"at": now.isoformat(timespec="minutes"), **res}
            )
            w["history"] = w["history"][-60:]
            changed = True
            low = res.get("lowest")
            if low and (
                (prev and low <= min(prev) * (1 - DROP_ALERT))
                or (
                    w.get("max_price")
                    and low <= w["max_price"]
                    and not any(p <= w["max_price"] for p in prev)
                )
            ):
                b = res.get("best") or {}
                self.notify(
                    f"📉 Flight price drop: {w['from']} → {w['to']} on"
                    f" {w['depart']}"
                    + (f" (return {w['return']})" if w.get("return") else "")
                    + f": ${low:,.0f}"
                    + (f", was ${min(prev):,.0f}" if prev else "")
                    + (
                        f"\n{b.get('airline')} · {b.get('stops')} stop(s)"
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
