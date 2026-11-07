"""Read OpenClaw usage metadata without model/API calls or exporting conversation content.

Usage breaker: if recorded tokens in a rolling window pass a ceiling, stop the OpenClaw container, write a latch
(data/usage/breaker.json) and send one Telegram alert. It stays stopped until `usage_monitor.py --resume`.
Only completed runs are recorded, so a run still in progress can overshoot the ceiling before it trips.

Keeps a deduplicated local ledger and daily JSON/Markdown reports. Token counts are
observed OpenClaw counters, not billed dollars or remaining subscription allowance.
"""

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
from collections import Counter
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def _timezone():
    """USAGE_TIMEZONE, else the timezone in data/config.json, else UTC."""
    name = os.environ.get("USAGE_TIMEZONE")
    config = ROOT / "data/config.json"
    if not name and config.exists():
        name = json.loads(config.read_text()).get("timezone")
    return ZoneInfo(name or "UTC")


TZ = _timezone()
BREAKER = (
    ROOT / "data/usage/breaker.json"
)  # latch: present = OpenClaw deliberately kept stopped
CONTAINER = os.environ.get("WATCHDOG_CONTAINER", "openclaw")
BREAKER_MINUTES = int(os.environ.get("USAGE_BREAKER_MINUTES", "60"))
BREAKER_TOKENS = int(
    os.environ.get("USAGE_BREAKER_TOKENS", "1500000")
)  # all input (incl. cached) + output
COUNTERS = (
    "input",
    "cacheRead",
    "cacheWrite",
    "output",
    "reasoningTokens",
    "total",
)

# Extract metadata inside SQLite: never load prompts, responses, arguments, or tool results.
QUERY = """
SELECT session_id, run_id, seq,
 json_extract(event_json, '$.type'), json_extract(event_json, '$.ts'),
 json_extract(event_json, '$.provider'), json_extract(event_json, '$.modelId'),
 json_extract(event_json, '$.sessionKey'), json_extract(event_json, '$.data.usage'),
 json_extract(event_json, '$.data.name'),
 json_array_length(json_extract(event_json, '$.data.tools')),
 json_extract(event_json, '$.data.aborted'), json_extract(event_json, '$.data.timedOut'),
 coalesce(json_extract(event_json, '$.data.promptError') NOT IN ('', 0), 0)
FROM trajectory_runtime_events
WHERE json_extract(event_json, '$.type') IN ('model.completed', 'tool.call', 'context.compiled')
ORDER BY session_id, seq
"""


def nonnegative(value):
    return (
        max(0, value)
        if isinstance(value, int) and not isinstance(value, bool)
        else 0
    )


def collect(path):
    """Return one summary per recorded run; completion usage is already aggregated across its steps."""
    agent = path.parent.parent.name
    runs = {}
    with sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10
    ) as source:
        source.execute("PRAGMA query_only=ON")
        for (
            sid,
            rid,
            _seq,
            kind,
            timestamp,
            provider,
            model,
            key,
            raw_usage,
            tool,
            catalog,
            aborted,
            timed_out,
            error,
        ) in source.execute(QUERY):
            if not rid or not (provider or "").startswith("openai"):
                continue
            identity = hashlib.sha256(
                f"{agent}\0{sid}\0{rid}".encode()
            ).hexdigest()
            r = runs.setdefault(
                identity,
                {
                    "id": identity,
                    "agent": agent,
                    "model": model,
                    "tools": Counter(),
                    "tool_catalog_size": None,
                    "workload": "interactive/other",
                },
            )
            if ":cron:" in (key or ""):
                r["workload"] = "scheduled"
            if kind == "tool.call":
                r["tools"][tool or "unknown"] += 1
            elif kind == "context.compiled":
                r["tool_catalog_size"] = catalog
            else:
                # Use the last completion for this run, never add it to transcript/snapshot totals.
                usage = json.loads(raw_usage) if raw_usage else None
                r.update(
                    at=timestamp,
                    usage_known=isinstance(usage, dict),
                    aborted=bool(aborted),
                    timed_out=bool(timed_out),
                    prompt_error=bool(error),
                )
                r["tokens"] = {
                    k: nonnegative((usage or {}).get(k)) for k in COUNTERS
                }
                ctx = (usage or {}).get("contextUsage") or {}
                r["context_tokens"] = (
                    nonnegative(ctx.get("promptTokens"))
                    if ctx.get("state") == "available"
                    else None
                )
    completed = []
    for r in runs.values():
        if "at" not in r:
            continue  # Running turns enter the ledger once completion metadata is available.
        if r["workload"] != "scheduled" and r["tools"].get("heartbeat_respond"):
            r["workload"] = "heartbeat"
        r["tool_calls"] = sum(r["tools"].values())
        r["day"] = (
            dt.datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
            .astimezone(TZ)
            .date()
            .isoformat()
        )
        completed.append(r)
    return completed


