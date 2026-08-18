"""Truncate all Postgres tables for a fresh start, WITHOUT touching the caches.

Clears the seed + operational tables (project / capture / capture_file /
source_provenance / event_cursor). Leaves the Redis cache, the local git clones,
and the triage-cache intact — so a re-ingest + recreate reuses them and stays cheap.

    python provisioning/reset_db.py --yes
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text          # noqa: E402
from db.session import engine        # noqa: E402

TABLES = ["rendered_event", "event_cursor", "source_provenance", "capture_file", "capture", "project"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--yes", action="store_true", help="required: confirm the truncation")
    args = ap.parse_args()
    if not args.yes:
        sys.exit("refusing to truncate without --yes")
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"))
    print(f"truncated: {', '.join(TABLES)} — caches (Redis, clones, triage-cache) untouched")


if __name__ == "__main__":
    main()
