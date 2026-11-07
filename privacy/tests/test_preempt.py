"""Tests for preemption: user model calls go before background calls.

Uses a fake Ollama (threaded HTTP server). Run: uv run --with pytest pytest -q
privacy/tests
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

import pytest

from privacy import core
from privacy import model

ARRIVALS = []


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVACY_STATE", str(tmp_path / "maps"))
    model.set_background(False)
    yield
    model.set_background(False)


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = body["messages"][1]["content"]
        ARRIVALS.append((text, time.time()))
        if "SLOW" in text:
            time.sleep(float(text.split()[-1]))
        out = json.dumps(
            {"message": {"content": json.dumps({"echo": text})}}
        ).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except OSError:
            pass  # client hung up (preempted)


@pytest.fixture()
def ollama(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(
        model, "OLLAMA", f"http://127.0.0.1:{srv.server_address[1]}/api/chat"
    )
    monkeypatch.setenv("PRIVACY_QUIET_SECONDS", "0")
    monkeypatch.setattr(model._scheduler, "last_user", float("-inf"))
    ARRIVALS.clear()
    yield
    srv.shutdown()


def _background(text, box):
    model.set_background(True)
    t = time.time()
    try:
        box["result"] = model.call("s", text, {})
    except model.PreemptedError:
        box["preempted"] = True
    except Exception as e:  # noqa: BLE001
        box["error"] = repr(e)
    box["secs"] = time.time() - t


def test_user_call_preempts_background(ollama):
    box = {}
    bg = threading.Thread(target=_background, args=("SLOW 8", box))
    bg.start()
    time.sleep(0.5)  # background call is now in flight
    t = time.time()
    assert model.call("s", "user question", {}) == {"echo": "user question"}
    assert time.time() - t < 2  # didn't wait 8 s for the background call
    bg.join(5)
    assert box.get("preempted") is True and box["secs"] < 3


def test_background_waits_for_user_call(ollama):
    done = {}
    user = threading.Thread(
        target=lambda: done.setdefault("u", model.call("s", "SLOW 1.5", {}))
    )
    user.start()
    time.sleep(0.3)
    box = {}
    bg = threading.Thread(target=_background, args=("bg page", box))
    bg.start()
    user.join(5)
    bg.join(5)
    assert box["result"] == {"echo": "bg page"}
    (_, t_user), (_, t_bg) = ARRIVALS[0], ARRIVALS[1]
    assert (
        ARRIVALS[0][0] == "SLOW 1.5" and t_bg - t_user >= 1.4
    )  # background started only after the user call


def test_preempted_is_not_cached_as_rules_only(monkeypatch):
    def boom(*a, **k):
        raise model.PreemptedError()

    monkeypatch.setattr(model, "call", boom)
    with pytest.raises(model.PreemptedError):
        core.filter_page({"id": "p", "title": "Notes", "text": "hello"})
    with pytest.raises(model.PreemptedError):
        core.filter_email({"from": "a@b.c", "subject": "hi", "body": "hello"})


def test_background_honours_quiet_period(ollama, monkeypatch):
    monkeypatch.setenv("PRIVACY_QUIET_SECONDS", "1.5")
    model.call("s", "user question", {})
    box = {}
    bg = threading.Thread(target=_background, args=("bg page", box))
    bg.start()
    bg.join(6)
    assert box["result"] == {"echo": "bg page"}
    assert (
        ARRIVALS[1][1] - ARRIVALS[0][1] >= 1.4
    )  # waited out the quiet period after the user call


def test_background_pages_are_chunked(monkeypatch):
    seen = []

    def fake(system, user, schema):
        seen.append(user)
        return {
            "sensitive": False,
            "gist": "notes",
            "extra_pii": ["SECRET-77"] if "SECRET-77" in user else [],
        }

    monkeypatch.setattr(model, "call", fake)
    text = (
        "\n".join(f"line {i} " + "x" * 80 for i in range(120))
        + "\nkey SECRET-77 at the end"
    )
    model.set_background(True)
    try:
        r = core.filter_page(
            {"id": "p", "title": "Notes", "text": text[-5000:]}
        )
    finally:
        model.set_background(False)
    assert len(seen) > 1 and all(
        len(u) < core.BG_PAGE_CHUNK + 200 for u in seen
    )
    assert (
        "SECRET-77" not in r["redacted_text"]
    )  # found in a later chunk, still redacted
    seen.clear()
    core.filter_page(
        {"id": "p", "title": "Notes", "text": text}
    )  # user lane: one call
    assert len(seen) == 1


def test_background_connect_does_not_block_user(monkeypatch):
    connecting = threading.Event()
    release_connect = threading.Event()
    user_done = threading.Event()

    class Connection:
        def close(self):
            pass

    def connect():
        if model.in_background():
            connecting.set()
            assert release_connect.wait(5)
        return Connection(), "/api/chat"

    monkeypatch.setattr(model, "_connect", connect)
    monkeypatch.setattr(model, "_exchange", lambda *args: {"ok": True})
    monkeypatch.setattr(model._scheduler, "last_user", float("-inf"))
    monkeypatch.setenv("PRIVACY_QUIET_SECONDS", "0")
    bg_box, user_box = {}, {}
    bg = threading.Thread(target=_background, args=("background", bg_box))

    def user():
        user_box["result"] = model.call("s", "user", {})
        user_done.set()

    foreground = threading.Thread(target=user)
    bg.start()
    try:
        assert connecting.wait(2)
        foreground.start()
        assert user_done.wait(1), (
            "background connection holds the scheduling lock"
        )
        assert user_box["result"] == {"ok": True}
    finally:
        release_connect.set()
        bg.join(5)
        if foreground.ident is not None:
            foreground.join(5)


def test_cancelled_successful_response_is_discarded(monkeypatch):
    exchanging = threading.Event()
    release_response = threading.Event()

    class Connection:
        sock = None

        def close(self):
            pass

    def exchange(conn, path, body):
        if model.in_background():
            exchanging.set()
            assert release_response.wait(5)
        return {"ok": True}

    monkeypatch.setattr(model, "_connect", lambda: (Connection(), "/api/chat"))
    monkeypatch.setattr(model, "_exchange", exchange)
    monkeypatch.setattr(model._scheduler, "last_user", float("-inf"))
    monkeypatch.setenv("PRIVACY_QUIET_SECONDS", "0")
    box = {}
    bg = threading.Thread(target=_background, args=("background", box))
    bg.start()
    try:
        assert exchanging.wait(2)
        assert model.call("s", "user", {}) == {"ok": True}
    finally:
        release_response.set()
        bg.join(5)
    assert box.get("preempted") is True
    assert "result" not in box


def test_later_page_chunk_failure_keeps_earlier_redactions(monkeypatch):
    calls = []

    def fake(*args):
        calls.append(1)
        if len(calls) > 1:
            raise ConnectionError("model unavailable")
        return {
            "bucket": "headsup",
            "sensitive": True,
            "summary": "A notice.",
            "gist": "A notice.",
            "events": [],
            "extra_pii": ["PRIVATE-MARKER"],
        }

    monkeypatch.setattr(model, "call", fake)
    model.set_background(True)
    text = "PRIVATE-MARKER\n" + "ordinary words " * 150
    result = core.filter_page({"title": "Notes", "text": text})
    assert len(calls) == 2
    assert result["sensitive"]
    assert not result[
        "model_used"
    ]  # incomplete work remains eligible for retry
    assert "PRIVATE-MARKER" not in result["redacted_text"]


def test_user_arrival_during_background_connect_defers_exchange(monkeypatch):
    connecting, release = threading.Event(), threading.Event()
    exchanged = []

    class Connection:
        def close(self):
            pass

    def connect():
        connecting.set()
        assert release.wait(5)
        return Connection(), "/api/chat"

    monkeypatch.setattr(model, "_connect", connect)
    monkeypatch.setattr(
        model, "_exchange", lambda *args: exchanged.append(True)
    )
    monkeypatch.setattr(model._scheduler, "last_user", float("-inf"))
    monkeypatch.setenv("PRIVACY_QUIET_SECONDS", "120")
    box = {}
    bg = threading.Thread(target=_background, args=("background", box))
    bg.start()
    try:
        assert connecting.wait(2)
        model.begin_user_request()
        model.end_user_request()
    finally:
        release.set()
        bg.join(5)
    assert box.get("preempted") is True
    assert exchanged == []


def test_default_quiet_period_is_two_minutes(monkeypatch):
    monkeypatch.delenv("PRIVACY_QUIET_SECONDS", raising=False)
    assert model.quiet_seconds() == 120


def test_fresh_scheduler_does_not_wait_right_after_boot(monkeypatch):
    # Regression: last_user started at 0.0, and monotonic time starts near zero
    # at boot, so a just-booted machine (e.g. a CI runner) sat inside the quiet
    # period although no user request had happened.
    monkeypatch.setattr(model.time, "monotonic", lambda: 30.0)
    monkeypatch.setenv("PRIVACY_QUIET_SECONDS", "120")
    scheduler = model._Scheduler()
    waiter = threading.Thread(target=scheduler.wait_for_background, daemon=True)
    waiter.start()
    waiter.join(1)
    assert not waiter.is_alive()
