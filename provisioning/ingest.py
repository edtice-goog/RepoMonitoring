"""One-time seed ingest — run per RELEASE build, while the checkouts + Coverity
idir still exist. Writes the three can't-recreate tables to Postgres:

  project            - the BD SCA project link + identity
  capture/capture_file - the distilled compiled-file set (parse_emit)
  source_provenance  - the .git-discovery result (actual source -> ground truth)

Everything else the dashboard needs is recreatable from these seeds + the Redis
cache (see provisioning/recreate.py), so after this runs the local build can be
deleted. This is the ONLY step that touches the ephemeral local artifacts.

    python provisioning/ingest.py --project repo-mon-stage3-curl --version 8.11.0
                                  [--emit <cov_emit_links.json>] [--bd-url <link>] [--replace]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from db.models import Capture, CaptureFile, Project, SourceProvenance  # noqa: E402
from db.session import SessionLocal                                    # noqa: E402
from attribute_capture import DEFAULT_EMIT, HDR_EXT, discover_git, parse_emit  # noqa: E402
from bd_scout import load_config                                       # noqa: E402
from gh_replay import GH, gh_token                                     # noqa: E402


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
    ap.add_argument("--bd-url", default=None, help="BD SCA project link (default: config url)")
    ap.add_argument("--override", action="append", default=[], metavar="COMPONENT=VERSION",
                    help="monitor a component at a different release than BD scanned "
                         "(e.g. --override curl=8.21.0); repeatable")
    ap.add_argument("--replace", action="store_true", help="re-ingest, replacing any existing rows")
    args = ap.parse_args()

    cfg = load_config()
    bd_url = args.bd_url or cfg.get("url", "")
    overrides = dict(kv.split("=", 1) for kv in args.override) or None

    primaries, all_compiled = parse_emit(args.emit)
    print(f"[emit] {len(primaries)} primary TUs, {len(all_compiled)} compiled files", flush=True)

    # .git discovery is the only external touch here (fork-parent resolution).
    gh = GH(gh_token())
    _, checkouts = discover_git(gh, all_compiled)
    print(f"[git] {len(checkouts)} checkout(s) discovered", flush=True)

    prim = set(primaries)
    with SessionLocal() as s:
        existing = s.query(Project).filter_by(name=args.project).one_or_none()
        if existing:
            if not args.replace:
                sys.exit(f"project {args.project!r} already ingested — pass --replace to overwrite")
            s.delete(existing)
            s.flush()

        proj = Project(name=args.project, bd_project_url=bd_url, bd_version=args.version,
                       overrides=overrides)
        s.add(proj)
        s.flush()

        cap = Capture(project_id=proj.id, build_id=f"{args.project}@{args.version}")
        s.add(cap)
        s.flush()
        s.add_all([
            CaptureFile(capture_id=cap.id, path=p, is_primary=(p in prim),
                        kind="header" if p.lower().endswith(HDR_EXT) else "source")
            for p in sorted(all_compiled)
        ])

        for info in checkouts.values():
            s.add(SourceProvenance(
                project_id=proj.id,
                ground_truth_url=info["ground_truth_url"],
                actual_source_url=info["actual_source_url"],
                actual_source_ref=info["actual_source_ref"],
                divergent=info["divergent"]))
            flag = " DIVERGENT" if info["divergent"] else ""
            print(f"    provenance: {info['actual_source_url']}@{info['actual_source_ref']} "
                  f"-> {info['ground_truth_url']}{flag}", flush=True)

        s.commit()
        print(f"[ingest] project={args.project} id={proj.id}: "
              f"{len(all_compiled)} capture files, {len(checkouts)} provenance rows", flush=True)


if __name__ == "__main__":
    main()