def aggregate(rows):
    out = {
        k: sum(r["tokens"][k] for r in rows if r["usage_known"])
        for k in COUNTERS
    }
    out.update(
        turns=len(rows),
        measured_turns=sum(r["usage_known"] for r in rows),
        missing_usage_turns=sum(not r["usage_known"] for r in rows),
        tool_calls=sum(r["tool_calls"] for r in rows),
        prompt_errors=sum(r["prompt_error"] for r in rows),
    )
    # OpenClaw normalizes these fields as disjoint counters (unlike raw OpenAI API input_tokens).
    out["all_input"] = out["input"] + out["cacheRead"] + out["cacheWrite"]
    out["cache_hit_percent"] = (
        round(100 * out["cacheRead"] / out["all_input"], 2)
        if out["all_input"]
        else None
    )
    out["mean_tool_calls"] = (
        round(out["tool_calls"] / len(rows), 2) if rows else 0
    )
    return out


def make_report(rows, day, change_at=None):
    rows = sorted((r for r in rows if r["day"] == day), key=lambda r: r["at"])
    workloads = defaultdict(list)
    models = defaultdict(list)
    hours = defaultdict(list)
    phases = defaultdict(list)
    tools = Counter()
    for r in rows:
        when = dt.datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
        workloads[r["workload"]].append(r)
        models[r["model"]].append(r)
        hours[when.astimezone(TZ).strftime("%H:00")].append(r)
        tools.update(r["tools"])
        if change_at:
            phases["before" if when < change_at else "after"].append(r)
    return {
        "date": day,
        "timezone": str(TZ),
        "updated_at": dt.datetime.now(TZ).isoformat(),
        "scope": "Recorded OpenClaw OpenAI completions only; excludes Codex CLI and local Ollama. Not billing or remaining quota.",
        "notes": [
            "Input is uncached; cacheRead/cacheWrite are separate OpenClaw counters.",
            "Reasoning is included in output: do not add it again.",
            "Missing usage is unknown, not zero; running turns are not yet counted.",
            "Before/after tasks differ; this is not a controlled savings experiment.",
        ],
        "totals": aggregate(rows),
        "workloads": {k: aggregate(v) for k, v in workloads.items()},
        "models": {k: aggregate(v) for k, v in models.items()},
        "hours": {k: aggregate(v) for k, v in hours.items()},
        "comparison_at": change_at.isoformat() if change_at else None,
        "comparison": {k: aggregate(v) for k, v in phases.items()},
        "tools": dict(tools.most_common()),
        "largest_turns": sorted(
            rows, key=lambda r: r["tokens"]["total"], reverse=True
        )[:10],
        "recent_turns": rows[-10:],
    }


