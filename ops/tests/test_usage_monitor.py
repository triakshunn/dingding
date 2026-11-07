import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["USAGE_TIMEZONE"] = (
    "America/New_York"  # expectations use this day boundary
)
SPEC = importlib.util.spec_from_file_location(
    "usage_monitor", Path(__file__).parents[1] / "usage_monitor.py"
)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


class UsageMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = (
            Path(self.temp.name) / "agents/test/agent/openclaw-agent.sqlite"
        )
        self.path.parent.mkdir(parents=True)
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute(
            "CREATE TABLE trajectory_runtime_events (session_id TEXT, run_id"
            " TEXT, seq INTEGER, event_json TEXT)"
        )

    def event(self, seq, kind, data, run="run1", provider="openai"):
        e = {
            "type": kind,
            "provider": provider,
            "modelId": "test-model",
            "sessionKey": "agent:test:main",
            "ts": "2026-11-07T01:00:00Z",
            "data": data,
        }
        self.db.execute(
            "INSERT INTO trajectory_runtime_events VALUES ('session',?,?,?)",
            (run, seq, json.dumps(e)),
        )
        self.db.commit()

    def test_separate_cache_and_reasoning_without_double_counting(self):
        usage = {
            "input": 100,
            "cacheRead": 900,
            "cacheWrite": 0,
            "output": 50,
            "reasoningTokens": 20,
            "total": 1050,
        }
        self.event(
            1,
            "context.compiled",
            {"tools": [{}, {}], "prompt": "PRIVATE PROMPT"},
        )
        self.event(
            2,
            "tool.call",
            {"name": "process", "arguments": {"secret": "PRIVATE ARGUMENT"}},
        )
        self.event(
            3,
            "model.completed",
            {
                "usage": usage,
                "messagesSnapshot": [
                    {"usage": usage, "content": "PRIVATE RESPONSE"}
                ],
            },
        )
        self.event(
            4, "model.completed", {"usage": usage}
        )  # repeated completion, same run
        rows = monitor.collect(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["day"], "2026-11-06")
        total = monitor.aggregate(rows)
        self.assertEqual(total["all_input"], 1000)
        self.assertEqual(total["total"], 1050)
        self.assertEqual(total["cache_hit_percent"], 90)
        self.assertEqual(total["tool_calls"], 1)
        report = monitor.make_report(rows, "2026-11-06")
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertNotIn("PRIVATE", monitor.markdown(report))
        self.assertFalse(rows[0]["prompt_error"])

    def test_unknown_usage_and_heartbeat_classification(self):
        self.event(1, "tool.call", {"name": "heartbeat_respond"})
        self.event(
            2,
            "model.completed",
            {"usage": None, "promptError": "PRIVATE ERROR"},
        )
        self.event(
            3,
            "model.completed",
            {"usage": {"input": 9}},
            run="local",
            provider="ollama",
        )
        rows = monitor.collect(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workload"], "heartbeat")
        self.assertTrue(rows[0]["prompt_error"])
        total = monitor.aggregate(rows)
        self.assertEqual(total["missing_usage_turns"], 1)
        self.assertEqual(total["measured_turns"], 0)
        self.assertIn(
            "unknown", monitor.markdown(monitor.make_report(rows, "2026-11-06"))
        )

    def test_in_progress_runs_are_not_counted_and_source_is_unchanged(self):
        self.event(1, "tool.call", {"name": "exec"})
        self.assertEqual(monitor.collect(self.path), [])
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM trajectory_runtime_events"
            ).fetchone()[0],
            1,
        )

    def test_repeated_collection_does_not_duplicate_usage(self):
        self.event(
            1,
            "model.completed",
            {
                "usage": {
                    "input": 100,
                    "cacheRead": 900,
                    "output": 50,
                    "total": 1050,
                }
            },
        )
        out = Path(self.temp.name) / "report"
        command = [
            sys.executable,
            str(Path(monitor.__file__)),
            "--state",
            self.temp.name,
            "--output",
            str(out),
            "--date",
            "2026-11-06",
            "--no-breaker",
        ]
        for _ in range(2):
            subprocess.run(command, check=True, capture_output=True, text=True)
        report = json.loads((out / "latest.json").read_text())
        self.assertEqual(report["totals"]["total"], 1050)
        self.assertEqual(report["totals"]["measured_turns"], 1)
        self.assertEqual((out / "latest.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)


class BreakerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.orig = monitor.BREAKER
        monitor.BREAKER = Path(self.temp.name) / "breaker.json"
        self.addCleanup(setattr, monitor, "BREAKER", self.orig)
        self.calls, self.alerts = [], []
        self.now = monitor.dt.datetime(
            2026, 11, 7, 0, 0, tzinfo=monitor.dt.timezone.utc
        )

    def row(self, minutes_ago, cached, uncached=0, output=0):
        at = (self.now - monitor.dt.timedelta(minutes=minutes_ago)).isoformat()
        return {
            "usage_known": True,
            "at": at,
            "tokens": {
                "input": uncached,
                "cacheRead": cached,
                "cacheWrite": 0,
                "output": output,
                "reasoningTokens": 0,
                "total": 0,
            },
        }

    def check(self, rows, limit=1000):
        return monitor.check_breaker(
            rows,
            self.now,
            minutes=60,
            limit=limit,
            stop=lambda *a: self.calls.append(a) or 0,
            notify=self.alerts.append,
        )

    def test_under_limit_or_old_usage_does_nothing(self):
        self.assertFalse(
            self.check([self.row(5, 600), self.row(90, 5000)])
        )  # 90 min ago is outside the window
        self.assertEqual(self.calls, [])
        self.assertFalse(monitor.BREAKER.exists())

    def test_trips_once_stops_container_latches_and_alerts(self):
        rows = [self.row(5, 700, 200, 50), self.row(30, 100)]
        self.assertTrue(self.check(rows))
        self.assertEqual(self.calls, [("stop", monitor.CONTAINER)])
        latch = json.loads(monitor.BREAKER.read_text())
        self.assertEqual(
            (latch["tokens"], latch["runs"], latch["container_stopped"]),
            (1050, 2, True),
        )
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("--resume", self.alerts[0])
        self.assertFalse(
            self.check([*rows, self.row(1, 99999)])
        )  # latched: no second stop or alert
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.alerts), 1)


if __name__ == "__main__":
    unittest.main()
