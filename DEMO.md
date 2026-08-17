# RepoMonitoring — Demo Instructions

A patch-gap monitor for embedded builds: it watches the upstream repos that feed a
build and triages each new commit as "quiet security fix" vs. routine. The demo runs
on a **real** captured project (cURL 8.11 + zlib 1.3.1 from source, linking OpenSSL
3.6.3) — there is no fictitious data.

## Architecture

| Component | What it is |
|---|---|
| `monitor/app.py` | Multi-project web dashboard + Git webhook endpoint + **Recreate** button |
| `triage-service/claude_server.py` | LLM triage (Claude) with a persistent cache; `--cache-only` for keyless replay |
| `driver/replay.py` | Fires saved upstream commits at the monitor as standard Git webhooks |
| `provisioning/ingest.py` | **Once per release:** writes the can't-recreate seeds to Postgres |
| `provisioning/recreate.py` | Rebuilds a project's dashboard artifacts from seeds + cache (0 external calls warm) |
| `db/` + `alembic/` | Postgres schema (SQLAlchemy) — the 3 seed tables |
| `services/cache.py` | Redis cache: every external result stored once, keyed by input |
| `infra/stack.sh` + `infra/redis.conf` | User-owned Postgres + Redis in WSL2 (persistent, no Docker) |

**Two data classes.** Postgres holds only what can't be recreated — the BD SCA project
*link*, the distilled Coverity compiled-file set, and the `.git` provenance (actual
source → canonical). Everything recreatable — the BD SBoM, Claude outputs, GitHub
trees/commits/diffs, triage verdicts — lives in the Redis cache. A full **recreate**
re-runs the whole pipeline but every external step is a cache hit, so a warm rebuild
makes **zero** Claude/GitHub calls. When the SCA KB grows, the new component is a cache
miss that fetches exactly once.

## Prerequisites

1. `pip install -r requirements-live.txt` (anthropic, SQLAlchemy, alembic, psycopg, redis).
2. **Datastores** (WSL2, one-time): the install of `postgresql` + `redis-server` needs
   sudo once; after that the stack is user-owned and needs no root:
   ```bash
   wsl -d Ubuntu -- sudo bash -c 'apt-get update && apt-get install -y postgresql redis-server'
   ```
3. `blackduck.local.json` (gitignored) — copy `blackduck.local.example.json` and fill in
   `url`, `api_token`, `anthropic_api_key`.
4. A GitHub login for commit/tree pulls: `gh auth login` (or `GITHUB_TOKEN`).

## One-time setup

```bash
# 1. bring up the user-owned Postgres + Redis (ports 5544 / 6380, persistent AOF)
wsl -d Ubuntu -- bash infra/stack.sh up

# 2. create the schema
python -m alembic upgrade head

# 3. seed the can't-recreate tables from the real capture (needs the checkouts + idir)
python provisioning/ingest.py --project repo-mon-stage3-curl --version 8.11.0
```

After step 3 the local build (`stage3/`, the Coverity idir) can be deleted — everything
else is recreatable from the seeds + cache.

## Recreate the dashboard artifacts

```bash
python provisioning/recreate.py --project repo-mon-stage3-curl --out-dir live-stage3
```

- **Cold cache:** makes the BD/Claude/GitHub calls once, populating Redis.
- **Warm cache:** `external_calls: 0` — identical output, no network.
- `--refresh-sbom` re-pulls the BD SBoM to pick up KB growth (new components miss + fetch
  once); `--refresh-events` re-pulls commits on the watch branches.

## Run the demo

### Start everything (one command)

`infra/serve.ps1` brings up the WSL2 datastores, the triage service, and the monitor
(background processes; logs under `logs/`):

```powershell
./infra/serve.ps1 up                 # datastores + triage + monitor
./infra/serve.ps1 up -CacheOnly      # keyless triage (offline, from cache)
./infra/serve.ps1 status
./infra/serve.ps1 down               # stop services (datastores persist; -IncludeDatastores to stop them too)
```

Then fire the saved commit events:

```bash
python driver/replay.py --events live-stage3/repo-mon-stage3-curl-commit-events.json --all
```

### Or start each service by hand

```bash
wsl -d Ubuntu -- bash infra/stack.sh up           # datastores
python triage-service/claude_server.py            # or --cache-only for keyless replay
python monitor/app.py --data-dir live-stage3
python driver/replay.py --events live-stage3/repo-mon-stage3-curl-commit-events.json --all
```

Open **http://127.0.0.1:8378/** — the project list. Click a project to drill in.

Load several projects with repeated `--data-dir`. A push to a shared upstream (zlib,
OpenSSL) routes to every project that watches it.

### What the dashboard shows

- **Precision funnel** — SBoM (BD) → compiled-from-source (BD/CPP) → monitored ·
  reference-only. For this build: 3 → 2 → **monitored curl + zlib**, OpenSSL
  reference-only (linked, never compiled → its commits are `not_monitored`).
- **Watch model** — each monitored repo shows the immutable *pinned ref* (file-scope
  snapshot) and, separately, the moving *watch branch* Claude resolved (curl→`master`,
  zlib→`develop`, OpenSSL→`openssl-3.6`). zlib carries `↳ actual source
  edtice-goog/zlib@… ⚠ divergent` — built from a fork, monitored on canonical `madler/zlib`.
- **Verdicts** — response_required (red), needs_human_review (amber), not_meaningful
  (light green), suppressed (green, not in the compiled set), not_monitored (grey).
  Every card has a collapsible "N changed files" list; in-scope files are tagged.
- **Cross-repo "patch everywhere"** — if the same physical file is compiled into more
  than one repo (an inline-vendored copy), a fix in one surfaces a note to propagate it.

### The Recreate button

On a project page, **🔄 Recreate** refreshes the BoM from Black Duck and re-runs the
cache-backed pipeline in the background (status shown inline). Unchanged BoM ⇒ one BD
call, no Claude/GitHub. Grown BoM ⇒ the new component fetches once. No terminal needed.

## Triage backends

`claude_server.py` calls Claude for a real verdict and **caches input→output** (keyed by
`{vcs_url, commit, files, cross_repo}`). First pass is slow; every repeat is an instant
cache hit with no token spend. `--cache-only` serves purely from cache (offline, keyless)
and fails safe on a miss. Default model `claude-opus-4-6`; `--model claude-opus-5` for
production-grade verdicts (clear the cache when changing models).

## Reset / persistence

- The monitor holds event state in memory only — Ctrl+C and restart to clear the feed.
- The Redis cache and Postgres seeds **persist to disk** (AOF + RDB). After a reboot,
  `wsl -d Ubuntu -- bash infra/stack.sh up` brings the stack back with the cache warm —
  a recreate still makes zero external calls.

## Troubleshooting

- **Can't reach 5544/6380 from Windows** — WSL2 localhost forwarding; run
  `wsl -d Ubuntu -- bash infra/stack.sh status`; if the distro was shut down, `… up`.
- **`alembic`/DB errors** — confirm `DATABASE_URL` (default
  `postgresql+psycopg://repomon@127.0.0.1:5544/repomon`) and that the stack is up.
- **Recreate button shows an error** — the datastores or `blackduck.local.json` aren't
  available; the message is shown inline next to the button.