def markdown(report):
    t = report["totals"]
    lines = [
        f"# OpenClaw usage — {report['date']}",
        "",
        f"Updated: {report['updated_at']}",
        "",
        report["scope"],
        "",
        "| Metric | Count |",
        "|---|---:|",
        f"| Completed runs with usage | {t['measured_turns']:,} |",
        f"| Completed runs missing usage | {t['missing_usage_turns']:,} |",
        f"| Uncached input tokens | {t['input']:,} |",
        f"| Cached input tokens | {t['cacheRead']:,} |",
        f"| Cache-write tokens | {t['cacheWrite']:,} |",
        f"| Output tokens | {t['output']:,} |",
        f"| Reasoning tokens (included in output) | {t['reasoningTokens']:,} |",
        f"| Input cache hit rate | {t['cache_hit_percent']}% |",
        f"| Tool calls | {t['tool_calls']:,} |",
        "",
        "## By workload",
        "",
        "| Workload | Measured runs | Uncached input | Cached input | Output | Tool calls |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, v in report["workloads"].items():
        lines.append(
            f"| {name} | {v['measured_turns']} | {v['input']:,} | {v['cacheRead']:,} | {v['output']:,} | {v['tool_calls']} |"
        )
    lines += [
        "",
        "## Recent runs",
        "",
        "| Time | Workload | Model | Uncached | Cached | Output | Tools | Catalog size |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in report["recent_turns"]:
        at = (
            dt.datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
            .astimezone(TZ)
            .strftime("%H:%M:%S")
        )
        v = r["tokens"]
        counts = [
            f"{v[k]:,}" if r["usage_known"] else "unknown"
            for k in ("input", "cacheRead", "output")
        ]
        lines.append(
            f"| {at} | {r['workload']} | {r['model']} | {' | '.join(counts)} | {r['tool_calls']} | {r['tool_catalog_size']} |"
        )
    lines += ["", "## Interpretation", ""] + [f"- {n}" for n in report["notes"]]
    return "\n".join(lines) + "\n"


def write_private(path, text):
    fd, tmp = tempfile.mkstemp(prefix=".usage-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run_tokens(r):
    t = r["tokens"]
    return t["input"] + t["cacheRead"] + t["cacheWrite"] + t["output"]


def window_usage(rows, now, minutes):
    """Tokens and runs completed within the last `minutes` (rows from the ledger, any day)."""
    cutoff = now - dt.timedelta(minutes=minutes)
    recent = [
        r
        for r in rows
        if r.get("usage_known")
        and r.get("at")
        and cutoff
        <= dt.datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
        <= now
    ]
    return sum(run_tokens(r) for r in recent), len(recent)


def docker(*args):
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,  # callers act on the return code
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        return -1


def alert(text):
    """Telegram via the watchdog's sender (token read on the host, never printed)."""
    try:
        spec = importlib.util.spec_from_file_location(
            "watchdog", Path(__file__).with_name("watchdog.py")
        )
        wd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wd)
        wd.send(text, dry=False)
    except Exception as e:  # noqa: BLE001
        print(f"alert failed: {type(e).__name__}")


def latch(reason, **info):
    write_private(
        BREAKER,
        json.dumps(
            {
                "at": dt.datetime.now(TZ).isoformat(timespec="seconds"),
                "reason": reason,
                **info,
            },
            indent=2,
        )
        + "\n",
    )


def check_breaker(
    rows,
    now,
    minutes=BREAKER_MINUTES,
    limit=BREAKER_TOKENS,
    *,
    stop=docker,
    notify=alert,
):
    """Trip once: stop the container, latch, alert. Returns True if it tripped on this call."""
    if BREAKER.exists():
        return False
    used, runs = window_usage(rows, now, minutes)
    if used <= limit:
        return False
    stopped = stop("stop", CONTAINER) == 0
    latch(
        "usage ceiling",
        window_minutes=minutes,
        tokens=used,
        limit=limit,
        runs=runs,
        container_stopped=stopped,
    )
    notify(
        f"⛔ dingding paused: OpenAI usage reached {used:,} tokens in the last {minutes} min "
        f"({runs} runs; limit {limit:,}). Telegram replies and briefs are off"
        + (
            ""
            if stopped
            else " — but stopping the container FAILED, check the laptop"
        )
        + ". To resume, on the laptop: cd ~/dingding && .venv/bin/python ops/usage_monitor.py --resume"
    )
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--state", type=Path, default=Path.home() / "dingding/docker/state"
    )
    p.add_argument("--output", type=Path, default=ROOT / "data/usage")
    p.add_argument("--date", default=dt.datetime.now(TZ).date().isoformat())
    p.add_argument(
        "--change-at",
        help="Timezone-qualified deployment timestamp for descriptive before/after totals",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="clear the breaker latch and start OpenClaw again",
    )
    p.add_argument(
        "--pause",
        metavar="REASON",
        help="stop OpenClaw and latch it stopped (watchdog stays quiet)",
    )
    p.add_argument(
        "--no-breaker",
        action="store_true",
        help="report only; never stop the container",
    )
    a = p.parse_args()
    if a.resume:
        BREAKER.unlink(missing_ok=True)
        rc = docker("start", CONTAINER)
        print(
            "breaker cleared; container start",
            "ok" if rc == 0 else f"FAILED ({rc})",
        )
        return
    if a.pause:
        os.umask(0o077)
        BREAKER.parent.mkdir(parents=True, exist_ok=True)
        latch(a.pause, container_stopped=docker("stop", CONTAINER) == 0)
        print(BREAKER.read_text())
        return
    dt.date.fromisoformat(a.date)
    change = dt.datetime.fromisoformat(a.change_at) if a.change_at else None
    if change and change.tzinfo is None:
        p.error("--change-at must include a timezone")
    sources = sorted(a.state.glob("agents/*/agent/openclaw-agent.sqlite"))
    if not sources:
        p.error("no agent usage databases found")
    # Read every source successfully before publishing a fresh report.
    rows = [row for source in sources for row in collect(source)]
    os.umask(0o077)
    a.output.mkdir(parents=True, exist_ok=True)
    os.chmod(a.output, 0o700)
    with sqlite3.connect(a.output / "usage.sqlite") as ledger:
        ledger.execute(
            "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, day TEXT NOT NULL, metadata TEXT NOT NULL)"
        )
        ledger.executemany(
            "INSERT OR REPLACE INTO runs VALUES (?, ?, ?)",
            [(r["id"], r["day"], json.dumps(r)) for r in rows],
        )
        ledger.commit()
        stored = [
            json.loads(r[0])
            for r in ledger.execute(
                "SELECT metadata FROM runs WHERE day=?", (a.date,)
            )
        ]
        yesterday = (
            dt.date.fromisoformat(a.date) - dt.timedelta(days=1)
        ).isoformat()
        window_rows = [
            json.loads(r[0])
            for r in ledger.execute(
                "SELECT metadata FROM runs WHERE day IN (?, ?)",
                (a.date, yesterday),
            )
        ]
    if not a.no_breaker:
        check_breaker(window_rows, dt.datetime.now(dt.UTC))
    report = make_report(stored, a.date, change)
    payload = json.dumps(report, indent=2) + "\n"
    for name, text in [
        (a.date + ".json", payload),
        (a.date + ".md", markdown(report)),
        ("latest.json", payload),
        ("latest.md", markdown(report)),
    ]:
        write_private(a.output / name, text)
    print(
        json.dumps(
            {
                "date": a.date,
                "report": str(a.output / "latest.md"),
                "totals": report["totals"],
            }
        )
    )


if __name__ == "__main__":
    main()
