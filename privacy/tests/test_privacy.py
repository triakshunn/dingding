"""Unit tests (model mocked). Run: cd ~/dingding && uv run --with pytest pytest -q privacy/tests"""

import os
import stat

import pytest

import privacy
from privacy import model
from privacy import patterns
from privacy import store


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVACY_STATE", str(tmp_path / "maps"))


def no_model(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("ollama down")

    monkeypatch.setattr(model, "call", boom)


def fake_model(monkeypatch, out):
    seen = {}

    def call(system, user, schema):
        seen["user"] = user
        return out

    monkeypatch.setattr(model, "call", call)
    return seen


# ---------- layer 1: detectors ----------


@pytest.mark.parametrize(
    "text,kind,value",
    [
        ("Statement for account ending 4821 is ready", "ACCOUNT", "4821"),
        ("card ending in 1234 was charged", "ACCOUNT", "1234"),
        ("Visa ****9876 declined", "ACCOUNT", "9876"),
        ("Account number 123456789012", "ACCOUNT", "123456789012"),
        ("Pay with 4111 1111 1111 1111 today", "CARD", "4111 1111 1111 1111"),
        ("SSN 123-45-6789", "SSN", "123-45-6789"),
        ("Call 1-800-935-9935 now", "PHONE", "1-800-935-9935"),
        ("Call (734) 555-0199", "PHONE", "(734) 555-0199"),
        ("My cell is 734.555.0199", "PHONE", "734.555.0199"),
        ("India: +91 98765 43210", "PHONE", "+91 98765 43210"),
        ("Passport No. Z1234567 required", "PASSPORT", "Z1234567"),
        ("Your UMID 41234567 is needed", "ID", "41234567"),
        ("Your verification code is 583920.", "CODE", "583920"),
        ("583920 is your Google code", "CODE", "583920"),
        ("DOB: 04/12/2003", "DOB", "04/12/2003"),
        ("Visit 1500 Example Rd, Springfield", "ADDRESS", "1500 Example Rd"),
        (
            "I live at 123 N Main Street Apt 4B now",
            "ADDRESS",
            "123 N Main Street Apt 4B",
        ),
    ],
)
def test_detects(text, kind, value):
    assert (kind, value) in patterns.find_pii(text)


@pytest.mark.parametrize(
    "text",
    [
        "Seminar Thursday Nov 12 at 4:00pm in Room 1311",
        "Meet in FRB 2300 at 2:30pm",
        "Total: $35.00 due Nov 20, 2026",
        "Order 1234567 shipped",  # 7 digits
        "Card 4111 1111 1111 1112",  # fails Luhn
    ],
)
def test_no_false_positives(text):
    kinds = {k for k, _ in patterns.find_pii(text)}
    assert not kinds & {"CARD", "PHONE", "ADDRESS", "SSN", "PASSPORT"}


def test_drop_rules():
    assert patterns.should_drop("Your verification code", "")
    assert patterns.should_drop("Reset your password", "")
    assert patterns.should_drop(
        "Sign in to Slack", "Click the link below to sign in"
    )
    assert not patterns.should_drop("Research meeting", "Can we meet Tuesday?")


def test_sensitive_rules():
    assert patterns.sender_sensitive("Chase <alerts@chase.com>")
    assert patterns.sender_sensitive("USCIS <noreply@uscis.gov>")
    assert patterns.keywords_sensitive("Please upload your I-20")
    assert not patterns.sender_sensitive("Priya <pshah@example.edu>")


# ---------- redact / rehydrate ----------


def test_redact_rehydrate_roundtrip():
    text = (
        "Call 1-800-935-9935 about account ending 4821. Again: 1-800-935-9935."
    )
    red, mid = privacy.redact(text)
    assert "935" not in red and "4821" not in red
    assert red.count("[PHONE_1]") == 2 and "[ACCOUNT_1]" in red
    assert privacy.rehydrate(red, mid) == text


def test_same_value_same_placeholder_across_calls():
    r1, mid = privacy.redact("phone (734) 555-0199")
    r2, _ = privacy.redact("again (734) 555-0199", mid)
    assert r1.split()[-1] == r2.split()[-1] == "[PHONE_1]"


def test_rehydrate_leaves_unknown_placeholders():
    _, mid = privacy.redact("nothing here")
    assert privacy.rehydrate("Hi [PHONE_9] there", mid) == "Hi [PHONE_9] there"


def test_store_permissions(tmp_path):
    _, mid = privacy.redact("SSN 123-45-6789")
    d = store.state_dir()
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(d / f"{mid}.json").st_mode) == 0o600


