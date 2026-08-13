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
