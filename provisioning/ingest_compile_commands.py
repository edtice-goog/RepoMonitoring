"""Ingest a compile_commands.json (CMake/clang compilation database) into the monitor.

For when the build itself is out of reach — e.g. someone EMAILS you a
compile_commands.json alongside an SBoM you don't trust. There is no Coverity
emit, no .git checkout on disk, and usually no Black Duck project; the
compilation database still names every translation unit the build compiles and
every include directory it searches, and that is enough for the server's
Claude-from-files reconstruction to identify the third-party components and
build the watch set.

Honest expectations for this mode: versions are Claude best-estimates (no KB
identity, no exact built ref), there is no built-from provenance, and
components that are neither compiled nor named by an include path are
invisible. The include directories ride along as header-only evidence (a path
like .../middleware/mbedtls3x/include identifies a component even when none of
its sources compile).

    python provisioning/ingest_compile_commands.py --project sbom-check --version 1.0 \
        --ccdb <path>/compile_commands.json --monitor-url http://<monitor>:8378 \
        [--bd-url https://<bd-server>/] [--dump]

--bd-url defaults to EMPTY (no BD SCA association) — the typical emailed-SBoM
case. Pass a server explicitly if a BD project exists for this build.
Pure stdlib, like ingest.py: usable anywhere Python runs.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest import post_ingest  # noqa: E402

SRC_EXT = (".c", ".cc", ".cpp", ".cxx")


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/")


def _is_scaffolding(path: str) -> bool:
    p = path.lower()
    return "/cmakefiles/" in p or "/compilerid" in p


def _is_abs(p: str) -> bool:
    return p.startswith("/") or (len(p) > 1 and p[1] == ":")


def _resolve(base: str, p: str) -> str:
    p = _norm(p)
    return p if _is_abs(p) or not base else f"{base.rstrip('/')}/{p}"


def _argv(entry: dict) -> list:
    if entry.get("arguments"):
        return list(entry["arguments"])
    # Plain split is lossy for quoted paths-with-spaces but fine for -I harvesting;
    # TU paths come from the structured "file" field, never from here.
    return (entry.get("command") or "").split()


def parse_ccdb(path: Path):
    """(translation_units, include_dirs) from a compilation database. TUs come from
    the structured 'file' fields; include dirs from -I/-isystem/-iquote//I flags,
    resolved against each entry's 'directory'."""
    db = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    tus, incdirs = set(), set()
    for entry in db:
        base = _norm(entry.get("directory") or "")
        f = _resolve(base, entry.get("file") or "")
        if f.lower().endswith(SRC_EXT) and not _is_scaffolding(f):
            tus.add(f)
        args = _argv(entry)
        i = 0
        while i < len(args):
            a, d = args[i], None
            if a in ("-I", "-isystem", "-iquote", "--include-directory") and i + 1 < len(args):
                d, i = args[i + 1], i + 1
            elif a.startswith(("-I", "/I")) and len(a) > 2:
                d = a[2:]
            elif a.startswith("-isystem") and len(a) > 8:
                d = a[8:]
            if d:
                d = _resolve(base, d.strip('"'))
                if not _is_scaffolding(d):
                    incdirs.add(d)
            i += 1
    return sorted(tus), sorted(incdirs)


def build_payload(project, version, ccdb, bd_url="", replace=False, reset_feed=False):
    tus, incdirs = parse_ccdb(ccdb)
    files = [{"path": p, "is_primary": True, "kind": "source"} for p in tus]
    # Include directories as header-only evidence: they feed the reconstruction's
    # included-header channel and (correctly) never make a component monitored.
    files += [{"path": d, "is_primary": False, "kind": "header"} for d in incdirs]
    return {
        "schema": 1,
        "project": project,
        "version": version,
        "bd_url": bd_url or "",
        "replace": bool(replace),
        "reset_feed": bool(reset_feed),
        "files": files,
        "checkouts": [],   # no build tree on this machine — no provenance to collect
    }


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--version", required=True,
                    help="version label for the build (Claude estimates per-component versions)")
    ap.add_argument("--ccdb", type=Path, required=True, help="path to compile_commands.json")
    ap.add_argument("--bd-url", default="",
                    help="BD SCA server/project link if one exists (default: empty = "
                         "no BD association; valid — the watch set comes from the "
                         "compilation database alone)")
    ap.add_argument("--monitor-url", default="http://127.0.0.1:8378")
    ap.add_argument("--replace", action="store_true", help="overwrite an existing project")
    ap.add_argument("--reset-feed", action="store_true")
    ap.add_argument("--dump", action="store_true",
                    help="print the payload instead of POSTing (inspection/handoff)")
    args = ap.parse_args()

    if not args.ccdb.exists():
        sys.exit(f"compilation database not found: {args.ccdb}")
    payload = build_payload(args.project, args.version, args.ccdb, args.bd_url,
                            args.replace, args.reset_feed)
    n_src = sum(1 for f in payload["files"] if f["is_primary"])
    print(f"[client] {n_src} translation units, "
          f"{len(payload['files']) - n_src} include dir(s) for "
          f"{args.project}@{args.version}", flush=True)
    if args.dump:
        print(json.dumps(payload, indent=2))
        return
    status, body = post_ingest(args.monitor_url, payload)
    print(f"[client] POST {args.monitor_url.rstrip('/')}/projects/ingest -> {status}", flush=True)
    print(json.dumps(body, indent=2))
    if status >= 400:
        sys.exit(1)


if __name__ == "__main__":
    main()
