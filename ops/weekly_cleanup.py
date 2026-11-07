"""Sunday mail cleanup: runs on the laptop (not the AI), for all three accounts, then reports on Telegram.

  Spam                     -> Trash (any age)
  Promotions, Social tabs  -> Trash if older than 7 days (never starred / important; Primary inbox untouched)

Trash is recoverable for 30 days. Undo from Telegram: "undo the Sunday cleanup" (ia mail-log / ia undo-mail).
  python weekly_cleanup.py            # run + report
  python weekly_cleanup.py --dry-run  # preview only, nothing changes, nothing sent
"""

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault(
    "BROKER_SOCK", str(Path.home() / "dingding/docker/broker-io/broker.sock")
)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "broker" / "client"))
sys.path.insert(0, str(HERE))
from brokerclient import call  # noqa: E402
from watchdog import send  # noqa: E402

RULES = [  # (areas, older_than_days)
    (["spam"], 0),
    (["promotions", "social"], 7),
]
MAX_ROUNDS = 3  # 2,000 emails per round -> up to 6,000 per rule (first run may have a backlog)


def _run_rule(areas, days, *, dry_run, totals, batches, problems):
    """Previews and trashes one rule's mail, a plan (<= 2,000 emails) at a time.

    Args:
        areas: Gmail areas for this rule, e.g. ["spam"].
        days: Only mail older than this many days (0 = any age).
        dry_run: Print the first preview instead of trashing anything.
        totals: Per-account, per-area counts; updated in place.
        batches: Batch ids of carried-out plans; appended to.
        problems: Human-readable failures; appended to.

    Returns:
        Whether mail was left over after MAX_ROUNDS plans.
    """
    for _ in range(1 if dry_run else MAX_ROUNDS):
        plan = call(
            "mail.cleanup", areas=areas, older_than_days=days, action="trash"
        )
        if not plan.get("total"):
            return False
        if dry_run:
            print(
                f"would trash {plan['total']} "
                f"(+{plan['more_not_included']} more): {plan['by_account']}  "
                f"top: {', '.join(plan['top_senders_sample'][:5])}"
            )
            return False
        r = call("mail.cleanup", confirm=plan["plan_id"], source="sunday")
        batches.append(r["batch_id"])
        problems += [
            f"{f['account']} {f['area']}: {f['error'][:80]}"
            for f in r["failed"]
        ]
        for acct, areas_n in r["by_account"].items():
            for area, n in areas_n.items():
                totals.setdefault(acct, {}).setdefault(area, 0)
                totals[acct][area] += n
        if not plan["more_not_included"]:
            return False
    return not dry_run


def _summary(totals, leftover, problems):
    """Returns the Telegram summary of a cleanup run."""
    if totals:
        lines = [
            f"• {acct}: "
            + ", ".join(f"{n} {area}" for area, n in sorted(v.items()))
            for acct, v in sorted(totals.items())
        ]
        n = sum(sum(v.values()) for v in totals.values())
        msg = (
            f"🧹 Sunday cleanup: moved {n} emails to Trash\n"
            + "\n".join(lines)
            + "\n(spam, plus promotions/social older than 7 days; "
            "starred & important kept)\n"
            "Recoverable for 30 days. "
            'Say "undo the Sunday cleanup" to bring them back.'
        )
    else:
        msg = "🧹 Sunday cleanup: nothing to clean this week."
    if leftover:
        msg += (
            "\n(There's more old mail than one run handles; "
            "the rest goes next Sunday.)"
        )
    if problems:
        msg += "\n⚠️ Problems: " + "; ".join(problems)[:500]
    return msg


def main():
    """Runs the weekly cleanup and sends the summary to Telegram."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    totals, batches, problems = {}, [], []
    leftover = False
    try:
        for areas, days in RULES:
            if _run_rule(
                areas,
                days,
                dry_run=a.dry_run,
                totals=totals,
                batches=batches,
                problems=problems,
            ):
                leftover = True
    except SystemExit as e:  # brokerclient exits with the broker's refusal
        if "daily cleanup limit" in str(e.code):
            leftover = True
        else:
            problems.append(str(e.code))
    if a.dry_run:
        return
    msg = _summary(totals, leftover, problems)
    print(msg)
    send(msg, dry=False)


if __name__ == "__main__":
    main()
