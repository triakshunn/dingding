"""Public API: redact, rehydrate, filter_email, filter_page."""
import re

from . import config, model, patterns, store

PLACEHOLDER_RE = re.compile(r"\[[A-Z]+_\d+\]")
EMAIL_LIMIT = 2000
PAGE_CHUNK = 3000


def _replace_value(text: str, value: str, ph: str) -> str:
    left = r"(?<![A-Za-z0-9])" if value[:1].isalnum() else ""
    right = r"(?![A-Za-z0-9])" if value[-1:].isalnum() else ""
    return re.sub(left + re.escape(value) + right, ph.replace("\\", "\\\\"), text)


def _apply(text: str, mapping_id: str, extra: list[tuple[str, str]] = ()) -> str:
    """Redact regex hits + extra (kind, value) pairs + every value already known to the mapping."""
    if not text:
        return text or ""
    m = store.load(mapping_id)
    pairs = list(patterns.find_pii(text)) + list(extra)
    for kind, value in pairs:
        store.placeholder_for(m, kind, value)
    # longest values first so "1-800-935-9935" wins over "9935"
    for value, ph in sorted(m["reverse"].items(), key=lambda kv: -len(kv[0])):
        text = _replace_value(text, value, ph)
    store.save(mapping_id, m)
    return text


def locked_placeholders(text: str) -> list[str]:
    """Placeholders present in text, in order of first appearance (e.g. ["[ACCOUNT_1]", "[PHONE_1]"])."""
    seen = []
    for ph in PLACEHOLDER_RE.findall(text or ""):
        if ph not in seen:
            seen.append(ph)
    return seen


def check_edit_preserves(original_redacted: str, new_redacted: str) -> tuple[bool, list[str]]:
    """An edit by the cloud model must keep every hidden value: each placeholder in the original must still appear
    (at least as many times) in the edited text. Returns (ok, missing_placeholders)."""
    new = new_redacted or ""
    missing = [ph for ph in locked_placeholders(original_redacted)
               if new.count(ph) < (original_redacted or "").count(ph)]
    return (not missing, missing)


def redact(text: str, mapping_id: str | None = None) -> tuple[str, str]:
    mapping_id = mapping_id or store.new_id()
    return _apply(text, mapping_id), mapping_id


def rehydrate(text: str, mapping_id: str) -> str:
    m = store.load(mapping_id)
    return PLACEHOLDER_RE.sub(lambda x: m["values"].get(x.group(0), x.group(0)), text or "")


def _extras(raw: list, original: str) -> list[tuple[str, str]]:
    out = []
    for v in raw or []:
        if not isinstance(v, str):
            continue
        v = v.strip()
        if 4 <= len(v) <= 80 and not PLACEHOLDER_RE.fullmatch(v) and v in original:
            out.append(("PII", v))
    return out


def _heuristic_bucket(sender: str, subject: str, body: str) -> str:
    t = f"{subject}\n{body}".lower()
    s = (sender or "").lower()
    if any(w in t for w in config.PROMO_WORDS) or "unsubscribe" in t and "noreply" in s:
        return "skip"
    if any(w in t for w in config.ACTION_WORDS):
        return "action"
    if any(w in t for w in config.EVENT_WORDS):
        return "event"
    if "?" in body and not re.search(r"no-?reply|notification|alerts?@|mailer|news", s):
        return "reply"
    return "headsup"


def _first_sentence(text: str, limit: int = 160) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    m = re.match(r"(.{20,}?[.!?])\s", t + " ")
    return (m.group(1) if m else t)[:limit]


def filter_email(msg: dict) -> dict:
    sender, subject, body = msg.get("from", ""), msg.get("subject", ""), msg.get("body", "") or ""
    mapping_id = store.new_id()
    drop = patterns.should_drop(subject, body)
    rule_sensitive = patterns.sender_sensitive(sender) or patterns.keywords_sensitive(f"{subject}\n{body}")

    r_subject = _apply(subject, mapping_id)
    r_body = _apply(body, mapping_id)

    result = {"drop": drop, "sensitive": rule_sensitive or drop, "bucket": "headsup", "summary": "",
              "events": [], "redacted_subject": r_subject, "redacted_body": r_body,
              "mapping_id": mapping_id, "model_used": False}

    if drop:
        # Never send these anywhere, not even to the local model.
        result["summary"] = "Security/sign-in message (hidden)."
        return result

    try:
        out = model.call(model.EMAIL_SYSTEM,
                         f"From: {sender}\nSubject: {r_subject}\nDate: {msg.get('date', '')}\n\n{r_body[:EMAIL_LIMIT]}",
                         model.EMAIL_SCHEMA)
        result["model_used"] = True
        result["bucket"] = out.get("bucket") if out.get("bucket") in ("action", "reply", "event", "headsup", "skip") \
            else _heuristic_bucket(sender, subject, body)
        result["sensitive"] = result["sensitive"] or bool(out.get("sensitive"))
        extra = _extras(out.get("extra_pii"), f"{subject}\n{body}")
        if extra:
            result["redacted_subject"] = _apply(subject, mapping_id, extra)
            result["redacted_body"] = _apply(body, mapping_id, extra)
        summary = out.get("summary") or ""
        events = [{"title": e.get("title", ""), "when": e.get("when", ""), "location": e.get("location", "")}
                  for e in out.get("events") or [] if isinstance(e, dict)]
    except Exception:
        result["bucket"] = _heuristic_bucket(sender, subject, body)
        summary = f"{subject}: {_first_sentence(body)}" if subject else _first_sentence(body)
        events = []

    # Final guarantee: no known PII value survives in summary/events (re-redact with the mapping).
    result["summary"] = _apply(summary, mapping_id)
    result["events"] = [{k: _apply(v, mapping_id) for k, v in e.items()} for e in events]
    return result


def filter_page(page: dict) -> dict:
    title, text = page.get("title", ""), page.get("text", "") or ""
    mapping_id = store.new_id()
    r_title = _apply(title, mapping_id)
    r_text = _apply(text, mapping_id)
    sensitive = patterns.title_sensitive(title) or patterns.keywords_sensitive(f"{title}\n{text[:5000]}") or any(
        k in ("SSN", "CARD", "PASSPORT", "ACCOUNT") for k, _ in patterns.find_pii(text))

    headings = [l.strip() for l in r_text.splitlines() if re.match(r"^\s*#{1,3}\s", l)]
    gist, model_used = "", False
    try:
        out = model.call(model.PAGE_SYSTEM, f"Title: {r_title}\n\n{r_text[:PAGE_CHUNK]}", model.PAGE_SCHEMA)
        model_used = True
        sensitive = sensitive or bool(out.get("sensitive"))
        extra = _extras(out.get("extra_pii"), f"{title}\n{text}")
        if extra:
            r_title = _apply(title, mapping_id, extra)
            r_text = _apply(text, mapping_id, extra)
            headings = [l.strip() for l in r_text.splitlines() if re.match(r"^\s*#{1,3}\s", l)]
        gist = out.get("gist") or ""
    except Exception:
        body_lines = [l for l in r_text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        gist = _first_sentence(" ".join(body_lines[:5]))

    outline = "\n".join([f"# {r_title}", f"Gist: {_apply(gist, mapping_id)}"] + headings)
    return {"sensitive": sensitive, "outline": outline, "redacted_text": r_text,
            "mapping_id": mapping_id, "model_used": model_used}
