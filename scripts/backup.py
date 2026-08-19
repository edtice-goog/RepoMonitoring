"""Back up the two authoritative stores to a synced folder (default: OneDrive).

The system's entire recoverable state is Postgres (seeds, provenance, cursors,
render cache) + redis (pipeline cache + triage verdicts — a few million tokens
of Claude output). Both run user-owned inside WSL2 (infra/stack.sh), so this
script shells into WSL for the dump tools and has them write DIRECTLY to the
Windows destination via /mnt/c — no intermediate copies.

    python scripts/backup.py [--dest DIR] [--keep 14]

Default destination: %OneDrive%\\RepoMonitoringBackups (cloud-synced = offsite).
Each run makes a timestamped subfolder with:
    repomon.pgdump      pg_dump custom format (pg_restore-able)
    redis-repomon.rdb   point-in-time RDB streamed via redis-cli --rdb
    manifest.json       row/key counts + sizes, for at-a-glance verification
and prunes to the newest --keep runs. Credentials (blackduck.local*.json,
bd-credentials.local.json) are NOT backed up — they are revocable secrets;
regenerate rather than replicate them to cloud storage.

Restore (documented here so it exists somewhere):
    wsl bash -lc 'PG=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1); \\
        "$PG/pg_restore" -h 127.0.0.1 -p 5544 -U repomon -d repomon \\
        --clean --if-exists "/mnt/c/<backup>/repomon.pgdump"'
    # redis: stop redis (infra/stack.sh down), copy the .rdb over
    # ~/repomon/redisdata/dump.rdb, remove appendonlydir/, start the stack.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PGPORT, REDISPORT = "5544", "6380"
PGUSER, PGDB = "repomon", "repomon"


def wsl(cmd: str) -> str:
    """Run one bash command inside WSL, return stdout (raises on failure)."""
    r = subprocess.run(["wsl", "-e", "bash", "-lc", cmd],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"wsl command failed: {cmd}\n{r.stderr.strip()}")
    return r.stdout.strip()


def to_mnt(win_path: Path) -> str:
    """C:\\x\\y -> /mnt/c/x/y (what the WSL-side tools write to)."""
    p = str(win_path.resolve()).replace("\\", "/")
    drive, rest = p[0].lower(), p[2:]
    return f"/mnt/{drive}{rest}"


def default_dest() -> Path:
    od = os.environ.get("OneDrive")
    if not od:
        sys.exit("no %OneDrive% environment variable — pass --dest explicitly")
    return Path(od) / "RepoMonitoringBackups"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", type=Path, default=None,
                    help="backup root (default: %%OneDrive%%\\RepoMonitoringBackups)")
    ap.add_argument("--keep", type=int, default=14,
                    help="prune to the newest N runs (default %(default)s)")
    args = ap.parse_args()

    root = args.dest or default_dest()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    dest = root / f"repomon-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    mnt = to_mnt(dest)

    print(f"[backup] -> {dest}")
    pgbin = 'PG=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)'

    # Postgres: custom-format dump straight onto the OneDrive mount.
    wsl(f'{pgbin}; "$PG/pg_dump" -h 127.0.0.1 -p {PGPORT} -U {PGUSER} -Fc '
        f'-f "{mnt}/repomon.pgdump" {PGDB}')
    # Redis: point-in-time RDB streamed over the socket (no server files touched).
    wsl(f'redis-cli -p {REDISPORT} --rdb "{mnt}/redis-repomon.rdb" >/dev/null')

    # Verification counts for the manifest — a backup you can't sanity-check
    # at a glance is a backup you won't trust during a restore.
    counts = {}
    try:
        q = ("SELECT (SELECT count(*) FROM project), (SELECT count(*) FROM capture_file), "
             "(SELECT count(*) FROM rendered_event), (SELECT count(*) FROM event_cursor)")
        row = wsl(f'{pgbin}; "$PG/psql" -h 127.0.0.1 -p {PGPORT} -U {PGUSER} '
                  f'-d {PGDB} -tAc "{q}"')
        pr, cf, re_, ec = row.split("|")
        counts["postgres"] = {"projects": int(pr), "capture_files": int(cf),
                              "rendered_events": int(re_), "event_cursors": int(ec)}
        counts["redis"] = {
            "triage_verdicts": int(wsl(
                f"redis-cli -p {REDISPORT} --scan --pattern 'repomon:triage:*' | wc -l")),
            "total_keys": int(wsl(f"redis-cli -p {REDISPORT} dbsize").split()[-1]),
        }
    except Exception as exc:                      # counts are advisory, never fatal
        counts["warning"] = f"count collection failed: {exc}"

    files = {f.name: f.stat().st_size for f in dest.iterdir() if f.is_file()}
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(),
                "files": files, "counts": counts}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for name, size in files.items():
        print(f"[backup]   {name}: {size:,} bytes")
    print(f"[backup]   counts: {json.dumps(counts)}")

    # Retention: newest --keep runs stay.
    runs = sorted((d for d in root.glob("repomon-*") if d.is_dir()), reverse=True)
    for old in runs[args.keep:]:
        import shutil
        shutil.rmtree(old, ignore_errors=True)
        print(f"[backup]   pruned {old.name}")
    print(f"[backup] done ({min(len(runs), args.keep)} run(s) retained)")


if __name__ == "__main__":
    main()
