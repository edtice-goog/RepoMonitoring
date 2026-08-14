# RepoMonitoring — Demo Instructions

A deterministic, self-contained demo of upstream security-fix monitoring for
embedded builds. Everything runs locally; there is no network access beyond
localhost, no API keys, and no external dependencies.

## What you need

- **Python 3.8 or newer** — nothing else. No `pip install`, no virtualenv.
  - Windows: `python` or `py`
  - Linux/macOS: `python3`
- A web browser.
- Three terminal windows (two for services, one for firing events).

Throughout these instructions, `python` means whatever invokes Python on your
machine (`py` on some Windows installs, `python3` on Linux/macOS).

## What's in the box

| Path | What it is |
|---|---|
| `monitor/app.py` | The monitoring component: web dashboard + Git webhook endpoint |
| `triage-service/server.py` | Stub "LLM triage service" (deterministic truth table) |
| `driver/replay.py` | Fires simulated upstream commits at the monitor as standard Git webhooks |
| `samples/` | Fictitious tool artifacts: build capture, SBOMs (3 formats), commit events, triage verdicts |
| `samples/README.md` | Why each artifact looks the way it does |
| `DESIGN.md` | Architecture and long-term vision (Black Duck SCA integration) |

The fictitious device is the **ACME GW-7100**, an embedded network gateway
built from source (Yocto-style) from three components: a forked Linux kernel,
BusyBox, and zlib.

## Running the demo

### 1. Start the triage service (terminal 1)

```
cd <unzipped folder>
python triage-service/server.py
```

You should see: `[triage-stub] serving on http://127.0.0.1:8377 ...`

### 2. Start the monitoring component (terminal 2)

```
cd <unzipped folder>
python monitor/app.py
```

You should see it load **4 watched repos** and **27 indexed files**, then:
`[monitor] dashboard http://127.0.0.1:8378/ ...`

### 3. Open the dashboard

Browse to **http://127.0.0.1:8378/** — it refreshes itself every 3 seconds.

You'll see the **watch manifest**: the repos derived from the build capture
and SBOM, including both ACME's internal kernel fork *and* its kernel.org
upstream (fixes land upstream first; the fork is what gets built), with
provenance for how each URL was discovered.

### 4. Fire the simulated commit events (terminal 3)

Interactive mode pauses before each event so you can narrate and watch the
dashboard update:

```
cd <unzipped folder>
python driver/replay.py
```

Or fire one at a time with `--event evt-001` (… `evt-004`), or everything at
once with `--all`. `--list` shows the scenarios.

## What each event demonstrates

| Event | Upstream commit | Expected dashboard result |
|---|---|---|
| **evt-001** | BusyBox: "udhcp: tidy option walking" — a *quiet* security fix touching `dhcpc.c` + `packet.c` + a docs file | **Red — response_required.** Both parser files matched in-scope (tier 1) and evaluated together in one triage call; the docs file excluded. Rationale: bounds checks quietly added to DHCP option parsing — public fix, no advisory yet. |
| **evt-002** | Linux: a *real* security fix in the ath11k wireless driver | **Green — suppressed.** The GW-7100 never builds that driver, so the commit is never selected and triage is never called. A CVE-feed tool would have paged someone. This is the precision story. |
| **evt-003** | Linux: "skbuff: simplify clone path" — ambiguous pointer reorder in `net/core/skbuff.c` | **Amber — needs_human_review.** Could be a use-after-free fix, could be cosmetic; routed to the human queue with reasoning. (Phase-1 rollout: humans triage, the filter does noise reduction.) |
| **evt-004** | zlib: K&R→ANSI declaration modernization in `zutil.c` | **Gray — not_meaningful.** In-scope but no security relevance; verdict and rationale logged for audit, no alert. |

Talking points baked into the data:

- **The commit hash is the unit of triage** — evt-001 shows two in-scope files
  from one commit evaluated together under a single verdict.
- **Every verdict carries reasoning** — including the suppressions; nothing is
  silently dropped. (Triage rationale text is simulated LLM output; in
  production a live model sits behind the same `/triage` endpoint.)
- **Fork + upstream both watched** — see the kernel rows in the watch manifest.

## Resetting between runs

The monitor keeps state in memory only. Stop it (Ctrl+C in terminal 2) and
start it again — the event feed is empty and the demo is ready to replay.

## Live mode (optional) — real Black Duck + Claude + GitHub data

Everything above is the **self-contained, offline demo** (Stage 1): fictitious
ACME GW-7100 data, no network, no keys. That path is unchanged and always
available. Two further stages layer *real* data on top of the same monitor and
driver — useful for building an audience's suspension of disbelief
progressively:

| Stage | Watch manifest | Commit events | Compiled-file index |
|---|---|---|---|
| **1 — sample** (default) | hand-crafted `samples/` | hand-crafted `samples/` | hand-crafted `samples/` |
| **2 — live provisioned** | **live Black Duck BOM**, VCS URLs **resolved by Claude** | **real upstream commits from GitHub** (cached) | real source tree at the release tag (approximated) |
| **3 — genuinely captured** | live Black Duck BOM | real commits | **real BD/CPP Coverity capture** of a build we ran ourselves |

Stage 2 is what the `scripts/` helpers produce from a binary-scan BOM: its
compiled-file index is *approximated* by each component's released source tree
(honest stand-in, flagged `gh_tree_approx`), because WinSCP can't be built
without the Embarcadero C++Builder toolchain.

**Stage 3 is real.** We build a project ourselves under BD/CPP so the
compiled-file index is authoritative — which is the only way to truthfully
separate *monitored* (compiled from source) from *reference-only* (linked but
not compiled). The reference build is **cURL 8.11 + zlib 1.3.1 compiled from
source, linking a prebuilt OpenSSL 3.6.3**:

- `stage3/` (outside the repo) holds the source, an OpenSSL-from-source build
  (`build_openssl.bat` — the linked "vendor" lib), and `build_capture.bat` which
  clean-builds curl + zlib. `capture.bat` runs `blackduck-c-cpp` over that build
  and pushes project `repo-mon-stage3-curl` to Black Duck.
- `scripts/attribute_capture.py` builds the watch set **without assuming files
  are tidily arranged by component** (if they were, you wouldn't need SCA). It
  takes the union of the **Black Duck BoM** and a **Claude reconstruction from the
  compiled file paths** (`cov_emit_links.json`), enumerates each candidate repo's
  file tree, and attributes every compiled file to a repo via the mapping service
  (`scripts/repo_mapper.py`, longest-suffix). A repo is **monitored iff it owns ≥1
  *primary* translation unit** — OpenSSL, whose headers were `#included` but whose
  `.c` were never compiled, owns none → reference-only. Build tools (CMake) are
  excluded. Run it:

```
python scripts/attribute_capture.py     # BD BoM ∪ Claude-from-files → mapped index + union manifest
python scripts/gh_replay.py --manifest live-stage3/hub-api-components.json --out-dir live-stage3 --events-name stage3-commit-events.json --events-only --commits 6
python triage-service/claude_server.py
python monitor/app.py --data-dir live-stage3
python driver/replay.py --events live-stage3/stage3-commit-events.json --all
```

The dashboard shows the **precision funnel** (SBOM 3 → compiled 2 → monitored 2 ·
reference-only 1), curl+zlib under *Monitored repos*, OpenSSL under *Referenced,
not monitored*, and OpenSSL's commits marked `not_monitored` (short-circuited at
the repo level — never relevance-filtered or triaged). A version Black Duck
couldn't supply (a repo only Claude spotted) is shown with an `≈ inferred` flag.

> **Known limitation (next up):** when a compiled file's path exists in more than
> one candidate repo — a vendor **fork** and its **upstream**, or a repo that
> **vendors a copy** of a dependency (CMake bundles curl+zlib, which is why it's
> filtered) — longest-suffix can't tell them apart and breaks the tie
> arbitrarily. This is the first objection an audience will raise; the
> `Attribution.ambiguous` flag marks these, and proper disambiguation is the
> planned follow-up in `repo_mapper.py`.

### Prerequisites for live mode

1. `pip install -r requirements-live.txt` (adds the `anthropic` SDK; the offline
   demo needs nothing).
2. Copy `blackduck.local.example.json` to `blackduck.local.json` (gitignored)
   and fill in `url`, `api_token` (Black Duck access token), `anthropic_api_key`,
   and the `project` / `version` to provision.
3. A GitHub login for the commit pull: `gh auth login` (or set `GITHUB_TOKEN`).

### Building the live artifacts (one time — writes to `live/`, gitignored)

```
python scripts/bd_scout.py                 # optional: find a project with a 3-7 component BOM
python scripts/bd_provision.py             # live BD BOM + Claude VCS enhancement -> live/hub-api-components.json
python scripts/gh_replay.py --commits 8    # real commits + file index    -> live/winscp-commit-events.json, live/build-capture.json
```

The commit/index data for an old release tag is effectively static, so fetch it
**once** and replay the saved files thereafter — no repeated GitHub calls.

### Running the live demo (same three terminals, `--data-dir live`)

```
python triage-service/server.py
python monitor/app.py --data-dir live
python driver/replay.py --events live/winscp-commit-events.json --all
```

The dashboard now shows the live watch manifest (real repos, `sca:kb_vcs_url` +
`capture:gh_tree` provenance, real pinned refs) and real upstream commits sorted
into in-scope (triaged) vs suppressed.

**Triage backend — stub vs live Claude.** With the *stub* `server.py`, every
matched real commit routes to `needs_human_review` (it only knows the four canned
sample hashes and fails safe on the rest). To get **real per-commit verdicts**,
run the live triage service instead of the stub in terminal 1:

```
python triage-service/claude_server.py     # calls Claude; same port/contract as the stub
```

It fetches each commit's message + diff from GitHub, asks Claude for a structured
verdict, and **caches input→output under `live/triage-cache/`**. The first pass is
slow (one model call per matched commit); every repeat of the same request is an
instant cache hit with no token spend — so re-running the demo, or two users
issuing the same request, share one answer. Default model is the economical
`claude-opus-4-6`; pass `--model claude-opus-5` for production-grade verdicts
(clear `live/triage-cache/` when changing models). `--cache-only` serves purely
from cache (offline, no keys) and fails safe on a miss.

## Resetting between runs (live mode)

Same as above — the monitor holds no disk state. To re-fetch live data (e.g.
after a new Black Duck scan), re-run the `scripts/` steps; the `live/` directory
is overwritten and is gitignored so instance-specific data never gets committed.

## Troubleshooting

- **"Address already in use"** — another process holds port 8377 or 8378. Use
  `--port` on either service; if you move them, also pass
  `--triage-url http://127.0.0.1:<port>/triage` to the monitor and
  `--webhook http://127.0.0.1:<port>/webhook` to the driver.
- **Driver prints "webhook delivery failed"** — the monitor isn't running, or
  is on a different port than the driver expects.
- **Cards show `triage_error`** — the monitor is up but the triage service
  isn't; start terminal 1 and re-fire the event.
- **Dashboard doesn't update** — it auto-refreshes every 3 s; force-reload
  once if your browser cached an old page.
