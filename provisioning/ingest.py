"""Ingestion CLIENT — runs on the build box, right after a RELEASE build, while the
Coverity emit + the .git checkouts still exist. It collects the two purely-local
artifacts and POSTs them to the monitor's ingestion API, which persists the seeds to
Postgres. The build box needs NO database access, NO GitHub token, and none of the
monitor's heavy dependencies — just Python stdlib + the `git` binary.

    python provisioning/ingest.py --project repo-mon-stage3-curl --version 8.21.0 \
        --monitor-url http://<monitor-host>:8378 [--emit <cov_emit_links.json>] \
        [--bd-url <BD SCA project link>] [--replace]

What gets sent (never the SBoM — that's reloaded from the BD link and cached):
  * files     - the compiled file set from cov_emit_links.json (path/is_primary/kind)
  * checkouts - per-.git origin URL + exact ref built; the server resolves each to its
                canonical upstream (fork-parent) and persists source_provenance.

--direct-db is a co-located DEV shortcut that skips the API and writes Postgres straight
from here (pulls in the server deps + needs a GitHub token); production uses the API.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from emit_local import (DEFAULT_EMIT, collect_checkouts_local,  # noqa: E402
                        files_payload, parse_emit)


def build_payload(project, version, emit, bd_url="", replace=False, reset_feed=False):
    """Collect the local artifacts into the ingestion payload. No network, no DB."""
    primaries, all_compiled = parse_emit(emit)
    return {
        "schema": 1,
        "project": project,
        "version": version,
        "bd_url": bd_url or "",
        "replace": bool(replace),
        "reset_feed": bool(reset_feed),
        "files": files_payload(primaries, all_compiled),
        "checkouts": collect_checkouts_local(all_compiled),
    }


def post_ingest(monitor_url, payload, timeout=180):
    """POST the payload to the monitor's ingestion API; return (status, body_dict)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        monitor_url.rstrip("/") + "/projects/ingest", data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {"error": str(e)}


def _default_bd_url():
    """Best-effort BD SCA link from a local blackduck.local.json, if present (plain JSON
    read — no bd_scout import, so the client stays dependency-light)."""
    try:
        return json.loads((REPO_ROOT / "blackduck.local.json")
                          .read_text(encoding="utf-8")).get("url", "")
    except Exception:
        return ""


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--emit", type=Path, default=DEFAULT_EMIT)
    ap.add_argument("--bd-url", default=None,
                    help="BD SCA server/project link (default: url from local "
                         "blackduck.local.json). Pass an empty string for a project "
                         "with no BD SCA association — valid; the server then builds "
                         "the watch set from compiled files + .git provenance alone.")
    ap.add_argument("--monitor-url", default="http://127.0.0.1:8378",
                    help="the monitor's ingestion API base (default: %(default)s)")
    ap.add_argument("--replace", action="store_true", help="overwrite an existing project")
    ap.add_argument("--reset-feed", action="store_true",
                    help="clear the durable event feed + cursors on the server (use when "
                         "RE-BASING an existing project to a new release)")
    ap.add_argument("--direct-db", action="store_true",
                    help="DEV: skip the API and persist straight to Postgres from here "
                         "(needs the server deps + a GitHub token; use only co-located)")
    args = ap.parse_args()

    if not args.emit.exists():
        sys.exit(f"emit not found: {args.emit} (pass --emit)")
    bd_url = args.bd_url if args.bd_url is not None else _default_bd_url()
    payload = build_payload(args.project, args.version, args.emit, bd_url,
                            args.replace, args.reset_feed)
    print(f"[client] {len(payload['files'])} files, {len(payload['checkouts'])} checkout(s) "
          f"for {args.project}@{args.version}", flush=True)
    for c in payload["checkouts"]:
        print(f"    checkout {c['actual_source_url']} @ {c['actual_source_ref']}", flush=True)

    if args.direct_db:
        sys.path.insert(0, str(REPO_ROOT))
        from provisioning.ingest_service import persist_ingest
        from gh_replay import GH, gh_token
        print("[client] --direct-db: persisting locally (no API)", flush=True)
        print(json.dumps(persist_ingest(payload, GH(gh_token())), indent=2))
        return

    status, body = post_ingest(args.monitor_url, payload)
    print(f"[client] POST {args.monitor_url.rstrip('/')}/projects/ingest -> {status}", flush=True)
    print(json.dumps(body, indent=2))
    if status >= 400:
        sys.exit(1)


if __name__ == "__main__":
    main()