def test_bad_mapping_id_rejected():
    with pytest.raises(ValueError):
        privacy.rehydrate("x", "../../etc/passwd")


# ---------- filter_email ----------

CHASE = {
    "account": "personal",
    "id": "a1",
    "from": "Chase <alerts@chase.com>",
    "subject": "Your statement is ready",
    "date": "Thu, 5 Nov 2026",
    "body": "Your statement for account ending 4821 is ready. Minimum payment of $35.00 is due Nov 20. "
    "Questions? Call 1-800-935-9935.",
}


def test_email_model_sees_only_redacted_text(monkeypatch):
    seen = fake_model(
        monkeypatch,
        {
            "bucket": "action",
            "sensitive": True,
            "summary": "Card ending 4821 payment of $35 due Nov 20.",
            "events": [],
            "extra_pii": [],
        },
    )
    r = privacy.filter_email(CHASE)
    assert "4821" not in seen["user"] and "935-9935" not in seen["user"]
    assert r["bucket"] == "action" and r["sensitive"] and r["model_used"]
    assert "4821" not in r["summary"]  # summary re-redacted by code
    assert "[ACCOUNT_1]" in r["redacted_body"]
    assert "4821" in privacy.rehydrate(r["redacted_body"], r["mapping_id"])


def test_email_extra_pii_from_model_is_redacted(monkeypatch):
    msg = {
        "from": "Landlord <ll@gmail.com>",
        "subject": "Lease",
        "body": "Gate code is red-falcon-22, see you Friday?",
    }
    fake_model(
        monkeypatch,
        {
            "bucket": "reply",
            "sensitive": False,
            "summary": "Gate code red-falcon-22 shared.",
            "events": [],
            "extra_pii": ["red-falcon-22", "[PHONE_1]", "x", "not-in-text-123"],
        },
    )
    r = privacy.filter_email(msg)
    assert (
        "red-falcon-22" not in r["redacted_body"]
        and "red-falcon-22" not in r["summary"]
    )
    assert "[PII_1]" in r["redacted_body"]


def test_email_drop_never_calls_model(monkeypatch):
    called = []
    monkeypatch.setattr(model, "call", lambda *a: called.append(1))
    r = privacy.filter_email(
        {
            "from": "Google <no-reply@accounts.google.com>",
            "subject": "Security code",
            "body": "Your verification code is 583920.",
        }
    )
    assert r["drop"] and r["sensitive"] and not called
    assert "583920" not in r["redacted_body"] and "583920" not in r["summary"]


def test_email_fallback_when_model_down(monkeypatch):
    no_model(monkeypatch)
    r = privacy.filter_email(CHASE)
    assert r["model_used"] is False
    assert r["bucket"] in ("action", "reply", "event", "headsup", "skip")
    assert r["bucket"] == "action"
    assert "4821" not in r["summary"] and "935-9935" not in r["summary"]


@pytest.mark.parametrize(
    "msg,bucket",
    [
        (
            {
                "from": "Uber Eats <promo@uber.com>",
                "subject": "20% off tonight!",
                "body": "Offer ends Sunday.",
            },
            "skip",
        ),
        (
            {
                "from": "ECE <ece-seminars@example.edu>",
                "subject": "Robotics seminar",
                "body": "Join us Thursday at 4pm.",
            },
            "event",
        ),
        (
            {
                "from": "Priya <pshah@example.edu>",
                "subject": "meeting",
                "body": "Could we meet Tuesday?",
            },
            "reply",
        ),
    ],
)
def test_fallback_heuristic_buckets(monkeypatch, msg, bucket):
    no_model(monkeypatch)
    assert privacy.filter_email(msg)["bucket"] == bucket


def test_event_fields_redacted(monkeypatch):
    msg = {
        "from": "Club <c@example.edu>",
        "subject": "Party",
        "body": "Party at 123 N Main Street Sat 7pm",
    }
    fake_model(
        monkeypatch,
        {
            "bucket": "event",
            "sensitive": False,
            "summary": "Party Saturday.",
            "events": [
                {
                    "title": "Party",
                    "when": "Sat 7pm",
                    "location": "123 N Main Street",
                }
            ],
            "extra_pii": [],
        },
    )
    r = privacy.filter_email(msg)
    assert r["events"][0]["location"].startswith("[ADDRESS_")
    assert r["events"][0]["when"] == "Sat 7pm"


def test_invalid_model_bucket_falls_back(monkeypatch):
    fake_model(
        monkeypatch,
        {
            "bucket": "weird",
            "sensitive": False,
            "summary": "s",
            "events": [],
            "extra_pii": [],
        },
    )
    r = privacy.filter_email(
        {"from": "x@y.com", "subject": "Payment due", "body": "Pay by Friday"}
    )
    assert r["bucket"] == "action"


