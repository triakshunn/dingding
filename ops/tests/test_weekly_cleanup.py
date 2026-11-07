"""Tests for ops/weekly_cleanup.py with a fake broker (no Gmail or Telegram)."""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "weekly_cleanup.py"


def load(path=SCRIPT):
    spec = importlib.util.spec_from_file_location("weekly_cleanup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_broker(plans, fail=None):
    """A fake `call`: hands out `plans` (dicts or exceptions) for previews."""
    calls = []
    queue = list(plans)

    def call(op, **kw):
        calls.append((op, kw))
        if "confirm" in kw:
            plan = next(p for p in sent if p["plan_id"] == kw["confirm"])
            return {
                "batch_id": "b-" + kw["confirm"],
                "failed": fail or [],
                "by_account": plan["by_account"],
            }
        nxt = queue.pop(0) if queue else {"total": 0}
        if isinstance(nxt, BaseException):
            raise nxt
        sent.append(nxt)
        return nxt

    sent = []
    return call, calls


def plan(pid, n, more=0, acct="personal", area="spam"):
    return {
        "plan_id": pid,
        "total": n,
        "more_not_included": more,
        "by_account": {acct: {area: n}},
        "top_senders_sample": ["Shop", "News"],
    }


SCENARIOS = {
    "nothing": ([], None, False),
    "one_round": ([plan("p1", 5)], None, False),
    "backlog": (
        [plan("p1", 2000, 10), plan("p2", 2000, 10), plan("p3", 2000, 10)],
        None,
        False,
    ),
    "failures": (
        [plan("p1", 4)],
        [{"account": "school", "area": "spam", "error": "quota"}],
        False,
    ),
    "daily_limit": (
        [SystemExit("daily cleanup limit (6000) reached")],
        None,
        False,
    ),
    "broker_error": (
        [SystemExit("Can't reach the privacy broker")],
        None,
        False,
    ),
    "dry_run": ([plan("p1", 7, 3)], None, True),
}


def run(module, plans, fail, dry):
    call, calls = fake_broker(plans, fail)
    sent = []
    argv = ["weekly_cleanup.py"] + (["--dry-run"] if dry else [])
    with (
        mock.patch.object(module, "call", call),
        mock.patch.object(
            module, "send", lambda msg, dry=False: sent.append(msg)
        ),
        mock.patch.object(sys, "argv", argv),
        mock.patch("builtins.print"),
    ):
        module.main()
    return sent, calls


class WeeklyCleanupTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_nothing_to_clean(self):
        sent, _ = run(self.m, *SCENARIOS["nothing"])
        self.assertEqual(
            sent, ["🧹 Sunday cleanup: nothing to clean this week."]
        )

    def test_backlog_stops_after_max_rounds_and_says_so(self):
        sent, calls = run(self.m, *SCENARIOS["backlog"])
        confirms = [kw["confirm"] for op, kw in calls if "confirm" in kw]
        self.assertEqual(confirms, ["p1", "p2", "p3"])
        self.assertIn("moved 6000 emails", sent[0])
        self.assertIn("the rest goes next Sunday", sent[0])

    def test_failures_and_daily_limit_are_reported(self):
        sent, _ = run(self.m, *SCENARIOS["failures"])
        self.assertIn("⚠️ Problems: school spam: quota", sent[0])
        sent, _ = run(self.m, *SCENARIOS["daily_limit"])
        self.assertIn("the rest goes next Sunday", sent[0])

    def test_dry_run_changes_nothing_and_sends_nothing(self):
        sent, calls = run(self.m, *SCENARIOS["dry_run"])
        self.assertEqual(sent, [])
        self.assertFalse(any("confirm" in kw for _, kw in calls))


if __name__ == "__main__":
    unittest.main()
