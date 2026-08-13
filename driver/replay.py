"""Webhook replay driver for the RepoMonitoring demo.

Reads the simulated upstream commits from samples/commit-events.json,
converts each to a standard Git webhook payload (GitHub push-event format),
and POSTs it to the monitoring component's /webhook endpoint.

Modes:
    python replay.py                  interactive — press Enter to fire each
                                      event in order (the demo mode)
    python replay.py --event evt-003  fire one event by id
    python replay.py --all            fire all events, no prompting
    python replay.py --list           show available events
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_EVENTS = Path(__file__).resolve().parent.parent / "samples" / "commit-events.json"
DEFAULT_WEBHOOK = "http://127.0.0.1:8378/webhook"


def to_push_payload(event: dict) -> dict:
    """Convert a sample commit event to a GitHub push-event webhook payload."""
    return {
        "ref": f"refs/heads/{event['branch']}",
        "after": event["commit"],
        "repository": {"clone_url": event["vcs_url"]},
        "commits": [
            {
                "id": event["commit"],
                "message": event.get("message", ""),
                "timestamp": event.get("committed_at", ""),
                "added": [],
                "removed": [],
                "modified": event["files_changed"],
            }
        ],
    }


def send(webhook_url: str, event: dict) -> None:
    payload = to_push_payload(event)
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-GitHub-Event": "push"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  !! webhook delivery failed: {exc}")
        return
    for c in result.get("commits", []):
        verdict = f" verdict={c['verdict']}" if c.get("verdict") else ""
        print(f"  -> commit {c['commit'][:12]}: {c['status']}{verdict}")
    if result.get("status") == "ignored":
        print(f"  -> ignored: {result.get('reason')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--webhook", default=DEFAULT_WEBHOOK)
    parser.add_argument("--event", help="fire a single event by id (e.g. evt-001)")
    parser.add_argument("--all", action="store_true", help="fire all events without prompting")
    parser.add_argument("--list", action="store_true", help="list available events")
    args = parser.parse_args()

    events = json.loads(args.events.read_text(encoding="utf-8-sig"))["events"]

    if args.list:
        for e in events:
            print(f"{e['id']}: {e['scenario']}")
        return

    if args.event:
        selected = [e for e in events if e["id"] == args.event]
        if not selected:
            sys.exit(f"no such event: {args.event} (use --list)")
    else:
        selected = events

    interactive = not args.all and not args.event and sys.stdin.isatty()
    for e in selected:
        print(f"\n[{e['id']}] {e['scenario']}")
        print(f"  repo:   {e['vcs_url']} ({e['branch']})")
        print(f"  commit: {e['commit'][:12]}  \"{e.get('message', '')}\"")
        print(f"  files:  {', '.join(e['files_changed'])}")
        if interactive:
            input("  press Enter to deliver this webhook... ")
        send(args.webhook, e)
    print("\ndone.")


if __name__ == "__main__":
    main()