# ---------- filter_page ----------


def test_page_sensitive_by_title_and_outline_has_no_pii(monkeypatch):
    fake_model(
        monkeypatch,
        {
            "sensitive": False,
            "gist": "Insurance policy details for car.",
            "extra_pii": [],
        },
    )
    page = {
        "id": "p1",
        "title": "Insurance",
        "text": "# Car\nPolicy for 2019 Civic. Agent phone (734) 555-0199.\n## Claims\nClaim filed March.",
    }
    r = privacy.filter_page(page)
    assert r["sensitive"]
    assert (
        "555-0199" not in r["outline"] and "555-0199" not in r["redacted_text"]
    )
    assert "# Car" in r["outline"] and "## Claims" in r["outline"]
    assert "555-0199" in privacy.rehydrate(r["redacted_text"], r["mapping_id"])


def test_page_normal_and_fallback(monkeypatch):
    no_model(monkeypatch)
    r = privacy.filter_page(
        {
            "id": "p2",
            "title": "F1",
            "text": "# 2026 season\nVerstappen vs Norris notes.",
        }
    )
    assert (
        not r["sensitive"] and r["model_used"] is False and "F1" in r["outline"]
    )


# ---------- agent D additions ----------


@pytest.mark.parametrize(
    "text,kind,value",
    [
        ("Call me at 555-0100.", "PHONE", "555-0100"),
        ("text 555 0100 tonight", "PHONE", "555 0100"),
        ("Contact +65 9123 4567 any time", "PHONE", "+65 9123 4567"),
        ("HP 9123 4567", "PHONE", "9123 4567"),
        ("Mob: +91-9876543210", "PHONE", "+91-9876543210"),
        ("call 98765 43210", "PHONE", "98765 43210"),
        (
            "Blk 123 Ang Mo Kio Ave 3 #05-12, Singapore 560123",
            "ADDRESS",
            "Blk 123 Ang Mo Kio Ave 3 #05-12, Singapore 560123",
        ),
        ("Deliver to Singapore 560123 please", "POSTAL", "560123"),
        (
            "Flat 302, Sunrise Apartments, Indiranagar, Bengaluru 560038",
            "POSTAL",
            "560038",
        ),
        (
            "Flat 302, Sunrise Apartments, Indiranagar, Bengaluru 560038",
            "ADDRESS",
            "Flat 302, Sunrise Apartments, Indiranagar",
        ),
        ("Your UMID 41234567", "ID", "41234567"),
        ("12345678 is your student number", "ID", "12345678"),
    ],
)
def test_detects_intl(text, kind, value):
    assert (kind, value) in patterns.find_pii(text)


@pytest.mark.parametrize(
    "text",
    [
        "Order #12345678 has shipped",
        "Order number: 12345678",
        "Tracking 12345678",
        "Booking ref 12345678",
        "Room 555-0100",
        "Pay $1,250.00 by 11/20/2026",
    ],
)
def test_not_redacted(text):
    assert patterns.find_pii(text) == []


def test_locked_placeholders_and_edit_check():
    orig = "Bank [ACCOUNT_1] — call [PHONE_1] or [PHONE_1]"
    assert privacy.locked_placeholders(orig) == ["[ACCOUNT_1]", "[PHONE_1]"]
    ok, missing = privacy.check_edit_preserves(
        orig, "Bank [ACCOUNT_1] — call [PHONE_1] or [PHONE_1] (updated)"
    )
    assert ok and missing == []
    ok, missing = privacy.check_edit_preserves(
        orig, "Bank account — call [PHONE_1]"
    )
    assert not ok and set(missing) == {"[ACCOUNT_1]", "[PHONE_1]"}
    assert privacy.check_edit_preserves("no secrets here", "anything") == (
        True,
        [],
    )


def test_model_requests_keep_alive(monkeypatch):
    sent = {}

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(model, "_connect", lambda: (Conn(), "/api/chat"))
    monkeypatch.setattr(
        model, "_exchange", lambda conn, path, body: sent.update(body) or {}
    )
    model.call("s", "u", {})
    assert sent["keep_alive"] and sent["model"] == model.model_name()
    assert sent["options"]["num_predict"] == model.max_tokens()


def test_id_context_is_checked_for_every_match():
    # Regression: the loop variable `kind` was overwritten after the first match, so later 8-digit numbers of the
    # same pattern skipped the context check (an order number after a student ID got hidden).
    found = patterns.find_pii(
        "Your student ID 12345678 was updated. Order #87654321 shipped."
    )
    assert ("ID", "12345678") in found
    assert all(v != "87654321" for _, v in found)
